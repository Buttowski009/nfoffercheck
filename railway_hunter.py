"""
╔══════════════════════════════════════════════════════════════╗
║   NETFLIX OFFER HUNTER — Railway Edition  v4.0              ║
║   Headless Chrome · India only · Telegram delivery          ║
║                                                             ║
║   ENV VARS needed on Railway:                               ║
║     TG_TOKEN   = your telegram bot token                    ║
║     TG_CHAT_ID = your chat/user id                          ║
║     DELAY      = seconds between attempts (default 5)       ║
╚══════════════════════════════════════════════════════════════╝

HOW TO USE:
  1. Push this folder to GitHub
  2. New project on Railway → Deploy from GitHub
  3. Set env vars TG_TOKEN + TG_CHAT_ID
  4. Bot starts auto-hunting
  5. When offer found → bot sends you cookies.json directly on Telegram
  6. /status anytime to check progress
  7. /stop to pause, /start to resume

HEADLESS CHROME FLOW (Linux/Railway):
  - Uses Chromium installed via apt in Dockerfile
  - Each attempt: fresh temp profile → headless Chrome → detect offer → dump cookies
  - No display needed, no GUI, pure CDP over websocket
"""

import asyncio, json, os, re, shutil, subprocess, sys, time, uuid, random, signal
from datetime import datetime
from pathlib import Path
import requests, websockets, websockets.exceptions

# ─── Env Config ───────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
DELAY      = float(os.environ.get("DELAY", "5"))

if not TG_TOKEN or not TG_CHAT_ID:
    print("❌ ERROR: Set TG_TOKEN and TG_CHAT_ID environment variables!")
    sys.exit(1)

# ─── Chrome paths (Linux first for Railway, Windows fallback for local test) ──
CHROME_PATHS_LINUX = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
]
CHROME_PATHS_WIN = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

BASE_PORT   = 9500
PAGE_WAIT   = 10
OFFER_WAIT  = 22

# ─── India ONLY — that's where the offer shows ────────────────────────────────
NETFLIX_URL = "https://www.netflix.com/in/"
REGION      = "IN"
GEO_COOKIE  = "geolocation=IN%3BMH"

# ─── Rotate user agents (Indian Chrome pattern) ───────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]

# ─── Stealth JS ───────────────────────────────────────────────────────────────
STEALTH_JS = r"""
(function(){
  try{delete Object.getPrototypeOf(navigator).webdriver;}catch(e){}
  Object.defineProperty(navigator,'webdriver',{get:()=>undefined,configurable:true});
  for(let k of Object.keys(window)){if(/^cdc_|^\$cdc_/.test(k)){try{delete window[k];}catch(e){}}}
  for(let k of Object.keys(document)){if(/^cdc_|^\$cdc_/.test(k)){try{delete document[k];}catch(e){}}}
  Object.defineProperty(navigator,'plugins',{get:()=>[
    {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'PDF',length:1},
    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:'',length:1},
    {name:'Native Client',filename:'internal-nacl-plugin',description:'',length:2},
  ]});
  Object.defineProperty(navigator,'languages',{get:()=>['en-IN','hi-IN','en-GB','en']});
  window.chrome=window.chrome||{};
  window.chrome.runtime=window.chrome.runtime||{};
  if(!window.chrome.csi)window.chrome.csi=()=>({startE:performance.now(),onloadT:performance.now()});
  if(!window.chrome.loadTimes)window.chrome.loadTimes=()=>({commitLoadTime:Date.now()/1000});
  try{
    if(navigator.connection&&navigator.connection.rtt===0)
      Object.defineProperty(navigator.connection,'rtt',{get:()=>50,configurable:true});
  }catch(e){}
})();
"""

# ─── Offer detection JS — 9 signal types ─────────────────────────────────────
OFFER_JS = r"""
(function(){
  var R={found:false,conf:0,signals:[],banner:'',url:window.location.href};
  function sig(s,c,x){R.signals.push(s+(x?': '+x:''));R.conf=Math.min(100,R.conf+c);if(c>=20)R.found=true;}

  // 1. Selector scan
  var sels=['button[data-uia="free-trial-banner"]','[data-uia="free-trial-banner"]',
    '[data-uia*="trial"]','[data-uia*="offer"]','[data-uia*="promo"]',
    'button[aria-label*="free"]','button[aria-label*="30 days"]',
    '[class*="TrialBanner"]','[class*="trialBanner"]','[class*="FreeTrial"]',
    '[class*="freeTrial"]','[class*="OfferBanner"]','[class*="PromoBar"]'];
  for(var s of sels){
    var el=document.querySelector(s);
    if(el&&el.offsetParent!==null){
      sig('selector',30,s);
      if(!R.banner)R.banner=(el.innerText||el.textContent||'').substring(0,200);
    }
  }

  // 2. Text scan
  var texts=['30 days free','₹0','free trial','Try 30 days','start your free',
    '1 month free','7 days free','no commitment','Get 1 month free','first month free',
    'try free','Watch free','days free'];
  var body=document.body?document.body.innerText.toLowerCase():'';
  var hits=0;
  for(var t of texts){if(body.includes(t.toLowerCase())&&++hits<=2)sig('text',15,t);}

  // 3. Button scan
  var btns=document.querySelectorAll('button,a[role="button"],[data-uia]');
  for(var b of btns){
    var txt=(b.innerText||b.textContent||'').trim().toLowerCase();
    if(txt.includes('30 days')||txt.includes('₹0')||txt.includes('free trial')
       ||txt.includes('try free')||txt.includes('1 month free')){
      sig('button',25,b.textContent.trim().substring(0,80));
      if(!R.banner)R.banner=b.textContent.trim().substring(0,200);
      break;
    }
  }

  // 4. React context
  try{
    if(window.netflix&&window.netflix.reactContext){
      var ctx=JSON.stringify(window.netflix.reactContext).toLowerCase();
      if(ctx.includes('trial')||ctx.includes('freetrial'))sig('react_ctx',35,'offer in reactContext');
    }
  }catch(e){}

  // 5. Preloaded state
  try{
    var ps=window.__PRELOADED_STATE__||window.__NEXT_DATA__;
    if(ps){var p=JSON.stringify(ps).toLowerCase();
      if(p.includes('trial')||p.includes('offer'))sig('preload',30,'offer in preloaded state');}
  }catch(e){}

  // 6. Network requests
  try{
    var ents=performance.getEntriesByType('resource');
    for(var e of ents){
      if(e.name.includes('MembershipOffers')||e.name.includes('freetrial')||e.name.includes('promotions')){
        sig('network',20,e.name.substring(0,100));break;
      }
    }
  }catch(ex){}

  return R;
})()
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════
def log(msg, level="ℹ️"):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {level} {msg}", flush=True)

def find_chrome():
    paths = CHROME_PATHS_LINUX if sys.platform != "win32" else CHROME_PATHS_WIN
    for p in paths:
        if os.path.exists(p):
            return p
    # Last resort: which
    try:
        r = subprocess.run(["which", "google-chrome"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        r = subprocess.run(["which", "chromium-browser"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    raise FileNotFoundError("Chrome/Chromium not found! Check Dockerfile.")

def kill_port(port):
    try:
        if sys.platform == "win32":
            r = subprocess.run(f'netstat -aon | findstr :{port} | findstr LISTENING',
                shell=True, capture_output=True, text=True, timeout=10)
            for line in r.stdout.strip().split('\n'):
                parts = line.split()
                if parts and parts[-1].isdigit():
                    subprocess.run(f'taskkill /F /PID {parts[-1]}', shell=True, capture_output=True)
        else:
            subprocess.run(f"fuser -k {port}/tcp", shell=True, capture_output=True, timeout=5)
    except Exception:
        pass

def get_ws_url(port, retries=35):
    for _ in range(retries):
        try:
            tabs = requests.get(f"http://127.0.0.1:{port}/json", timeout=4).json()
            for t in tabs:
                if t.get("type") == "page":
                    return t["webSocketDebuggerUrl"]
            if tabs:
                return tabs[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.6)
    raise RuntimeError(f"Chrome on :{port} not responding")

def make_consent_cookie():
    ts_ms = int(time.time() * 1000)
    cid   = str(uuid.uuid4())
    val = (
        f"landingPath=https%3A%2F%2Fwww.netflix.com%2Fin%2F"
        f"&datestamp={datetime.utcnow().strftime('%a+%b+%d+%Y+%H%%3A%M%%3A%S+GMT%%2B0000')}"
        f"&version=202604.2.0&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1"
        f"&hosts=&fclco=&consentId={cid}&interactionCount=1&isAnonUser=1"
        f"&prevHadToken=0&intType=1&crTime={ts_ms}&isGpcEnabled=0"
        f"&browserGpcFlag=0&isDntEnabled=0&isIABGlobal=false&AwaitingReconsent=false"
        f"&{GEO_COOKIE}"
    )
    return {"name":"OptanonConsent","value":val,"domain":".netflix.com",
            "path":"/","secure":False,"httpOnly":False}

def make_nfvdid():
    import base64
    val = base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")[:78]
    return {"name":"nfvdid","value":f"BQFmAAEBE{val}%3D%3D",
            "domain":".netflix.com","path":"/","secure":False,"httpOnly":False}

def chrome_args(port, profile, ua):
    """Build Chrome args — headless on Linux, windowed on Windows."""
    args = [
        find_chrome(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        f"--user-agent={ua}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--window-size=1280,900",
        "about:blank",
    ]
    if sys.platform != "win32":  # Railway / Linux
        args += [
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-setuid-sandbox",
            "--disable-software-rasterizer",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-sync",
            "--metrics-recording-only",
            "--mute-audio",
            "--no-zygote",          # avoids sandbox issues in containers
            "--single-process",     # Railway has limited processes
        ]
    else:
        args += ["--incognito"]
    return args


# ══════════════════════════════════════════════════════════════════════════════
#  CDP Client
# ══════════════════════════════════════════════════════════════════════════════
class CDP:
    def __init__(self, ws_url):
        self.ws_url=ws_url; self.ws=None; self._id=0
        self._pending={}; self._dead=False

    async def connect(self):
        self.ws = await websockets.connect(
            self.ws_url, max_size=100*1024*1024,
            ping_interval=None, ping_timeout=None, open_timeout=30)
        self._dead = False
        asyncio.get_running_loop().create_task(self._recv_loop())

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                try: msg = json.loads(raw)
                except: continue
                mid = msg.get("id")
                if mid and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done(): fut.set_result(msg)
        except (websockets.exceptions.ConnectionClosed,
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
                EOFError, OSError): pass
        finally:
            self._dead = True
            for fut in self._pending.values():
                if not fut.done():
                    try: fut.set_exception(RuntimeError("ws dead"))
                    except: pass
            self._pending.clear()

    async def send(self, method, params=None, timeout=30):
        if self._dead: return {}
        self._id += 1; mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try: await self.ws.send(json.dumps({"id":mid,"method":method,"params":params or{}}))
        except: self._pending.pop(mid,None); return {}
        try:
            r = await asyncio.wait_for(fut, timeout=timeout)
            return r.get("result", {})
        except (asyncio.TimeoutError, RuntimeError):
            self._pending.pop(mid, None); return {}

    async def eval(self, expr, timeout=15):
        r = await self.send("Runtime.evaluate",
            {"expression":expr,"returnByValue":True,"awaitPromise":False}, timeout=timeout)
        return (r.get("result") or {}).get("value")

    async def get_all_cookies(self):
        r = await self.send("Network.getAllCookies")
        return r.get("cookies", [])

    async def goto(self, url):
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(2)

    async def close(self):
        if self.ws:
            try: await self.ws.close()
            except: pass




# TG helpers
def tg_send(text, parse_mode="HTML"):
    if not TG_TOKEN or not TG_CHAT_ID: return 0
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=15
        )
        return r.json().get("result", {}).get("message_id", 0)
    except Exception as e:
        log(f"TG send failed: {e}", "WARNING")
        return 0

def tg_edit(mid, text, parse_mode="HTML"):
    if not TG_TOKEN or not TG_CHAT_ID or not mid: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText",
            json={"chat_id": TG_CHAT_ID, "message_id": mid,
                  "text": text, "parse_mode": parse_mode},
            timeout=15
        )
    except Exception as e:
        log(f"TG edit failed: {e}", "WARNING")

def tg_send_file(path, caption):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"document": (os.path.basename(path), f, "application/json")},
                timeout=20
            )
    except Exception as e:
        log(f"TG file send failed: {e}", "WARNING")

def tg_get_updates(offset=0):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"timeout": 30, "offset": offset},
            timeout=35
        )
        return r.json().get("result", [])
    except Exception:
        return []


# Bot State
class BotState:
    def __init__(self):
        self.running     = True
        self.attempt     = 0
        self.found       = 0
        self.start_ts    = datetime.now()
        self.last_conf   = 0
        self.last_signal = ""
        self.counter_mid = 0   # message_id of live counter message

STATE = BotState()


def build_counter_text():
    elapsed = int((datetime.now() - STATE.start_ts).total_seconds())
    h = elapsed // 3600
    m = (elapsed % 3600) // 60
    s = elapsed % 60
    rate   = STATE.attempt / max(1, elapsed / 60)
    status = "HUNTING" if STATE.running else "PAUSED"

    conf   = STATE.last_conf or 0
    filled = int(conf / 5)
    bar    = "#" * filled + "." * (20 - filled)

    sig = (STATE.last_signal[:45] if STATE.last_signal else "scanning...").replace("<","&lt;")

    return (
        "<b>Netflix Offer Hunter</b>\n"
        "------------------------------\n"
        f"Status:   {status}\n"
        f"Attempts: <code>{STATE.attempt}</code>\n"
        f"Found:    <code>{STATE.found}</code>\n"
        f"Speed:    <code>{rate:.1f}/min</code>\n"
        f"Uptime:   <code>{h}h {m}m {s}s</code>\n"
        "------------------------------\n"
        f"Last conf: <code>{conf}%</code>\n"
        f"[{bar}]\n"
        f"<code>{sig}</code>\n"
        "------------------------------\n"
        "<i>/stop  /start  /help</i>"
    )


def tg_poll_commands():
    import threading
    def _poll():
        offset = 0
        while True:
            try:
                updates = tg_get_updates(offset)
                for upd in updates:
                    offset = upd["update_id"] + 1
                    msg     = upd.get("message") or upd.get("channel_post") or {}
                    text    = (msg.get("text") or "").strip().lower()
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    if chat_id != str(TG_CHAT_ID):
                        continue
                    if text in ["/start", "/resume"]:
                        STATE.running = True
                        tg_edit(STATE.counter_mid, build_counter_text())
                        tg_send("Hunt RESUMED")
                    elif text in ["/stop", "/pause"]:
                        STATE.running = False
                        tg_edit(STATE.counter_mid, build_counter_text())
                        tg_send("Hunt PAUSED — /start to resume")
                    elif text == "/status":
                        tg_edit(STATE.counter_mid, build_counter_text())
                    elif text == "/help":
                        tg_send(
                            "<b>Netflix Offer Hunter</b>\n\n"
                            "/start  — resume hunt\n"
                            "/stop   — pause hunt\n"
                            "/status — refresh counter\n"
                            "/help   — this message"
                        )
            except Exception as e:
                log(f"TG poll error: {e}", "WARNING")
            time.sleep(2)
    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return t


# One Hunt Attempt
async def one_hunt(attempt, port):
    profile = f"/tmp/_nf_hunt_{uuid.uuid4().hex[:8]}"
    if sys.platform == "win32":
        profile = str(Path(__file__).parent / f"_hunt_{uuid.uuid4().hex[:6]}")
    proc = None
    ua = random.choice(USER_AGENTS)
    try:
        kill_port(port)
        os.makedirs(profile, exist_ok=True)
        proc = subprocess.Popen(
            chrome_args(port, profile, ua),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(4 if sys.platform != "win32" else 3)
        ws  = get_ws_url(port)
        cdp = CDP(ws)
        await cdp.connect()
        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
        await cdp.send("Network.enable")
        await cdp.send("Page.enable")
        for c in [make_nfvdid(), make_consent_cookie(),
                  {"name":"netflix-sans-normal-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
                  {"name":"netflix-sans-bold-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False}]:
            await cdp.send("Network.setCookie", {"name":c["name"],"value":c["value"],
                "domain":c["domain"],"path":c["path"],"secure":c["secure"],"httpOnly":c["httpOnly"]})
        await cdp.goto(NETFLIX_URL)
        log(f"[{attempt}] Navigated", "INFO")
        await asyncio.sleep(PAGE_WAIT)
        detection = None
        for scan in range(3):
            await asyncio.sleep(OFFER_WAIT // 3)
            detection = await cdp.eval(OFFER_JS)
            conf = detection.get("conf", 0) if detection else 0
            log(f"[{attempt}] Scan {scan+1}/3 conf={conf}", "INFO")
            if detection and detection.get("found"):
                break
        if not detection or not detection.get("found") or detection.get("conf", 0) < 20:
            await cdp.close()
            return {"found": False, "attempt": attempt,
                    "conf": detection.get("conf", 0) if detection else 0,
                    "signals": detection.get("signals", []) if detection else []}
        log(f"[{attempt}] OFFER FOUND conf={detection.get('conf')}", "INFO")
        all_cookies = await cdp.get_all_cookies()
        nf_cookies  = [c for c in all_cookies if "netflix.com" in c.get("domain","")]
        await cdp.close()
        return {"found": True, "attempt": attempt,
                "conf": detection.get("conf"), "signals": detection.get("signals",[]),
                "banner": detection.get("banner",""),
                "cookies": nf_cookies, "all_cookies": all_cookies}
    except Exception as e:
        log(f"[{attempt}] Error: {e}", "ERROR")
        return {"found": False, "attempt": attempt, "error": str(e), "conf": 0, "signals": []}
    finally:
        kill_port(port)
        if proc:
            try: proc.terminate(); proc.wait(timeout=6)
            except: pass
        shutil.rmtree(profile, ignore_errors=True)


# Deliver cookies via Telegram
def deliver_cookies(result, attempt):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    nf_path = f"/tmp/nf_offer_{ts}.json" if sys.platform != "win32" \
              else str(Path(__file__).parent / f"nf_offer_{ts}.json")
    with open(nf_path, "w") as f:
        json.dump(result.get("cookies", []), f, indent=2)
    best_path = Path(__file__).parent / "best_offer.json"
    with open(best_path, "w") as f:
        json.dump({"meta": {"found_at": ts, "attempt": attempt,
                             "conf": result.get("conf"),
                             "signals": result.get("signals",[]),
                             "banner": result.get("banner",""),
                             "url": NETFLIX_URL, "region": REGION},
                   "netflix_cookies": result.get("cookies",[])}, f, indent=2)
    cookie_names = [c.get("name","?") for c in result.get("cookies",[])]
    tg_send(
        f"OFFER COOKIE FOUND!\n\n"
        f"Time: {ts}\n"
        f"Confidence: {result.get('conf')}%\n"
        f"Region: India\n"
        f"Signals: {', '.join(result.get('signals',[])[:4])}\n"
        f"Cookies ({len(result.get('cookies',[]))} total):\n"
        f"{', '.join(cookie_names)}\n\n"
        "Full cookies JSON attached below"
    )
    tg_send_file(nf_path, f"Netflix offer cookies — attempt #{attempt}")
    log(f"Cookies delivered! File: {nf_path}", "INFO")
    return nf_path


# Main loop
async def main():
    log("Netflix Offer Hunter v4.0 starting...", "INFO")
    try:
        chrome = find_chrome()
        log(f"Chrome found: {chrome}", "INFO")
    except FileNotFoundError as e:
        log(str(e), "ERROR"); sys.exit(1)

    tg_poll_commands()
    log("TG command listener started", "INFO")

    # Send startup message
    tg_send(
        "<b>Netflix Offer Hunter STARTED!</b>\n\n"
        f"Region: India (IN)\n"
        f"URL: {NETFLIX_URL}\n"
        f"Delay: {DELAY}s\n\n"
        "Counter message below — it will update every attempt.\n"
        "Send cookies the moment offer is found."
    )

    # Spawn the live counter message — this is the ONLY ongoing message
    STATE.counter_mid = tg_send(build_counter_text())
    log(f"Counter message id: {STATE.counter_mid}", "INFO")

    port    = BASE_PORT
    attempt = 0

    while True:
        if not STATE.running:
            log("Paused...", "INFO")
            await asyncio.sleep(5)
            continue

        attempt += 1
        STATE.attempt = attempt
        port = BASE_PORT + (attempt % 150)

        log(f"Attempt #{attempt}", "INFO")
        result = await one_hunt(attempt, port)
        found  = result.get("found", False)

        # Update live state
        STATE.last_conf   = result.get("conf", 0) or 0
        sigs = result.get("signals", [])
        STATE.last_signal = sigs[0] if sigs else ""

        # Edit the ONE counter message — no new messages
        tg_edit(STATE.counter_mid, build_counter_text())

        if found:
            STATE.found += 1
            log(f"OFFER FOUND #{attempt}", "INFO")
            # Deliver sends a NEW message + file — that's intentional (it's the prize!)
            deliver_cookies(result, attempt)

        d = DELAY + random.uniform(0, 1.5)
        log(f"Sleeping {d:.1f}s...", "INFO")
        await asyncio.sleep(d)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Stopped.", "INFO")
        tg_send("Hunter stopped.")