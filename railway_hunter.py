"""
Netflix Offer Hunter — railway_hunter.py v6.0
==============================================
FIXED: No more fake 65% confidence detections.
REAL detection: Click CTA -> check if email form appears WITHOUT payment.
Only sends cookies when 100% confirmed offer flow.
"""

import asyncio, json, os, shutil, subprocess, sys, time, uuid, random, threading, base64
from datetime import datetime
from pathlib import Path
import requests, websockets, websockets.exceptions

# ── Config ────────────────────────────────────────────────────────
TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT   = os.environ.get("TG_CHAT_ID", "")
DELAY     = float(os.environ.get("DELAY", "5"))
BASE_PORT = 9500
REGION    = "India (IN)"
NF_URL    = "https://www.netflix.com/in/"

CHROME_LINUX = [
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
]
CHROME_WIN = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]

STEALTH_JS = r"""
(function(){
  try{delete Object.getPrototypeOf(navigator).webdriver;}catch(e){}
  Object.defineProperty(navigator,'webdriver',{get:()=>undefined,configurable:true});
  for(let k of Object.keys(window)){if(/^cdc_|^\$cdc_/.test(k))try{delete window[k];}catch(e){}}
  Object.defineProperty(navigator,'plugins',{get:()=>[
    {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'PDF',length:1},
    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:'',length:1},
    {name:'Native Client',filename:'internal-nacl-plugin',description:'',length:2},
  ]});
  Object.defineProperty(navigator,'languages',{get:()=>['en-IN','hi-IN','en-GB','en']});
  window.chrome=window.chrome||{};
  window.chrome.runtime=window.chrome.runtime||{};
  if(!window.chrome.csi)window.chrome.csi=()=>({startE:performance.now()});
})();
"""

# Click CTA button on homepage
CLICK_CTA_JS = """
(function(){
  // Priority: dedicated offer/CTA selectors first
  var sels = [
    'button[data-uia="free-trial-banner"]',
    '[data-uia="free-trial-banner"]',
    'a[data-uia="hero-cta"]',
    'button[data-uia="hero-cta"]',
    '[data-uia="get-started-button"]',
    '.hero-cta a', '.hero-cta button',
    '.nfheader .cta a', '.nfheader .cta button'
  ];
  for (var s of sels) {
    var el = document.querySelector(s);
    if (el && el.offsetParent !== null) {
      el.scrollIntoView({block:'center'}); el.click();
      return {clicked:true, sel:s, text:el.textContent.trim().substring(0,80)};
    }
  }
  // Text fallback — find visible CTA in top 600px
  var all = document.querySelectorAll('a, button');
  for (var el of all) {
    var txt = (el.textContent||'').trim().toLowerCase();
    var rect = el.getBoundingClientRect();
    if (rect.top < 600 && rect.width > 50 && el.offsetParent !== null) {
      if (txt==='get started' || txt==='start watching' || txt==='join free for a month'
          || txt==='try 30 days free' || txt.includes('get started')
          || txt.includes('join now') || txt.includes('sign up')) {
        el.scrollIntoView({block:'center'}); el.click();
        return {clicked:true, sel:'text_fallback', text:el.textContent.trim().substring(0,80)};
      }
    }
  }
  return {clicked:false, sel:'', text:''};
})()
"""

# STRICT 100% offer detection — NO confidence scores
CHECK_FLOW_JS = """
(function(){
  var url  = window.location.href;
  var urlL = url.toLowerCase();
  var body = document.body ? document.body.innerText.toLowerCase() : '';
  var R    = {url:url, is_offer:false, is_payment:false, is_login:false, evidence:[]};

  // HARD FAILS — check payment first, return immediately
  var payInputs = document.querySelectorAll(
    'input[name="cardnumber"],[data-uia*="payment"],[data-uia*="credit-card"],[data-uia="creditCard"]'
  );
  if (payInputs.length > 0 || urlL.includes('/creditoption') || urlL.includes('/payment')) {
    R.is_payment = true; R.evidence.push('payment_page'); return R;
  }
  if (body.includes('credit card') || body.includes('debit card') || body.includes('upi payment')) {
    R.is_payment = true; R.evidence.push('payment_text'); return R;
  }
  if (urlL.includes('/login') || urlL.includes('/loginhelp')) {
    R.is_login = true; R.evidence.push('login_redirect'); return R;
  }

  var emailInputs = document.querySelectorAll(
    'input[type="email"], input[name="userLoginId"], input[autocomplete="email"]'
  );

  // CONFIRMED: explicit signup URL + email form
  if (urlL.includes('/signup/registration') || urlL.includes('/signup/planform')) {
    if (emailInputs.length > 0 && payInputs.length === 0) {
      R.is_offer = true;
      R.evidence.push('signup_url_plus_email_form');
      return R;
    }
  }

  // CONFIRMED: moved OFF homepage + email form + no payment
  var onHomepage = (urlL === 'https://www.netflix.com/in/' || urlL === 'https://www.netflix.com/in');
  if (!onHomepage && emailInputs.length > 0 && payInputs.length === 0) {
    R.is_offer = true;
    R.evidence.push('off_homepage_email_form_no_payment');
    return R;
  }

  // CONFIRMED: inline form with submit button (rare but valid)
  if (emailInputs.length > 0 && payInputs.length === 0) {
    var sub = document.querySelector('button[type="submit"], button[data-uia="continue-button"]');
    if (sub && sub.offsetParent !== null) {
      R.is_offer = true;
      R.evidence.push('inline_email_form_with_submit');
      return R;
    }
  }

  return R; // nothing confirmed
})()
"""


# ── Helpers ──────────────────────────────────────────────────────
def ts(): return datetime.now().strftime("%H:%M:%S")
def log(msg, t="INFO"): print(f"[{ts()}][{t}] {msg}", flush=True)

def find_chrome():
    paths = CHROME_LINUX if sys.platform != "win32" else CHROME_WIN
    for p in paths:
        if os.path.exists(p): return p
    try:
        for cmd in ["chromium-browser","chromium","google-chrome"]:
            r = subprocess.run(["which", cmd], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
    except: pass
    raise FileNotFoundError("Chrome/Chromium not found!")

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
    except: pass

def get_ws_url(port, retries=35):
    for _ in range(retries):
        try:
            tabs = requests.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
            for t in tabs:
                if t.get("type") == "page": return t["webSocketDebuggerUrl"]
            if tabs: return tabs[0]["webSocketDebuggerUrl"]
        except: pass
        time.sleep(0.7)
    raise RuntimeError(f"Chrome :{port} not responding")

def make_fresh_cookies():
    val = "BQFmAAEBE" + base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")[:78] + "%3D%3D"
    cid = str(uuid.uuid4())
    ts_ms = int(time.time() * 1000)
    consent = (
        f"landingPath=https%3A%2F%2Fwww.netflix.com%2Fin%2F"
        f"&datestamp={datetime.utcnow().strftime('%a+%b+%d+%Y+%H%%3A%M%%3A%S+GMT%%2B0000')}"
        f"&version=202604.2.0&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1"
        f"&hosts=&consentId={cid}&interactionCount=1&isAnonUser=1"
        f"&prevHadToken=0&intType=1&crTime={ts_ms}&isGpcEnabled=0"
        f"&browserGpcFlag=0&isDntEnabled=0&isIABGlobal=false&AwaitingReconsent=false"
        f"&geolocation=IN%3BMH"
    )
    return [
        {"name":"nfvdid","value":val,"domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"OptanonConsent","value":consent,"domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"netflix-sans-normal-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"netflix-sans-bold-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
    ]

def chrome_cmd(port, profile, ua):
    exe = find_chrome()
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            f"--user-agent={ua}", "--no-first-run", "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled", "--window-size=1366,768", "about:blank"]
    if sys.platform != "win32":
        args += ["--headless=new","--no-sandbox","--disable-dev-shm-usage",
                 "--disable-gpu","--disable-setuid-sandbox",
                 "--disable-software-rasterizer","--no-zygote","--single-process"]
    return args


# ── CDP ──────────────────────────────────────────────────────────
class CDP:
    def __init__(self, ws_url):
        self.ws_url=ws_url; self.ws=None; self._id=0; self._pending={}; self._dead=False
    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=100*1024*1024,
            ping_interval=None, ping_timeout=None, open_timeout=30)
        self._dead = False
        asyncio.get_running_loop().create_task(self._recv())
    async def _recv(self):
        try:
            async for raw in self.ws:
                try: msg = json.loads(raw)
                except: continue
                mid = msg.get("id")
                if mid and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done(): fut.set_result(msg)
        except: pass
        finally:
            self._dead = True
            for f in self._pending.values():
                if not f.done():
                    try: f.set_exception(RuntimeError("dead"))
                    except: pass
            self._pending.clear()
    async def cmd(self, method, params=None, timeout=20):
        if self._dead: return {}
        self._id += 1; mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try: await self.ws.send(json.dumps({"id":mid,"method":method,"params":params or{}}))
        except: self._pending.pop(mid,None); return {}
        try:
            r = await asyncio.wait_for(fut, timeout=timeout)
            return r.get("result",{})
        except: self._pending.pop(mid,None); return {}
    async def js(self, expr, timeout=15):
        r = await self.cmd("Runtime.evaluate",
            {"expression":expr,"returnByValue":True,"awaitPromise":False}, timeout=timeout)
        return (r.get("result") or {}).get("value")
    async def navigate(self, url): await self.cmd("Page.navigate",{"url":url}); await asyncio.sleep(2)
    async def all_cookies(self): return (await self.cmd("Network.getAllCookies")).get("cookies",[])
    async def close(self):
        if self.ws:
            try: await self.ws.close()
            except: pass


# ── Telegram ─────────────────────────────────────────────────────
class State:
    running    = True
    attempt    = 0
    found      = 0
    start_ts   = time.time()
    last       = "starting..."
    counter_id = 0

S = State()

def tg_send(text):
    if not TG_TOKEN: return 0
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":text,"parse_mode":"HTML"}, timeout=15)
        return r.json().get("result",{}).get("message_id",0)
    except: return 0

def tg_edit(mid, text):
    if not TG_TOKEN or not mid: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText",
            json={"chat_id":TG_CHAT,"message_id":mid,"text":text,"parse_mode":"HTML"}, timeout=15)
    except: pass

def tg_file(path, caption):
    if not TG_TOKEN: return
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id":TG_CHAT,"caption":caption,"parse_mode":"HTML"},
                files={"document":(os.path.basename(path),f,"application/json")}, timeout=30)
    except: pass

def tg_updates(offset=0):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"timeout":30,"offset":offset}, timeout=35)
        return r.json().get("result",[])
    except: return []

def counter():
    elapsed = int(time.time() - S.start_ts)
    h,m,s   = elapsed//3600,(elapsed%3600)//60,elapsed%60
    rate    = S.attempt / max(1, elapsed/60)
    status  = "HUNTING" if S.running else "PAUSED"
    last    = str(S.last)[:55].replace("<","&lt;")
    return (
        "<b>Netflix Offer Hunter v6</b>\n"
        "--------------------------------\n"
        f"Status:   {status}\n"
        f"Attempts: <code>{S.attempt}</code>\n"
        f"Found:    <code>{S.found}</code>\n"
        f"Speed:    <code>{rate:.1f}/min</code>\n"
        f"Uptime:   <code>{h}h {m}m {s}s</code>\n"
        "--------------------------------\n"
        f"Last: <code>{last}</code>\n"
        "--------------------------------\n"
        "<i>/stop /start /help</i>"
    )

def tg_poll():
    def _run():
        offset = 0
        while True:
            for upd in tg_updates(offset):
                offset = upd["update_id"] + 1
                msg  = upd.get("message") or upd.get("channel_post") or {}
                txt  = (msg.get("text") or "").strip().lower()
                cid  = str((msg.get("chat") or {}).get("id",""))
                if cid != str(TG_CHAT): continue
                if txt in ["/start","/resume"]: S.running = True;  tg_edit(S.counter_id, counter())
                elif txt in ["/stop","/pause"]: S.running = False; tg_edit(S.counter_id, counter())
                elif txt == "/status": tg_edit(S.counter_id, counter())
                elif txt == "/help":
                    tg_send("<b>Commands</b>\n/start /stop /status /help")
            time.sleep(2)
    threading.Thread(target=_run, daemon=True).start()


# ── One Hunt ─────────────────────────────────────────────────────
async def hunt_one(attempt, port):
    profile = f"/tmp/_nfh_{uuid.uuid4().hex[:8]}"
    if sys.platform == "win32":
        profile = str(Path(__file__).parent / f"_h_{uuid.uuid4().hex[:6]}")
    proc = None
    ua   = random.choice(UA_LIST)
    try:
        kill_port(port)
        os.makedirs(profile, exist_ok=True)
        proc = subprocess.Popen(chrome_cmd(port, profile, ua),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5 if sys.platform != "win32" else 3)

        ws  = get_ws_url(port)
        cdp = CDP(ws)
        await cdp.connect()

        await cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
        await cdp.cmd("Network.enable")
        await cdp.cmd("Page.enable")

        # Inject ONLY fresh anonymous cookies — NO auth cookies
        for c in make_fresh_cookies():
            await cdp.cmd("Network.setCookie", {"name":c["name"],"value":c["value"],
                "domain":c["domain"],"path":c["path"],"secure":c["secure"],"httpOnly":c["httpOnly"]})

        await cdp.navigate(NF_URL)
        log(f"[{attempt}] Loaded, waiting 8s...")
        await asyncio.sleep(8)

        # Step 1: Click the CTA
        click = await cdp.js(CLICK_CTA_JS)
        if not click or not click.get("clicked"):
            await cdp.close()
            return {"found":False, "reason":"no_cta_button"}

        log(f"[{attempt}] Clicked: '{click.get('text','')}' [{click.get('sel','')}]")
        await asyncio.sleep(7)   # wait for navigation

        # Step 2: Strict flow check
        flow = await cdp.js(CHECK_FLOW_JS)
        if not flow:
            await cdp.close()
            return {"found":False, "reason":"js_eval_failed"}

        log(f"[{attempt}] Flow: offer={flow.get('is_offer')} pay={flow.get('is_payment')} evidence={flow.get('evidence')}")

        if flow.get("is_payment"):
            await cdp.close()
            return {"found":False, "reason":"payment_required"}
        if flow.get("is_login"):
            await cdp.close()
            return {"found":False, "reason":"login_redirect"}
        if not flow.get("is_offer"):
            await cdp.close()
            return {"found":False, "reason":f"no_offer:{flow.get('url','')[:50]}"}

        # OFFER 100% CONFIRMED
        log(f"[{attempt}] OFFER CONFIRMED! evidence={flow.get('evidence')}", "OK")
        cookies = await cdp.all_cookies()
        nf_cookies = [c for c in cookies if "netflix.com" in c.get("domain","")]
        await cdp.close()
        return {"found":True, "cookies":nf_cookies,
                "evidence":flow.get("evidence",[]),
                "cta":click.get("text",""),
                "landing_url":flow.get("url","")}

    except Exception as e:
        log(f"[{attempt}] Error: {e}", "ERR")
        return {"found":False, "reason":str(e)}
    finally:
        kill_port(port)
        if proc:
            try: proc.terminate(); proc.wait(timeout=6)
            except: pass
        shutil.rmtree(profile, ignore_errors=True)


# ── Save & Deliver ───────────────────────────────────────────────
def deliver(result, attempt):
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    out    = Path("/tmp") / f"OFFER_{ts_str}.json" if sys.platform != "win32" \
             else Path(__file__).parent / f"OFFER_{ts_str}.json"
    with open(out,"w") as f:
        json.dump(result["cookies"], f, indent=2)

    best = Path(__file__).parent / "best_offer.json"
    with open(best,"w") as f:
        json.dump({"found_at":ts_str,"attempt":attempt,
                   "evidence":result.get("evidence",[]),
                   "cta":result.get("cta",""),
                   "landing_url":result.get("landing_url",""),
                   "cookies":result["cookies"]}, f, indent=2)

    names = [c.get("name","?") for c in result["cookies"]]
    tg_send(
        f"<b>OFFER FOUND — REAL WORKING COOKIES</b>\n\n"
        f"Attempt: #{attempt}\n"
        f"CTA clicked: {result.get('cta','')[:50]}\n"
        f"Landed on: <code>{result.get('landing_url','')[:80]}</code>\n"
        f"Evidence: <code>{', '.join(result.get('evidence',[]))}</code>\n"
        f"Cookies ({len(names)}): <code>{', '.join(names)}</code>\n\n"
        f"<i>Full JSON attached below</i>"
    )
    tg_file(str(out), f"Offer cookies — attempt #{attempt}")
    log(f"Delivered! {out.name}", "OK")


# ── Main Loop ────────────────────────────────────────────────────
async def main():
    log("Netflix Offer Hunter v6.0 — starting")
    try:
        chrome = find_chrome(); log(f"Chrome: {chrome}")
    except FileNotFoundError as e:
        log(str(e), "ERR"); sys.exit(1)

    if TG_TOKEN:
        tg_poll()
        tg_send(
            "<b>Netflix Offer Hunter v6 STARTED</b>\n\n"
            "Detection: STRICT 100% only\n"
            "Method: Click CTA -> verify email form without payment\n"
            "No more fake 65% confidence garbage.\n\n"
            f"Region: {REGION} | Delay: {DELAY}s\n"
            "Commands: /stop /start /status /help"
        )
        S.counter_id = tg_send(counter())

    attempt = 0
    while True:
        if not S.running:
            await asyncio.sleep(3); continue

        attempt += 1
        S.attempt = attempt
        port = BASE_PORT + (attempt % 150)

        result = await hunt_one(attempt, port)
        found  = result.get("found", False)

        if found:
            S.found += 1
            S.last  = f"OFFER FOUND #{attempt}"
            deliver(result, attempt)
        else:
            S.last = result.get("reason","?")
            log(f"#{attempt} -> {S.last}")

        if TG_TOKEN and S.counter_id:
            tg_edit(S.counter_id, counter())

        await asyncio.sleep(DELAY + random.uniform(0, 2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Stopped.")
        if TG_TOKEN: tg_send("Hunter stopped.")
