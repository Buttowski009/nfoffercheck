"""
NETFLIX REAL OFFER HUNTER v5.0
================================
THE ACTUAL PROBLEM WITH OLD SYSTEM:
  - Old code detected "free trial" TEXT on homepage = ALWAYS PRESENT (marketing copy)
  - Those cookies were from a normal anonymous session = NO offer state
  - Dumping cookies from a session that just loaded homepage = useless

THE REAL LOGIC:
  Netflix offer works like this:
  1. You go to netflix.com/in/ with fresh anonymous session (Indian IP)
  2. IF Netflix decides to show you the offer:
     - A SPECIFIC banner/button appears (data-uia="free-trial-banner" OR specific CTA)
     - Clicking it takes you to /signup/registration or /signup/planform WITHOUT payment step
     - The URL changes + an email form appears WITHOUT payment required
  3. IF no offer: clicking any CTA asks for payment immediately

  SO THE REAL TEST IS:
  - Load page fresh
  - Click the MAIN CTA (Get Started / Start Watching etc)
  - See what URL / page you land on
  - If it goes to /signup/registration or shows email form ONLY = OFFER ACTIVE
  - If it asks for plan + payment = no offer, skip
  - Cookie capture = RIGHT BEFORE clicking CTA (session that has offer state)

  WHY LOCAL ONLY (not Railway):
  - Netflix India offer ONLY shows to Indian IPs
  - Railway servers = US IPs = no offer ever
  - Run this on your Windows machine with your Indian internet

USAGE:
  python real_hunter.py
  python real_hunter.py --delay 3 --tg-token TOKEN --tg-chat CHATID
"""

import asyncio, json, os, shutil, subprocess, sys, time, uuid, random, argparse, threading
from datetime import datetime
from pathlib import Path
import requests, websockets, websockets.exceptions

# ── Config ────────────────────────────────────────────────────────────────────
HERE       = Path(__file__).parent
NETFLIX_IN = "https://www.netflix.com/in/"
BASE_PORT  = 9600

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# ── Stealth JS ────────────────────────────────────────────────────────────────
STEALTH_JS = r"""
(function(){
  try{delete Object.getPrototypeOf(navigator).webdriver;}catch(e){}
  Object.defineProperty(navigator,'webdriver',{get:()=>undefined,configurable:true});
  for(let k of Object.keys(window)){if(/^cdc_|^\$cdc_/.test(k))try{delete window[k];}catch(e){}}
  for(let k of Object.keys(document)){if(/^cdc_|^\$cdc_/.test(k))try{delete document[k];}catch(e){}}
  Object.defineProperty(navigator,'plugins',{get:()=>[
    {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'PDF',length:1},
    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:'',length:1},
    {name:'Native Client',filename:'internal-nacl-plugin',description:'',length:2},
  ]});
  Object.defineProperty(navigator,'languages',{get:()=>['en-IN','hi-IN','en-GB','en']});
  window.chrome=window.chrome||{};
  window.chrome.runtime=window.chrome.runtime||{PlatformOs:{WIN:'win'}};
  if(!window.chrome.csi)window.chrome.csi=()=>({startE:performance.now()});
  if(!window.chrome.loadTimes)window.chrome.loadTimes=()=>({commitLoadTime:Date.now()/1000});
  try{
    if(navigator.connection&&navigator.connection.rtt===0)
      Object.defineProperty(navigator.connection,'rtt',{get:()=>50,configurable:true});
  }catch(e){}
})();
"""

# ── Find and click CTA button JS ──────────────────────────────────────────────
CLICK_CTA_JS = """
(function(){
  // Priority order: most specific offer selectors first
  var selectors = [
    // Dedicated offer banner (highest confidence)
    'button[data-uia="free-trial-banner"]',
    '[data-uia="free-trial-banner"]',
    // Hero CTA buttons
    'a[data-uia="hero-cta"]',
    'button[data-uia="hero-cta"]',
    '[data-uia="hero-cta"]',
    // Generic get-started
    'a[data-uia="get-started-button"]',
    '[data-uia="get-started-button"]',
    // Fallback: any visible CTA in the hero
    '.hero-cta a', '.hero-cta button',
    '.nfheader .cta a', '.nfheader .cta button',
  ];

  for(var sel of selectors){
    var el = document.querySelector(sel);
    if(el && el.offsetParent !== null){
      el.scrollIntoView({block:'center'});
      el.click();
      return {clicked: true, selector: sel, text: el.textContent.trim().substring(0,100)};
    }
  }

  // Last resort: find any button/link with CTA-like text that's in the header/hero area
  var allLinks = document.querySelectorAll('a, button');
  for(var el of allLinks){
    var txt = (el.textContent || '').trim().toLowerCase();
    var rect = el.getBoundingClientRect();
    // Must be visible and in top portion of page
    if(rect.top < 600 && rect.width > 50 && el.offsetParent !== null){
      if(txt.includes('get started') || txt.includes('start watching')
         || txt.includes('try now') || txt.includes('join now')
         || txt.includes('subscribe') || txt.includes('sign up')){
        el.scrollIntoView({block:'center'});
        el.click();
        return {clicked: true, selector: 'text_fallback', text: el.textContent.trim().substring(0,100)};
      }
    }
  }
  return {clicked: false, selector: '', text: ''};
})()
"""

# ── Check if current page is offer flow (no payment) ─────────────────────────
CHECK_OFFER_FLOW_JS = """
(function(){
  var url = window.location.href.toLowerCase();
  var bodyText = document.body ? document.body.innerText.toLowerCase() : '';
  var result = {
    url: window.location.href,
    is_offer: false,
    is_login: false,
    is_payment: false,
    is_email_form: false,
    evidence: []
  };

  // Offer flow indicators (email entry WITHOUT payment = free trial)
  if(url.includes('/signup/registration')){
    result.is_offer = true;
    result.evidence.push('url:signup/registration');
  }
  if(url.includes('/signup/planform') && !bodyText.includes('payment')){
    result.is_offer = true;
    result.evidence.push('url:signup/planform_no_payment');
  }

  // Email-only form present = offer flow
  var emailInputs = document.querySelectorAll('input[type="email"], input[name="userLoginId"], input[autocomplete="email"]');
  var paymentInputs = document.querySelectorAll('input[name="cardnumber"], input[data-uia="billing-card-number"], [data-uia*="payment"], [data-uia*="credit"]');
  if(emailInputs.length > 0 && paymentInputs.length === 0){
    result.is_email_form = true;
    result.evidence.push('email_form_no_payment');
    result.is_offer = true;
  }

  // Payment page = NOT an offer
  if(paymentInputs.length > 0 || url.includes('/signup/creditoption')
     || url.includes('/signup/payment') || bodyText.includes('credit card')
     || bodyText.includes('debit card') || bodyText.includes('upi payment')){
    result.is_payment = true;
    result.is_offer = false;
    result.evidence.push('payment_page_found');
  }

  // Login page = not new user flow
  if(url.includes('/login') || url.includes('/LoginHelp')){
    result.is_login = true;
    result.is_offer = false;
    result.evidence.push('login_page');
  }

  // Plan selection with free option = offer
  if(url.includes('/planform') || url.includes('/plan')){
    if(bodyText.includes('free') || bodyText.includes('rs 0') || bodyText.includes('rs. 0')){
      result.is_offer = true;
      result.evidence.push('plan_page_free_option');
    }
  }

  return result;
})()
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════
def ts(): return datetime.now().strftime("%H:%M:%S")
def log(msg, tag="INFO"):
    colors = {"OK":"\033[92m","ERR":"\033[91m","WARN":"\033[93m","HUNT":"\033[95m","SAVE":"\033[96m","INFO":"\033[0m"}
    c = colors.get(tag, "")
    W = "\033[0m"
    print(f"{c}[{ts()}][{tag}] {msg}{W}", flush=True)

def find_chrome():
    for p in CHROME_PATHS:
        if os.path.exists(p): return p
    raise FileNotFoundError("Chrome not found!")

def kill_port(port):
    try:
        r = subprocess.run(f'netstat -aon | findstr :{port} | findstr LISTENING',
            shell=True, capture_output=True, text=True, timeout=10)
        for line in r.stdout.strip().split('\n'):
            parts = line.split()
            if parts and parts[-1].isdigit():
                subprocess.run(f'taskkill /F /PID {parts[-1]}', shell=True, capture_output=True, timeout=5)
    except: pass

def get_ws_url(port, retries=30):
    for _ in range(retries):
        try:
            tabs = requests.get(f"http://localhost:{port}/json", timeout=3).json()
            for t in tabs:
                if t.get("type") == "page": return t["webSocketDebuggerUrl"]
            if tabs: return tabs[0]["webSocketDebuggerUrl"]
        except: pass
        time.sleep(0.6)
    raise RuntimeError(f"Chrome :{port} not responding")

def make_fresh_cookies():
    """Minimal cookies for a fresh Indian anonymous session."""
    import base64
    nfvdid_val = "BQFmAAEBE" + base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")[:78] + "%3D%3D"
    ts_ms = int(time.time() * 1000)
    cid = str(uuid.uuid4())
    consent_val = (
        f"landingPath=https%3A%2F%2Fwww.netflix.com%2Fin%2F"
        f"&datestamp={datetime.utcnow().strftime('%a+%b+%d+%Y+%H%%3A%M%%3A%S+GMT%%2B0000')}"
        f"&version=202604.2.0&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1"
        f"&hosts=&consentId={cid}&interactionCount=1&isAnonUser=1"
        f"&prevHadToken=0&intType=1&crTime={ts_ms}&isGpcEnabled=0"
        f"&browserGpcFlag=0&isDntEnabled=0&isIABGlobal=false&AwaitingReconsent=false"
        f"&geolocation=IN%3BMH"
    )
    return [
        {"name":"nfvdid","value":nfvdid_val,"domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"OptanonConsent","value":consent_val,"domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"netflix-sans-normal-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"netflix-sans-bold-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  CDP Client
# ══════════════════════════════════════════════════════════════════════════════
class CDP:
    def __init__(self, ws_url):
        self.ws_url=ws_url; self.ws=None; self._id=0; self._pending={}; self._dead=False

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=100*1024*1024,
            ping_interval=None, ping_timeout=None, open_timeout=25)
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

    async def url(self):
        return await self.js("window.location.href") or ""

    async def navigate(self, url):
        await self.cmd("Page.navigate", {"url": url})
        await asyncio.sleep(2)

    async def all_cookies(self):
        r = await self.cmd("Network.getAllCookies")
        return r.get("cookies", [])

    async def close(self):
        if self.ws:
            try: await self.ws.close()
            except: pass


# ══════════════════════════════════════════════════════════════════════════════
#  TG
# ══════════════════════════════════════════════════════════════════════════════
_TG_TOKEN   = ""
_TG_CHAT    = ""
_COUNTER_ID = 0
_STATE      = {"attempt":0,"found":0,"start":time.time(),"last_result":"starting...","running":True}

def tg_send(text):
    if not _TG_TOKEN: return 0
    try:
        r = requests.post(f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={"chat_id":_TG_CHAT,"text":text,"parse_mode":"HTML"}, timeout=15)
        return r.json().get("result",{}).get("message_id",0)
    except: return 0

def tg_edit(mid, text):
    if not _TG_TOKEN or not mid: return
    try:
        requests.post(f"https://api.telegram.org/bot{_TG_TOKEN}/editMessageText",
            json={"chat_id":_TG_CHAT,"message_id":mid,"text":text,"parse_mode":"HTML"}, timeout=15)
    except: pass

def tg_file(path, caption):
    if not _TG_TOKEN: return
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{_TG_TOKEN}/sendDocument",
                data={"chat_id":_TG_CHAT,"caption":caption,"parse_mode":"HTML"},
                files={"document":(os.path.basename(path),f,"application/json")}, timeout=20)
    except: pass

def counter_text():
    s = _STATE
    elapsed = int(time.time() - s["start"])
    h,m,sec = elapsed//3600,(elapsed%3600)//60,elapsed%60
    rate = s["attempt"]/max(1,elapsed/60)
    status = "HUNTING" if s["running"] else "PAUSED"
    return (
        "<b>Netflix Real Offer Hunter</b>\n"
        "--------------------------------\n"
        f"Status:   {status}\n"
        f"Attempts: <code>{s['attempt']}</code>\n"
        f"Found:    <code>{s['found']}</code>\n"
        f"Speed:    <code>{rate:.1f}/min</code>\n"
        f"Uptime:   <code>{h}h {m}m {sec}s</code>\n"
        "--------------------------------\n"
        f"Last: <code>{str(s['last_result'])[:60]}</code>\n"
        "--------------------------------\n"
        "<i>/stop /start /help</i>"
    )

def tg_poll(token, chat):
    def _run():
        global _COUNTER_ID
        offset = 0
        while True:
            try:
                r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"timeout":30,"offset":offset}, timeout=35)
                for upd in r.json().get("result",[]):
                    offset = upd["update_id"]+1
                    msg = upd.get("message") or upd.get("channel_post") or {}
                    txt = (msg.get("text") or "").strip().lower()
                    cid = str((msg.get("chat") or {}).get("id",""))
                    if cid != str(chat): continue
                    if txt in ["/start","/resume"]:
                        _STATE["running"] = True
                        tg_edit(_COUNTER_ID, counter_text())
                    elif txt in ["/stop","/pause"]:
                        _STATE["running"] = False
                        tg_edit(_COUNTER_ID, counter_text())
                    elif txt == "/status":
                        tg_edit(_COUNTER_ID, counter_text())
                    elif txt == "/help":
                        tg_send("<b>Commands</b>\n/start /stop /status /help")
            except: pass
            time.sleep(2)
    threading.Thread(target=_run, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
#  THE REAL HUNT — click CTA, verify offer flow
# ══════════════════════════════════════════════════════════════════════════════
async def hunt_one(attempt, port):
    """
    Real offer detection:
    1. Fresh session → minimal cookies → netflix.com/in/
    2. Wait for page load
    3. Capture cookies BEFORE clicking (pre-click state)
    4. Click main CTA button
    5. Wait for navigation
    6. Check if we landed on email-only form (= offer) or payment page (= no offer)
    7. Return result with cookies
    """
    profile = str(HERE / f"_h_{uuid.uuid4().hex[:7]}")
    proc = None
    ua = random.choice(USER_AGENTS)

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
            "--window-size=1366,768",
            "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        time.sleep(3)
        ws  = get_ws_url(port)
        cdp = CDP(ws)
        await cdp.connect()

        await cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
        await cdp.cmd("Network.enable")
        await cdp.cmd("Page.enable")

        # Inject ONLY minimal fresh cookies (no auth, no old session)
        for c in make_fresh_cookies():
            await cdp.cmd("Network.setCookie", {
                "name":c["name"],"value":c["value"],"domain":c["domain"],
                "path":c["path"],"secure":c["secure"],"httpOnly":c["httpOnly"]
            })

        # Navigate to Netflix India
        await cdp.navigate(NETFLIX_IN)
        log(f"[{attempt}] Loaded Netflix India, waiting 8s...", "HUNT")
        await asyncio.sleep(8)

        cur_url = await cdp.url()
        log(f"[{attempt}] URL: {cur_url}", "HUNT")

        # Capture cookies RIGHT NOW — before clicking anything
        # These are the "pre-click" session cookies Netflix set for us
        pre_cookies = await cdp.all_cookies()
        nf_pre = [c for c in pre_cookies if "netflix.com" in c.get("domain","")]
        log(f"[{attempt}] Got {len(nf_pre)} Netflix cookies pre-click", "HUNT")

        # Now click the CTA
        click_result = await cdp.js(CLICK_CTA_JS)
        if not click_result or not click_result.get("clicked"):
            log(f"[{attempt}] No CTA button found on page", "WARN")
            await cdp.close()
            return {"found":False,"reason":"no_cta","attempt":attempt}

        log(f"[{attempt}] Clicked: '{click_result.get('text','')}' via {click_result.get('selector','')}", "HUNT")

        # Wait for navigation after click
        await asyncio.sleep(6)

        post_url = await cdp.url()
        log(f"[{attempt}] Post-click URL: {post_url}", "HUNT")

        # Check if we're in an offer flow
        flow = await cdp.js(CHECK_OFFER_FLOW_JS)
        if not flow:
            await cdp.close()
            return {"found":False,"reason":"js_eval_failed","attempt":attempt}

        log(f"[{attempt}] Flow check: offer={flow.get('is_offer')} payment={flow.get('is_payment')} evidence={flow.get('evidence')}", "HUNT")

        if flow.get("is_payment"):
            # No offer — Netflix wants payment
            await cdp.close()
            return {"found":False,"reason":"payment_required","url":post_url,"attempt":attempt}

        if flow.get("is_login"):
            await cdp.close()
            return {"found":False,"reason":"redirected_to_login","url":post_url,"attempt":attempt}

        if not flow.get("is_offer") and not flow.get("is_email_form"):
            await cdp.close()
            return {"found":False,"reason":f"unknown_page:{post_url[:60]}","attempt":attempt}

        # OFFER CONFIRMED
        log(f"[{attempt}] OFFER CONFIRMED! URL={post_url} evidence={flow.get('evidence')}", "OK")

        # Grab cookies AFTER click too (might have more session state)
        post_cookies = await cdp.all_cookies()
        nf_post = [c for c in post_cookies if "netflix.com" in c.get("domain","")]

        await cdp.close()

        return {
            "found": True,
            "attempt": attempt,
            "pre_url": NETFLIX_IN,
            "post_url": post_url,
            "cta_clicked": click_result.get("text",""),
            "evidence": flow.get("evidence",[]),
            "cookies_pre": nf_pre,    # cookies from homepage (offer state)
            "cookies_post": nf_post,  # cookies after clicking into flow
        }

    except Exception as e:
        log(f"[{attempt}] Error: {e}", "ERR")
        return {"found":False,"reason":str(e),"attempt":attempt}
    finally:
        kill_port(port)
        if proc:
            try: proc.terminate(); proc.wait(timeout=6)
            except: pass
        shutil.rmtree(profile, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Save and deliver
# ══════════════════════════════════════════════════════════════════════════════
def save_and_deliver(result, attempt):
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Save PRE-click cookies (these are the offer-state cookies for reuse)
    pre_path = HERE / f"OFFER_pre_{ts_str}.json"
    post_path = HERE / f"OFFER_post_{ts_str}.json"

    with open(pre_path, "w") as f:
        json.dump(result["cookies_pre"], f, indent=2)
    with open(post_path, "w") as f:
        json.dump(result["cookies_post"], f, indent=2)

    # best_offer always = latest find
    best = HERE / "best_offer.json"
    with open(best, "w") as f:
        json.dump({
            "meta": {
                "found_at": ts_str, "attempt": attempt,
                "cta_clicked": result.get("cta_clicked",""),
                "post_url": result.get("post_url",""),
                "evidence": result.get("evidence",[]),
                "pre_cookie_count": len(result["cookies_pre"]),
                "post_cookie_count": len(result["cookies_post"]),
            },
            "homepage_cookies": result["cookies_pre"],
            "offer_flow_cookies": result["cookies_post"],
        }, f, indent=2)

    log(f"Saved: {pre_path.name} ({len(result['cookies_pre'])} cookies)", "SAVE")
    log(f"Saved: {post_path.name} ({len(result['cookies_post'])} cookies)", "SAVE")
    log(f"Updated: best_offer.json", "SAVE")

    # Telegram delivery
    names_pre  = [c.get("name","?") for c in result["cookies_pre"]]
    names_post = [c.get("name","?") for c in result["cookies_post"]]
    msg = (
        "OFFER FOUND — WORKING COOKIES\n\n"
        f"Time: {ts_str}\n"
        f"Attempt: #{attempt}\n"
        f"CTA clicked: {result.get('cta_clicked','')[:50]}\n"
        f"Landed on: {result.get('post_url','')[:80]}\n"
        f"Evidence: {', '.join(result.get('evidence',[]))}\n\n"
        f"Homepage cookies ({len(names_pre)}): {', '.join(names_pre)}\n"
        f"Post-click cookies ({len(names_post)}): {', '.join(names_post)}\n\n"
        "Two files attached:\n"
        "OFFER_pre = homepage cookies (inject these to see offer)\n"
        "OFFER_post = post-click flow cookies"
    )
    tg_send(msg)
    tg_file(str(pre_path),  f"OFFER_pre cookies — attempt #{attempt}")
    tg_file(str(post_path), f"OFFER_post cookies — attempt #{attempt}")


# ══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ══════════════════════════════════════════════════════════════════════════════
async def main(args):
    global _TG_TOKEN, _TG_CHAT, _COUNTER_ID

    _TG_TOKEN = args.tg_token
    _TG_CHAT  = args.tg_chat

    print("\n" + "="*55)
    print("  NETFLIX REAL OFFER HUNTER v5.0")
    print("  Properly detects offer by clicking CTA + checking flow")
    print("="*55 + "\n")
    log(f"Delay: {args.delay}s | Max: {args.max_attempts}", "INFO")

    # Test chrome
    try:
        chrome = find_chrome()
        log(f"Chrome: {chrome}", "INFO")
    except FileNotFoundError as e:
        log(str(e), "ERR"); sys.exit(1)

    if _TG_TOKEN and _TG_CHAT:
        tg_poll(_TG_TOKEN, _TG_CHAT)
        tg_send(
            "<b>Netflix Real Offer Hunter v5.0 STARTED</b>\n\n"
            "This version ACTUALLY clicks the CTA and verifies\n"
            "if the offer flow appears (no payment = offer active).\n\n"
            f"Delay: {args.delay}s\n"
            "Cookies sent the moment real offer is found."
        )
        _COUNTER_ID = tg_send(counter_text())

    attempt = 0
    while attempt < args.max_attempts:
        if not _STATE["running"]:
            await asyncio.sleep(3); continue

        attempt += 1
        _STATE["attempt"] = attempt
        port = BASE_PORT + (attempt % 180)

        log(f"--- Attempt #{attempt} ---", "HUNT")
        result = await hunt_one(attempt, port)

        reason = result.get("reason","")
        found  = result.get("found", False)

        if found:
            _STATE["found"] += 1
            _STATE["last_result"] = f"OFFER FOUND (attempt #{attempt})"
            log(f"OFFER FOUND on #{attempt}!", "OK")
            save_and_deliver(result, attempt)
        else:
            _STATE["last_result"] = reason
            log(f"#{attempt} -> {reason}", "INFO")

        # Update TG counter every attempt
        if _TG_TOKEN and _COUNTER_ID:
            tg_edit(_COUNTER_ID, counter_text())

        d = args.delay + random.uniform(0, 2)
        log(f"Sleep {d:.1f}s...", "INFO")
        await asyncio.sleep(d)

    log("Max attempts reached.", "INFO")
    if _TG_TOKEN:
        tg_send(f"Hunt done. {_STATE['found']} offers found in {attempt} attempts.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Netflix Real Offer Hunter v5.0")
    ap.add_argument("--max-attempts", type=int, default=99999, help="Max attempts (default: infinite)")
    ap.add_argument("--delay", type=float, default=4.0, help="Delay between attempts (seconds)")
    ap.add_argument("--tg-token", type=str, default="", help="Telegram bot token")
    ap.add_argument("--tg-chat", type=str, default="", help="Telegram chat ID")
    args = ap.parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        log("Stopped.", "INFO")
        if _TG_TOKEN: tg_send("Hunter stopped.")
