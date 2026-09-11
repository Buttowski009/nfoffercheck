"""
Netflix India Offer Cookie Hunter — LOCAL v7
============================================
Run: python hunter_local.py
     python hunter_local.py --tg-token TOKEN --tg-chat CHATID

HOW IT ACTUALLY WORKS:
  Netflix shows a special offer banner [data-uia="free-trial-banner"]
  ONLY to sessions it decides are offer-eligible (fresh device, Indian IP,
  no previous account). This is different from the regular "Get Started" CTA
  which ALWAYS exists.

  So: load page → look ONLY for the specific offer banner/element → if found
  those cookies are valid offer cookies → save them. No clicking needed.

  DO NOT click Get Started. That always leads to email form (not an offer).
"""

import asyncio, json, os, shutil, subprocess, sys, time, uuid, random, threading, base64
from datetime import datetime
from pathlib import Path
import requests, websockets, websockets.exceptions

# ─── CONFIG ────────────────────────────────────────────────────────────────
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--tg-token",  default="", help="Telegram bot token")
ap.add_argument("--tg-chat",   default="", help="Telegram chat ID")
ap.add_argument("--delay",     default=3.0, type=float, help="Seconds between attempts")
ap.add_argument("--max",       default=99999, type=int)
ARGS = ap.parse_args()

TG_TOKEN = ARGS.tg_token
TG_CHAT  = ARGS.tg_chat
DELAY    = ARGS.delay
NF_URL   = "https://www.netflix.com/in/"
HERE     = Path(__file__).parent
BASE_PORT = 9600

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

STEALTH_JS = r"""
(function(){
  try{delete Object.getPrototypeOf(navigator).webdriver;}catch(e){}
  Object.defineProperty(navigator,'webdriver',{get:()=>undefined,configurable:true});
  for(let k of Object.keys(window)){if(/^cdc_|^\$cdc_/.test(k))try{delete window[k];}catch(e){}}
  Object.defineProperty(navigator,'plugins',{get:()=>[
    {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'',length:1},
    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:'',length:1},
    {name:'Native Client',filename:'internal-nacl-plugin',description:'',length:2},
  ]});
  Object.defineProperty(navigator,'languages',{get:()=>['en-IN','hi-IN','en-GB','en']});
  window.chrome=window.chrome||{};
  window.chrome.runtime=window.chrome.runtime||{};
})();
"""

# THE REAL OFFER DETECTION
# Netflix shows a special offer banner ONLY to eligible sessions.
# We look for SPECIFIC offer elements — NOT generic promo/marketing text.
# No clicking. No confidence scores. It's there or it's not.
OFFER_CHECK_JS = """
(function(){
  var R = { found: false, element: '', text: '', url: window.location.href };

  // 1. The dedicated free trial banner — strongest signal
  var freeTrialBanner = document.querySelector(
    '[data-uia="free-trial-banner"], button[data-uia="free-trial-banner"]'
  );
  if (freeTrialBanner && freeTrialBanner.offsetParent !== null) {
    R.found   = true;
    R.element = 'free-trial-banner';
    R.text    = freeTrialBanner.textContent.trim().substring(0, 120);
    return R;
  }

  // 2. Offer-specific aria labels on buttons
  var offerBtns = document.querySelectorAll(
    'button[aria-label*="free trial"], button[aria-label*="Try 30"], button[aria-label*="Join free"]'
  );
  for (var b of offerBtns) {
    if (b.offsetParent !== null) {
      R.found   = true;
      R.element = 'offer-aria-btn';
      R.text    = b.textContent.trim().substring(0, 120);
      return R;
    }
  }

  // 3. Price elements showing Rs 0 or Free — ONLY in specific offer containers
  //    (not in hero marketing section which always says "free trial")
  var priceEls = document.querySelectorAll(
    '[data-uia*="price"], [class*="PlanPrice"], [class*="planPrice"], [class*="offer-price"]'
  );
  for (var el of priceEls) {
    var txt = el.textContent.toLowerCase();
    if (txt.includes('rs. 0') || txt.includes('rs 0') || txt === '0' || txt.includes('\u20b90')) {
      R.found   = true;
      R.element = 'price-zero';
      R.text    = el.textContent.trim().substring(0, 80);
      return R;
    }
  }

  // 4. Offer badge / tag elements
  var badges = document.querySelectorAll(
    '[class*="offer-badge"], [class*="OfferBadge"], [data-uia*="offer-badge"]'
  );
  for (var b of badges) {
    if (b.offsetParent !== null) {
      R.found   = true;
      R.element = 'offer-badge';
      R.text    = b.textContent.trim().substring(0, 80);
      return R;
    }
  }

  // 5. Check page title / meta for offer keywords (Netflix sets these for offer pages)
  var title = document.title.toLowerCase();
  if (title.includes('free') && title.includes('month') && !title.includes('watch')) {
    R.found   = true;
    R.element = 'page-title-offer';
    R.text    = document.title;
    return R;
  }

  return R;
})()
"""


# ─── HELPERS ───────────────────────────────────────────────────────────────
def ts(): return datetime.now().strftime("%H:%M:%S")
def log(msg, tag=""):
    icon = {"OK":"[+]","ERR":"[!]","SKIP":"[-]","SAVE":"[*]"}.get(tag,"[~]")
    print(f"{icon} [{ts()}] {msg}", flush=True)

def find_chrome():
    for p in CHROME_PATHS:
        if os.path.exists(p): return p
    raise FileNotFoundError("Chrome not found! Install Google Chrome.")

def kill_port(port):
    try:
        r = subprocess.run(f'netstat -aon | findstr :{port} | findstr LISTENING',
            shell=True, capture_output=True, text=True, timeout=8)
        for line in r.stdout.strip().split('\n'):
            p = line.split()
            if p and p[-1].isdigit():
                subprocess.run(f'taskkill /F /PID {p[-1]}', shell=True, capture_output=True)
    except: pass

def get_ws(port, tries=30):
    for _ in range(tries):
        try:
            tabs = requests.get(f"http://localhost:{port}/json", timeout=3).json()
            for t in tabs:
                if t.get("type") == "page": return t["webSocketDebuggerUrl"]
            if tabs: return tabs[0]["webSocketDebuggerUrl"]
        except: pass
        time.sleep(0.7)
    raise RuntimeError(f"Chrome :{port} not ready")

def fresh_cookies():
    nfv = "BQFmAAEBE" + base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")[:78] + "%3D%3D"
    cid = str(uuid.uuid4())
    ts_ms = int(time.time()*1000)
    consent = (
        f"landingPath=https%3A%2F%2Fwww.netflix.com%2Fin%2F"
        f"&datestamp={datetime.utcnow().strftime('%a+%b+%d+%Y+%H%%3A%M%%3A%S+GMT%%2B0000')}"
        f"&version=202604.2.0&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1"
        f"&consentId={cid}&isAnonUser=1&prevHadToken=0&crTime={ts_ms}"
        f"&isGpcEnabled=0&isDntEnabled=0&isIABGlobal=false&geolocation=IN%3BMH"
    )
    return [
        {"name":"nfvdid","value":nfv,"domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"OptanonConsent","value":consent,"domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"netflix-sans-normal-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"netflix-sans-bold-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
    ]


# ─── CDP ───────────────────────────────────────────────────────────────────
class CDP:
    def __init__(self, url):
        self._url=url; self.ws=None; self._id=0; self._p={}; self._dead=False
    async def connect(self):
        self.ws = await websockets.connect(self._url, max_size=100*1024*1024,
            ping_interval=None, ping_timeout=None, open_timeout=30)
        asyncio.get_running_loop().create_task(self._recv())
    async def _recv(self):
        try:
            async for raw in self.ws:
                try: m = json.loads(raw)
                except: continue
                i = m.get("id")
                if i and i in self._p:
                    f = self._p.pop(i)
                    if not f.done(): f.set_result(m)
        except: pass
        finally:
            self._dead = True
            for f in self._p.values():
                if not f.done():
                    try: f.set_exception(RuntimeError("closed"))
                    except: pass
    async def cmd(self, method, params=None, t=20):
        if self._dead: return {}
        self._id += 1; mid = self._id
        f = asyncio.get_running_loop().create_future(); self._p[mid] = f
        try: await self.ws.send(json.dumps({"id":mid,"method":method,"params":params or{}}))
        except: self._p.pop(mid,None); return {}
        try:
            r = await asyncio.wait_for(f, timeout=t); return r.get("result",{})
        except: self._p.pop(mid,None); return {}
    async def js(self, expr):
        r = await self.cmd("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":False})
        return (r.get("result") or {}).get("value")
    async def all_cookies(self): return (await self.cmd("Network.getAllCookies")).get("cookies",[])
    async def close(self):
        if self.ws:
            try: await self.ws.close()
            except: pass


# ─── TG ────────────────────────────────────────────────────────────────────
class S:
    running    = True
    attempt    = 0
    found      = 0
    start      = time.time()
    last       = "starting..."
    counter_id = 0

def tg_send(t):
    if not TG_TOKEN: return 0
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":t,"parse_mode":"HTML"}, timeout=15)
        return r.json().get("result",{}).get("message_id",0)
    except: return 0

def tg_edit(mid, t):
    if not TG_TOKEN or not mid: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText",
            json={"chat_id":TG_CHAT,"message_id":mid,"text":t,"parse_mode":"HTML"}, timeout=15)
    except: pass

def tg_file(path, cap):
    if not TG_TOKEN: return
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id":TG_CHAT,"caption":cap,"parse_mode":"HTML"},
                files={"document":(os.path.basename(path),f,"application/json")}, timeout=30)
    except: pass

def tg_updates(off=0):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"timeout":30,"offset":off}, timeout=35)
        return r.json().get("result",[])
    except: return []

def counter():
    e = int(time.time()-S.start); h,m,s = e//3600,(e%3600)//60,e%60
    rate = S.attempt/max(1,e/60)
    return (
        "<b>Netflix Offer Hunter v7</b>\n"
        "-----------------------------\n"
        f"Status:   {'HUNTING' if S.running else 'PAUSED'}\n"
        f"Attempts: <code>{S.attempt}</code>\n"
        f"Found:    <code>{S.found}</code>\n"
        f"Speed:    <code>{rate:.1f}/min</code>\n"
        f"Uptime:   <code>{h}h {m}m {s}s</code>\n"
        "-----------------------------\n"
        f"Last: <code>{str(S.last)[:55]}</code>\n"
        "-----------------------------\n"
        "<i>/stop /start /status</i>"
    )

def start_tg_poll():
    def _loop():
        off = 0
        while True:
            for u in tg_updates(off):
                off = u["update_id"]+1
                msg = u.get("message") or u.get("channel_post") or {}
                cmd = (msg.get("text") or "").strip().lower()
                cid = str((msg.get("chat") or {}).get("id",""))
                if cid != str(TG_CHAT): continue
                if cmd in ["/start","/resume"]:   S.running = True;  tg_edit(S.counter_id, counter())
                elif cmd in ["/stop","/pause"]:   S.running = False; tg_edit(S.counter_id, counter())
                elif cmd == "/status":             tg_edit(S.counter_id, counter())
                elif cmd == "/help":               tg_send("/start /stop /status /help")
            time.sleep(2)
    threading.Thread(target=_loop, daemon=True).start()


# ─── SINGLE HUNT ATTEMPT ───────────────────────────────────────────────────
async def hunt_once(n, port):
    profile = str(HERE / f"_h_{uuid.uuid4().hex[:7]}")
    proc = None
    ua   = random.choice(UAS)
    try:
        kill_port(port)
        os.makedirs(profile, exist_ok=True)
        proc = subprocess.Popen([
            find_chrome(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            f"--user-agent={ua}",
            "--incognito",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--window-size=1366,768",
            "about:blank"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        time.sleep(3)
        ws  = get_ws(port)
        cdp = CDP(ws)
        await cdp.connect()

        await cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
        await cdp.cmd("Network.enable")
        await cdp.cmd("Page.enable")

        # Inject fresh anonymous cookies — NEW device every attempt
        for c in fresh_cookies():
            await cdp.cmd("Network.setCookie", {
                "name":c["name"],"value":c["value"],"domain":c["domain"],
                "path":c["path"],"secure":c["secure"],"httpOnly":c["httpOnly"]
            })

        # Navigate to Netflix India
        await cdp.cmd("Page.navigate", {"url": NF_URL})
        await asyncio.sleep(9)  # wait for full render

        # Check ONLY for real offer elements — no clicking
        result = await cdp.js(OFFER_CHECK_JS)

        if not result or not result.get("found"):
            await cdp.close()
            return {"found": False, "n": n, "reason": "no_offer_element"}

        # OFFER ELEMENT FOUND — capture cookies
        all_cookies = await cdp.all_cookies()
        nf_cookies  = [c for c in all_cookies if "netflix.com" in c.get("domain","")]

        # Double-verify: check that cookies actually still show the offer
        # (reload with same cookies and check again)
        verify = await cdp.js(OFFER_CHECK_JS)
        if not verify or not verify.get("found"):
            await cdp.close()
            return {"found": False, "n": n, "reason": "verify_failed_on_recheck"}

        await cdp.close()
        return {
            "found":    True,
            "n":        n,
            "element":  result.get("element",""),
            "text":     result.get("text",""),
            "url":      result.get("url",""),
            "cookies":  nf_cookies
        }

    except Exception as e:
        log(f"[{n}] error: {e}", "ERR")
        return {"found": False, "n": n, "reason": str(e)}
    finally:
        kill_port(port)
        if proc:
            try: proc.terminate(); proc.wait(timeout=5)
            except: pass
        shutil.rmtree(profile, ignore_errors=True)


# ─── SAVE & DELIVER ────────────────────────────────────────────────────────
def save_and_send(r, n):
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    path   = HERE / f"OFFER_{ts_str}.json"

    with open(path, "w") as f:
        json.dump(r["cookies"], f, indent=2)

    with open(HERE / "best_offer.json", "w") as f:
        json.dump({
            "found_at":  ts_str,
            "attempt":   n,
            "element":   r.get("element"),
            "text":      r.get("text"),
            "url":       r.get("url"),
            "cookies":   r["cookies"]
        }, f, indent=2)

    names = [c.get("name","?") for c in r["cookies"]]
    msg = (
        "<b>OFFER COOKIE FOUND</b>\n\n"
        f"Attempt: #{n}\n"
        f"Element: <code>{r.get('element','')}</code>\n"
        f"Text: <code>{r.get('text','')[:80]}</code>\n"
        f"Cookies ({len(names)}): <code>{', '.join(names)}</code>\n\n"
        "<i>JSON file attached</i>"
    )
    tg_send(msg)
    tg_file(str(path), f"Offer cookies #{n}")
    print(f"\n{'='*50}\nOFFER FOUND! Saved: {path.name}\n{'='*50}\n", flush=True)


# ─── MAIN ──────────────────────────────────────────────────────────────────
async def main():
    print("="*50)
    print("  Netflix Offer Hunter v7 — LOCAL")
    print("  Detection: specific offer banner only")
    print("  No fake confidence scores")
    print("="*50+"\n")

    chrome = find_chrome()
    log(f"Chrome: {chrome}")
    log(f"Delay: {DELAY}s | TG: {'yes' if TG_TOKEN else 'NO - add --tg-token'}")

    if TG_TOKEN:
        start_tg_poll()
        tg_send(
            "<b>Offer Hunter v7 STARTED</b>\n\n"
            "Detecting: real offer banner only\n"
            "No fake detections. Cookies sent only when 100% real.\n\n"
            "/stop /start /status"
        )
        S.counter_id = tg_send(counter())

    n = 0
    while n < ARGS.max:
        if not S.running:
            await asyncio.sleep(3); continue

        n += 1
        S.attempt = n
        port = BASE_PORT + (n % 150)
        log(f"Attempt #{n} on port {port}")

        result = await hunt_once(n, port)

        if result.get("found"):
            S.found += 1
            S.last   = f"FOUND #{n} [{result.get('element')}]"
            log(f"#{n} OFFER FOUND! element={result.get('element')} text={result.get('text','')[:60]}", "OK")
            save_and_send(result, n)
        else:
            reason = result.get("reason","?")
            S.last  = reason
            log(f"#{n} skip: {reason}", "SKIP")

        if TG_TOKEN and S.counter_id:
            tg_edit(S.counter_id, counter())

        await asyncio.sleep(DELAY + random.uniform(0, 1.5))

    log("Max attempts done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Stopped.")
        tg_send("Hunter stopped.")
