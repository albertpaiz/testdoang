"""
Titan Exchange TESTNET auto-trader (headless).
Siklus: buka SEMUA koin barengan, arah long/short acak, pakai 50% saldo,
tahan 30-90 detik, close semua, jeda 1-2 menit, ulang terus.

Install (sekali):
    pip install playwright
    playwright install --with-deps chromium
    (butuh layar: jalankan vncserver dulu lalu export DISPLAY=:1)
Jalankan:
    python titan_bot.py
Stop: Ctrl+C
"""
import asyncio, random, re, os, time, json
from playwright.async_api import async_playwright

# ================== CONFIG ==================
EMAIL = "isi_email_kamu@gmail.com"
PASSWORD = "isi_password_kamu"
MARKETS = ["btc-usdcx", "eth-usdcx", "sol-usdcx", "cc-usdcx"]
BALANCE_PCT = 0.50          # 50% saldo dipakai, dibagi rata ke semua market
HOLD_SEC = (30, 90)         # lama posisi ditahan sebelum close (detik)
DELAY_MIN = (1, 2)          # jeda antar siklus (menit)
# tiap siklus SEMUA koin dibuka barengan (tab paralel), arah long/short acak per koin
HEADLESS = None  # otomatis: run pertama tampil untuk login, berikutnya tersembunyi
BASE = "https://testnet.titanx.cc/trade/"
PROFILE_DIR = "titan_chromium_profile"   # sesi login disimpan di sini
BROWSER_CHANNEL = None   # Termux/Ubuntu: pakai Chromium bawaan Playwright
LOGIN_MARK = os.path.join(PROFILE_DIR, ".logged_in")
# ============================================

def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)

def num(text):
    m = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else 0.0

async def logged_in(page):
    try:
        if not await page.get_by_text("Available to trade").count():
            return False
        if await page.get_by_text("Connect a wallet to view").count():
            return False
        return True
    except Exception:
        return False

async def login(page):
    await page.goto(BASE + MARKETS[0])
    for _ in range(15):  # tunggu sampai 45 detik (Cloudflare + loading)
        await page.wait_for_timeout(3000)
        if await logged_in(page):
            log("Sudah login (pakai sesi tersimpan)")
            return
    log("Login...")
    try:
        await page.locator("text=Connect").first.click(timeout=10000)
        await page.wait_for_timeout(2000)
        for t in ["Email", "email", "Continue with email", "Sign in with email"]:
            el = page.get_by_text(t, exact=False)
            if await el.count():
                try:
                    await el.first.click(timeout=2000); break
                except Exception:
                    pass
        await page.locator("input[type=email], input[name*=email i], input[placeholder*=mail i]").first.fill(EMAIL, timeout=10000)
        pw = page.locator("input[type=password]")
        if not await pw.count():
            await page.keyboard.press("Enter")
            await pw.first.wait_for(timeout=15000)
        await pw.first.fill(PASSWORD)
        await page.keyboard.press("Enter")
    except Exception as e:
        log(f"Auto-login belum cocok ({e.__class__.__name__}). Silakan login MANUAL di jendela browser, bot menunggu...")
    # tunggu sampai saldo muncul (maks 5 menit) - bisa login otomatis atau manual
    for _ in range(100):
        await page.wait_for_timeout(3000)
        enable = page.get_by_role("button", name=re.compile("Enable Trading", re.I))
        if await enable.count():
            try:
                await enable.first.click(timeout=3000)
            except Exception:
                pass
        if await logged_in(page):
            log("Login OK")
            return
    raise RuntimeError("Login gagal: saldo tidak muncul dalam 5 menit")

async def available(page):
    row = page.get_by_text("Available to trade").first.locator("xpath=..")
    return num(await row.inner_text())

OVERLAY = "div.fixed.inset-0[aria-hidden=true]"

async def handle_confirm(page, timeout_ms=4000):
    """Klik sheet konfirmasi (Confirm Buy / Confirm Sell / Confirm Close) kalau muncul."""
    conf = page.get_by_role("button", name=re.compile(r"^Confirm", re.I))
    for _ in range(timeout_ms // 250):
        if await conf.count() and await conf.last.is_visible():
            dont = page.get_by_text(re.compile(r"Don.t ask again", re.I))
            try:
                if await dont.count():
                    await dont.first.click(timeout=1500)
            except Exception:
                pass
            await conf.last.click(timeout=3000)
            await page.wait_for_timeout(1500)
            return True
        await page.wait_for_timeout(250)
    return False

async def clear_overlays(page):
    """Tutup pengumuman 'Titan Update' (announce sheet), banner testnet, dan popup lain."""
    for sel in ["button.announce-close", "button.sheet-btn-secondary"]:
        b = page.locator(sel)
        try:
            if await b.count() and await b.first.is_visible():
                await b.first.click(timeout=3000)
                await page.wait_for_timeout(600)
        except Exception:
            pass
    for _ in range(3):
        if not await page.locator(OVERLAY).count():
            return
        if await handle_confirm(page, 500):
            continue
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(700)

DECIMALS = {"btc": 5, "eth": 4, "sol": 2, "cc": 0}

async def submit_enabled(page, timeout_ms=5000):
    btn = page.locator("button[data-tour=confirm]").first
    for _ in range(timeout_ms // 250):
        try:
            if await btn.is_enabled():
                return True
        except Exception:
            pass
        await page.wait_for_timeout(250)
    return False

OPEN_FILE = "open_positions.json"

def load_open():
    try:
        return json.load(open(OPEN_FILE))
    except Exception:
        return {}

def save_open(d):
    json.dump(d, open(OPEN_FILE, "w"))

async def close_tracked(ctx, opened):
    """Close posisi lewat order lawan arah + Reduce only, paralel. Return yang gagal."""
    if not opened:
        return {}
    tabs = [await ctx.new_page() for _ in opened]
    res = await asyncio.gather(*[close_position(t, m, sd, v) for t, (m, (sd, v)) in zip(tabs, opened.items())], return_exceptions=True)
    failed = {}
    for (m, val), r in zip(opened.items(), res):
        if isinstance(r, Exception):
            log(f"Gagal close {m}: {short_err(r)}")
            failed[m] = val
    for t in tabs:
        try: await t.close()
        except Exception: pass
    return failed

def short_err(e):
    lines = [l.strip() for l in str(e).splitlines() if l.strip()]
    wait = next((l for l in lines if l.startswith("- waiting for")), "")
    return (lines[0] if lines else e.__class__.__name__) + (" | " + wait if wait else "")

def panel(page):
    """Panel order (kotak kanan) - supaya tidak salah klik tulisan 'Market' di tabel Positions."""
    return page.locator("button[data-tour=confirm]").first.locator(
        "xpath=ancestor::*[.//*[normalize-space(text())='Market'] and .//*[normalize-space(text())='Buy / Long'] and .//*[normalize-space(text())='Size']][1]")

async def set_reduce_only(page, on):
    try:
        cb = page.get_by_role("checkbox", name=re.compile("Reduce only", re.I))
        if await cb.count():
            if (await cb.first.is_checked()) != on:
                await cb.first.click(timeout=3000)
            return
        sw = page.get_by_role("switch", name=re.compile("Reduce only", re.I))
        if await sw.count():
            state = (await sw.first.get_attribute("aria-checked")) == "true"
            if state != on:
                await sw.first.click(timeout=3000)
            return
        if on:
            await page.get_by_text("Reduce only", exact=True).first.click(timeout=3000)
    except Exception:
        pass

async def place_order(page, market, side, margin=None, size=None, reduce_only=False):
    await page.goto(BASE + market)
    submit = page.locator("button[data-tour=confirm]").first
    await submit.wait_for(state="attached", timeout=30000)
    await page.wait_for_timeout(2000)
    await clear_overlays(page)
    # tab order Market (button.wallet-tab), bukan header tabel Positions
    await page.locator("button.wallet-tab", has_text=re.compile(r"^Market$")).first.click(timeout=10000)
    tab = "Buy / Long" if side == "long" else "Sell / Short"
    await page.get_by_role("button", name=tab, exact=True).first.click(timeout=10000)
    await page.wait_for_timeout(400)
    await set_reduce_only(page, reduce_only)
    try:
        lev = num(await page.get_by_text(re.compile(r"^\d+×$")).first.inner_text(timeout=3000)) or 10
    except Exception:
        lev = 10
    asset = market.split("-")[0]
    size_box = page.get_by_role("textbox", name="Size", exact=True)
    if not await size_box.count():
        size_box = page.locator('input[inputmode="decimal"][placeholder="0"]')
    size_box = size_box.first
    if size is None:
        notional = margin * lev
        mark = num(await page.get_by_text("Mark", exact=True).first.locator("xpath=..").inner_text())
        value = f"{notional / mark:.{DECIMALS.get(asset, 3)}f}" if mark else f"{notional:.2f}"
    else:
        value = size
    # pastikan satuan size = koin (tombol di kanan Size harus menunjukkan nama koin)
    unit_btn = page.get_by_role("button", name=re.compile(r"swap_vert$")).filter(has_text=re.compile(r"^(BTC|ETH|SOL|CC|USDCx)"))
    try:
        for i in range(await unit_btn.count()):
            t = (await unit_btn.nth(i).inner_text()).strip()
            if t.startswith("USDCx"):
                await unit_btn.nth(i).click(timeout=2000)
                break
    except Exception:
        pass
    await size_box.click(timeout=10000)
    await size_box.fill("")
    await size_box.press_sequentially(value, delay=40)
    if not await submit_enabled(page):
        if reduce_only:
            raise RuntimeError("tombol close nonaktif (posisi mungkin sudah tertutup)")
        log(f"{market}: size {value} ditolak, pakai tombol 25%")
        await page.get_by_role("button", name="25%", exact=True).first.click()
        if not await submit_enabled(page):
            raise RuntimeError("tombol order tetap nonaktif (saldo kurang / size di bawah minimum)")
    await submit.click(timeout=10000)
    await page.wait_for_timeout(1000)
    await handle_confirm(page)
    await clear_overlays(page)
    await page.wait_for_timeout(1500)
    if reduce_only:
        await set_reduce_only(page, False)
    return lev, value

async def open_position(page, market, margin):
    side = random.choice(["long", "short"])
    lev, value = await place_order(page, market, side, margin=margin)
    log(f"OPEN {market.upper()} {side.upper()} {lev:.0f}x size {value}")
    return side, value

async def close_position(page, market, side, value):
    opp = "short" if side == "long" else "long"
    await place_order(page, market, opp, size=value, reduce_only=True)
    log(f"CLOSE {market.upper()} {side.upper()} size {value}")

async def check_positions(page):
    await page.goto(BASE + MARKETS[0])
    await page.wait_for_timeout(4000)
    try:
        txt = await page.get_by_text(re.compile(r"^Positions")).last.inner_text(timeout=5000)
        left = int(num(txt))
        log("Cek: semua posisi tertutup" if left == 0 else f"PERINGATAN: masih ada {left} posisi terbuka")
    except Exception:
        pass

async def close_all(page):
    await page.goto(BASE + MARKETS[0])
    await page.wait_for_timeout(4000)
    await clear_overlays(page)
    tab = page.get_by_text(re.compile(r"^Positions"))
    await tab.last.click()
    await page.wait_for_timeout(1500)
    # 1) tombol "Close All" kalau ada
    close_all_btn = page.get_by_role("button", name=re.compile(r"close all", re.I))
    if await close_all_btn.count():
        await close_all_btn.first.click()
        await handle_confirm(page)
        await page.wait_for_timeout(3000)
    # 2) close per baris posisi
    for _ in range(12):
        await clear_overlays(page)
        btn = page.get_by_role("button", name=re.compile(r"^(Close|Market|Market Close|Close Market)$", re.I))
        if not await btn.count():
            break
        try:
            await btn.first.click(timeout=5000)
        except Exception as e:
            log(f"Klik close gagal: {e.__class__.__name__}")
            await page.keyboard.press("Escape")
            continue
        await page.wait_for_timeout(1000)
        # kadang muncul dialog close dengan tombol "Close" / "Confirm"
        dlg = page.get_by_role("button", name=re.compile(r"^(Confirm|Close Position|Close)$", re.I))
        if not await handle_confirm(page, 2000) and await dlg.count() > 1:
            try:
                await dlg.last.click(timeout=3000)
            except Exception:
                pass
        await page.wait_for_timeout(2500)
    txt = await tab.last.inner_text()
    left = int(num(txt))
    log("Semua posisi di-close" if left == 0 else f"PERINGATAN: masih ada {left} posisi terbuka")

async def main():
    async with async_playwright() as p:
        hidden = os.path.exists(LOGIN_MARK)
        args = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
        if hidden:
            # bukan headless (Cloudflare memblokir headless), tapi jendela digeser keluar layar
            args += ["--window-position=-32000,-32000", "--window-size=1440,900"]
        ctx = await p.chromium.launch_persistent_context(
            PROFILE_DIR, channel=BROWSER_CHANNEL, headless=False,
            viewport={"width": 1440, "height": 900},
            ignore_default_args=["--enable-automation"], args=args)
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await login(page)
        except Exception:
            if hidden:
                log("Sesi habis. Hapus file titan_chromium_profile/.logged_in lalu jalankan ulang untuk login lagi.")
                try: os.remove(LOGIN_MARK)
                except Exception: pass
            raise
        open(LOGIN_MARK, "w").write("ok")
        if not hidden:
            log("Login tersimpan. Run berikutnya browser tersembunyi (di luar layar).")
        log("Menutup posisi lama dulu...")
        left = load_open()
        if left:
            save_open(await close_tracked(ctx, left))
        await check_positions(page)
        cycle = 0
        while True:
            cycle += 1
            try:
                await page.goto(BASE + MARKETS[0]); await page.wait_for_timeout(3000)
                bal = await available(page)
                picks = MARKETS[:]
                margin = bal * BALANCE_PCT / len(picks)
                log(f"Siklus {cycle} | saldo ${bal:,.2f} | margin/koin ${margin:,.2f} | buka {len(picks)} koin barengan")
                tabs = [await ctx.new_page() for _ in picks]
                results = await asyncio.gather(*[open_position(t, m, margin) for t, m in zip(tabs, picks)], return_exceptions=True)
                opened = {}
                for m, r in zip(picks, results):
                    if isinstance(r, Exception):
                        log(f"Gagal open {m}: {short_err(r)}")
                    else:
                        opened[m] = list(r)
                save_open(opened)
                for t in tabs:
                    try: await t.close()
                    except Exception: pass
                hold = random.randint(*HOLD_SEC)
                log(f"Tahan {hold} detik"); await asyncio.sleep(hold)
                failed = await close_tracked(ctx, opened)
                if failed:
                    log("Coba close ulang yang gagal...")
                    failed = await close_tracked(ctx, failed)
                save_open(failed)
                await check_positions(page)
            except Exception as e:
                log(f"Error siklus: {short_err(e)}")
                try: save_open(await close_tracked(ctx, load_open()))
                except Exception: pass
                try: await login(page)
                except Exception: pass
            wait = random.uniform(*DELAY_MIN) * 60
            log(f"Jeda {wait/60:.1f} menit"); await asyncio.sleep(wait)

if __name__ == "__main__":
    asyncio.run(main())
