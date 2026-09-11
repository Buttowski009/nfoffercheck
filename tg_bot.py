"""
Netflix Bot v6.0 — tg_bot.py
================================
OLD features kept:
  - Send mail|pass|refresh|client_id  -> get signup link

NEW feature added:
  /hunt        — start offer cookie hunter (runs in background)
  /huntstop    — stop hunter
  /huntstatus  — check hunter progress (updates live counter)

Hunter logic:
  - Fresh incognito session each attempt
  - Goes to netflix.com/in/
  - Clicks main CTA (Get Started)
  - Checks if landing page = email form only (no payment) = OFFER
  - If offer found -> sends cookies.json to your Telegram
"""

import asyncio, json, os, re, shutil, subprocess, sys, time, uuid, io, threading, random, base64
from datetime import datetime
from pathlib import Path
import requests as http_requests
import websockets
import websockets.exceptions
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)

# ── Bot Config ────────────────────────────────────────────────────────────────
TOKEN    = "8871993832:AAFKe9Y60EGhynDWO3ETESOdw2xawdA04rE"
ADMIN_ID = 8725113938

HERE        = Path(__file__).parent
NETFLIX_URL = "https://www.netflix.com/in/"
BASE_PORT   = 9400
HUNT_PORT   = 9700   # separate port range for hunter

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

# ═══════════════════════════════════════════════════════════════════
#  Netflix Cookies (for old link pipeline)
# ═══════════════════════════════════════════════════════════════════
COOKIES_FILE = HERE / "cookies.json"
def _load_cookies():
    try:
        raw = COOKIES_FILE.read_text(encoding="utf-8").strip()
        idx = raw.find("[")
        if idx >= 0: raw = raw[idx:]
        return [{"name":c["name"],"value":c["value"],"domain":c["domain"],
                 "path":c.get("path","/"),"secure":c.get("secure",False),
                 "httpOnly":c.get("httpOnly",False)} for c in json.loads(raw)]
    except:
        return []
HARD_COOKIES = _load_cookies()

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
  try{if(navigator.connection&&navigator.connection.rtt===0)
    Object.defineProperty(navigator.connection,'rtt',{get:()=>50,configurable:true});}catch(e){}
})();
"""

# ── Click CTA JS ──────────────────────────────────────────────────
CLICK_CTA_JS = """
(function(){
  var sels=['button[data-uia="free-trial-banner"]','[data-uia="free-trial-banner"]',
    'a[data-uia="hero-cta"]','button[data-uia="hero-cta"]','[data-uia="hero-cta"]',
    'a[data-uia="get-started-button"]','[data-uia="get-started-button"]',
    '.hero-cta a','.hero-cta button','.nfheader .cta a','.nfheader .cta button'];
  for(var s of sels){var el=document.querySelector(s);
    if(el&&el.offsetParent!==null){el.scrollIntoView({block:'center'});el.click();
      return {clicked:true,sel:s,text:el.textContent.trim().substring(0,80)};}}
  var all=document.querySelectorAll('a,button');
  for(var el of all){var txt=(el.textContent||'').trim().toLowerCase();
    var r=el.getBoundingClientRect();
    if(r.top<600&&r.width>50&&el.offsetParent!==null){
      if(txt.includes('get started')||txt.includes('start watching')||
         txt.includes('try now')||txt.includes('join now')||
         txt.includes('subscribe')||txt.includes('sign up')){
        el.scrollIntoView({block:'center'});el.click();
        return {clicked:true,sel:'text_fallback',text:el.textContent.trim().substring(0,80)};}}}
  return {clicked:false,sel:'',text:''};
})()
"""

# ── Check if offer flow JS ────────────────────────────────────────
CHECK_FLOW_JS = """
(function(){
  var url=window.location.href.toLowerCase();
  var body=document.body?document.body.innerText.toLowerCase():'';
  var R={url:window.location.href,is_offer:false,is_payment:false,is_login:false,evidence:[]};
  if(url.includes('/signup/registration')){R.is_offer=true;R.evidence.push('signup/registration');}
  var emailInputs=document.querySelectorAll('input[type="email"],input[name="userLoginId"],input[autocomplete="email"]');
  var payInputs=document.querySelectorAll('input[name="cardnumber"],[data-uia*="payment"],[data-uia*="credit"]');
  if(emailInputs.length>0&&payInputs.length===0){R.is_offer=true;R.evidence.push('email_form_no_payment');}
  if(payInputs.length>0||url.includes('/creditoption')||url.includes('/payment')||
     body.includes('credit card')||body.includes('debit card')||body.includes('upi payment')){
    R.is_payment=true;R.is_offer=false;R.evidence.push('payment_page');}
  if(url.includes('/login')){R.is_login=true;R.is_offer=false;R.evidence.push('login_redirect');}
  return R;
})()
"""


# ═══════════════════════════════════════════════════════════════════
#  CDP Client
# ═══════════════════════════════════════════════════════════════════
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
            tabs = http_requests.get(f"http://localhost:{port}/json", timeout=3).json()
            for t in tabs:
                if t.get("type") == "page": return t["webSocketDebuggerUrl"]
            if tabs: return tabs[0]["webSocketDebuggerUrl"]
        except: pass
        time.sleep(0.6)
    raise RuntimeError(f"Chrome :{port} not responding")

class CDP:
    def __init__(self, ws_url):
        self.ws_url=ws_url; self.ws=None; self._id=0; self._pending={}; self._dead=False
    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=50*1024*1024,
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
    async def url(self): return await self.js("window.location.href") or ""
    async def navigate(self, url): await self.cmd("Page.navigate",{"url":url}); await asyncio.sleep(2)
    async def all_cookies(self):
        r = await self.cmd("Network.getAllCookies"); return r.get("cookies",[])
    async def close(self):
        if self.ws:
            try: await self.ws.close()
            except: pass


# ═══════════════════════════════════════════════════════════════════
#  OLD pipeline helpers (kept from original)
# ═══════════════════════════════════════════════════════════════════
MAX_RETRIES    = 3
PAGE_LOAD_WAIT = 8
BANNER_WAIT    = 15
MAIL_POLL_INTERVAL = 5
MAIL_POLL_MAX_WAIT = 15
GRAPH_TOKEN_URL    = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
_processing = False

def parse_entries(text: str) -> list:
    entries = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = line.split("|")
        if len(parts) < 4: continue
        entries.append({"mail":parts[0].strip(),"pass":parts[1].strip(),
                        "refresh_token":parts[2].strip(),"client_id":parts[3].strip()})
    return entries

def get_access_token(refresh_token, client_id):
    try:
        r = http_requests.post(GRAPH_TOKEN_URL, data={
            "client_id":client_id,"refresh_token":refresh_token,
            "grant_type":"refresh_token","scope":"https://graph.microsoft.com/Mail.Read"
        }, timeout=15)
        return r.json().get("access_token","")
    except: return ""

def extract_netflix_link(body):
    m = re.search(r'https://www\.netflix\.com/epr\?code=[A-Za-z0-9_\-]+(?:#[A-Za-z0-9_\-]*)?', body)
    return m.group(0) if m else ""

async def poll_inbox(access_token, since_ts):
    headers = {"Authorization":f"Bearer {access_token}","Content-Type":"application/json"}
    deadline = time.time() + MAIL_POLL_MAX_WAIT
    while time.time() < deadline:
        try:
            r = http_requests.get(GRAPH_MESSAGES_URL, headers=headers,
                params={"$top":10,"$orderby":"receivedDateTime desc",
                        "$select":"subject,body,from,receivedDateTime"}, timeout=15)
            for msg in r.json().get("value",[]):
                body = msg.get("body",{}).get("content","")
                subj = msg.get("subject","")
                frm  = msg.get("from",{}).get("emailAddress",{}).get("address","")
                if "netflix" in subj.lower() or "netflix" in frm.lower() or "netflix.com/epr" in body.lower():
                    link = extract_netflix_link(body)
                    if link: return link
        except: pass
        await asyncio.sleep(MAIL_POLL_INTERVAL)
    return ""

async def netflix_phase(email, port):
    profile = str(HERE / f"_tmp_{uuid.uuid4().hex[:7]}")
    proc = None
    try:
        kill_port(port); os.makedirs(profile, exist_ok=True)
        proc = subprocess.Popen([find_chrome(),f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}","--incognito","--no-first-run",
            "--no-default-browser-check","--window-size=1366,768","about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        ws = get_ws_url(port); cdp = CDP(ws); await cdp.connect()
        await cdp.cmd("Page.addScriptToEvaluateOnNewDocument",{"source":STEALTH_JS})
        await cdp.cmd("Network.enable")
        for c in HARD_COOKIES:
            await cdp.cmd("Network.setCookie",{"name":c["name"],"value":c["value"],
                "domain":c["domain"],"path":c["path"],"secure":c["secure"],"httpOnly":c["httpOnly"]})
        await cdp.navigate(NETFLIX_URL)
        await asyncio.sleep(PAGE_LOAD_WAIT)
        banner_sels = ['button[data-uia="free-trial-banner"]','[data-uia="free-trial-banner"]',
                       'button[aria-label*="free"]','button[aria-label*="30 days"]']
        clicked = False
        for sel in banner_sels:
            v = await cdp.js(f"(function(){{var el=document.querySelector('{sel}');if(el&&el.offsetParent!==null){{el.scrollIntoView({{block:'center'}});el.click();return true;}}return false;}})()");
            if v: clicked = True; break
        if not clicked:
            v = await cdp.js("(function(){var btns=document.querySelectorAll('button');for(var b of btns){if(b.textContent.includes('30 days')||b.textContent.includes('₹0')){b.scrollIntoView({block:'center'});b.click();return 'clicked:'+b.textContent.trim().substring(0,40);}}return 'nf';})()")
            if v and str(v).startswith("clicked:"): clicked = True
        if not clicked: return False
        await asyncio.sleep(3)
        email_sels = ['[data-uia="field-userLoginId"]','input[name="userLoginId"]',
                      'input[autocomplete="email"]','input[type="email"]','input[type="text"]']
        cont_sels  = ['button[data-uia="continue-button"]','button[type="submit"]']
        for attempt in range(MAX_RETRIES):
            filled = False
            for sel in email_sels:
                ok = await cdp.js(f"""(function(){{
                  var el=document.querySelector('{sel}');if(!el)return false;
                  el.focus();var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
                  setter.call(el,'');el.dispatchEvent(new Event('input',{{bubbles:true}}));
                  setter.call(el,'{email}');el.dispatchEvent(new Event('input',{{bubbles:true}}));
                  el.dispatchEvent(new Event('change',{{bubbles:true}}));return true;}})()""")
                if ok: filled = True; break
            if not filled: return False
            await asyncio.sleep(1)
            for sel in cont_sels:
                ok = await cdp.js(f"(function(){{var el=document.querySelector('{sel}');if(el){{el.click();return true;}}return false;}})()");
                if ok: break
            await asyncio.sleep(5)
            page_text = await cdp.js("document.body.innerText") or ""
            if "went wrong" not in page_text.lower(): return True
        return False
    except Exception as e:
        print(f"netflix_phase error: {e}")
        return False
    finally:
        kill_port(port)
        if proc:
            try: proc.terminate(); proc.wait(timeout=5)
            except: pass
        shutil.rmtree(profile, ignore_errors=True)

async def process_one(entry, port):
    result = {"mail":entry["mail"],"pass":entry["pass"],"link":""}
    baseline = time.time()
    token = get_access_token(entry["refresh_token"], entry["client_id"])
    if not token: result["link"]="FAILED — token error"; return result
    ok = await netflix_phase(entry["mail"], port)
    if not ok: result["link"]="FAILED — Netflix phase error"; return result
    await asyncio.sleep(15)
    link = await poll_inbox(token, baseline)
    result["link"] = link if link else "FAILED — link not found in inbox"
    return result


# ═══════════════════════════════════════════════════════════════════
#  OFFER HUNTER — runs as asyncio background task
# ═══════════════════════════════════════════════════════════════════
class HunterState:
    def __init__(self):
        self.running    = False
        self.task       = None        # asyncio.Task
        self.attempt    = 0
        self.found      = 0
        self.start_ts   = None
        self.last       = "idle"
        self.counter_id = 0           # TG message_id for live counter

HUNTER = HunterState()

def make_fresh_cookies():
    nfvdid_val = "BQFmAAEBE" + base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")[:78] + "%3D%3D"
    ts_ms = int(time.time() * 1000)
    cid   = str(uuid.uuid4())
    consent = (
        f"landingPath=https%3A%2F%2Fwww.netflix.com%2Fin%2F"
        f"&datestamp={datetime.now().strftime('%a+%b+%d+%Y+%H%%3A%M%%3A%S+GMT%%2B0530')}"
        f"&version=202604.2.0&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1"
        f"&hosts=&consentId={cid}&interactionCount=1&isAnonUser=1"
        f"&prevHadToken=0&intType=1&crTime={ts_ms}&isGpcEnabled=0"
        f"&browserGpcFlag=0&isDntEnabled=0&isIABGlobal=false&AwaitingReconsent=false"
        f"&geolocation=IN%3BMH"
    )
    return [
        {"name":"nfvdid","value":nfvdid_val,"domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"OptanonConsent","value":consent,"domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"netflix-sans-normal-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
        {"name":"netflix-sans-bold-3-loaded","value":"true","domain":".netflix.com","path":"/","secure":False,"httpOnly":False},
    ]

def build_counter(chat_id=None):
    if not HUNTER.start_ts:
        return "<b>Hunter</b>\nStatus: IDLE\nType /hunt to start"
    elapsed = int(time.time() - HUNTER.start_ts)
    h,m,s  = elapsed//3600,(elapsed%3600)//60,elapsed%60
    rate   = HUNTER.attempt / max(1, elapsed/60)
    status = "HUNTING" if HUNTER.running else "STOPPED"
    return (
        "<b>Netflix Offer Hunter</b>\n"
        "--------------------------------\n"
        f"Status:   {status}\n"
        f"Attempts: <code>{HUNTER.attempt}</code>\n"
        f"Found:    <code>{HUNTER.found}</code>\n"
        f"Speed:    <code>{rate:.1f}/min</code>\n"
        f"Uptime:   <code>{h}h {m}m {s}s</code>\n"
        "--------------------------------\n"
        f"Last: <code>{str(HUNTER.last)[:55]}</code>\n"
        "--------------------------------\n"
        "<i>/hunt /huntstop /huntstatus</i>"
    )

async def hunt_one_attempt(attempt, port):
    profile = str(HERE / f"_h_{uuid.uuid4().hex[:7]}")
    proc = None
    ua   = random.choice(USER_AGENTS)
    try:
        kill_port(port); os.makedirs(profile, exist_ok=True)
        proc = subprocess.Popen([
            find_chrome(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            f"--user-agent={ua}",
            "--incognito","--no-first-run","--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1366,768","about:blank"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        ws = get_ws_url(port); cdp = CDP(ws); await cdp.connect()
        await cdp.cmd("Page.addScriptToEvaluateOnNewDocument",{"source":STEALTH_JS})
        await cdp.cmd("Network.enable"); await cdp.cmd("Page.enable")
        for c in make_fresh_cookies():
            await cdp.cmd("Network.setCookie",{"name":c["name"],"value":c["value"],
                "domain":c["domain"],"path":c["path"],"secure":c["secure"],"httpOnly":c["httpOnly"]})
        await cdp.navigate(NETFLIX_URL)
        await asyncio.sleep(8)
        click = await cdp.js(CLICK_CTA_JS)
        if not click or not click.get("clicked"):
            await cdp.close()
            return {"found":False,"reason":"no_cta"}
        await asyncio.sleep(6)
        flow = await cdp.js(CHECK_FLOW_JS)
        if not flow:
            await cdp.close()
            return {"found":False,"reason":"js_fail"}
        if flow.get("is_payment"):
            await cdp.close()
            return {"found":False,"reason":"payment_page"}
        if flow.get("is_login"):
            await cdp.close()
            return {"found":False,"reason":"login_redirect"}
        if not flow.get("is_offer"):
            await cdp.close()
            return {"found":False,"reason":f"unknown:{flow.get('url','')[:50]}"}
        # OFFER FOUND
        cookies = await cdp.all_cookies()
        nf = [c for c in cookies if "netflix.com" in c.get("domain","")]
        await cdp.close()
        return {"found":True,"cookies":nf,"evidence":flow.get("evidence",[]),"cta":click.get("text","")}
    except Exception as e:
        return {"found":False,"reason":str(e)}
    finally:
        kill_port(port)
        if proc:
            try: proc.terminate(); proc.wait(timeout=6)
            except: pass
        shutil.rmtree(profile, ignore_errors=True)

async def hunter_loop(app):
    """Background task — runs hunt_one_attempt in a loop, updates TG counter."""
    attempt = 0
    while HUNTER.running:
        attempt += 1
        HUNTER.attempt = attempt
        port = HUNT_PORT + (attempt % 150)

        result = await hunt_one_attempt(attempt, port)
        found  = result.get("found", False)

        if found:
            HUNTER.found += 1
            HUNTER.last  = f"OFFER FOUND #{attempt}"

            # Save cookies
            ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = HERE / f"OFFER_{ts_str}.json"
            with open(out_path,"w") as f:
                json.dump(result["cookies"], f, indent=2)
            best = HERE / "best_offer.json"
            with open(best,"w") as f:
                json.dump({"found_at":ts_str,"attempt":attempt,
                           "evidence":result.get("evidence",[]),
                           "cta":result.get("cta",""),
                           "cookies":result["cookies"]}, f, indent=2)

            # Send offer message + file
            names = [c.get("name","?") for c in result["cookies"]]
            msg = (
                f"OFFER COOKIE FOUND!\n\n"
                f"Attempt: #{attempt}\n"
                f"CTA: {result.get('cta','')}\n"
                f"Evidence: {', '.join(result.get('evidence',[]))}\n"
                f"Cookies ({len(names)}): {', '.join(names)}\n\n"
                f"File: OFFER_{ts_str}.json"
            )
            await app.bot.send_message(chat_id=ADMIN_ID, text=msg)
            with open(out_path,"rb") as f:
                await app.bot.send_document(chat_id=ADMIN_ID, document=f,
                    filename=out_path.name, caption=f"Offer cookies — attempt #{attempt}")
        else:
            HUNTER.last = result.get("reason","?")

        # Update live counter message
        if HUNTER.counter_id:
            try:
                await app.bot.edit_message_text(
                    chat_id=ADMIN_ID,
                    message_id=HUNTER.counter_id,
                    text=build_counter(),
                    parse_mode="HTML"
                )
            except: pass

        # Small sleep between attempts
        await asyncio.sleep(4 + random.uniform(0, 2))

    HUNTER.running = False
    # Final counter update
    if HUNTER.counter_id:
        try:
            await app.bot.edit_message_text(
                chat_id=ADMIN_ID,
                message_id=HUNTER.counter_id,
                text=build_counter(),
                parse_mode="HTML"
            )
        except: pass


# ═══════════════════════════════════════════════════════════════════
#  Telegram Bot Handlers
# ═══════════════════════════════════════════════════════════════════
def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text(
        "Netflix Bot v6.0\n\n"
        "OLD: Send mail|pass|refresh|client_id to get signup link\n\n"
        "NEW: Offer Cookie Hunter\n"
        "/hunt       — start hunting for offer cookies\n"
        "/huntstop   — stop hunter\n"
        "/huntstatus — check live counter\n"
    )


async def cmd_hunt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if HUNTER.running:
        await update.message.reply_text("Already hunting! /huntstop to stop, /huntstatus to check.")
        return
    HUNTER.running  = True
    HUNTER.attempt  = 0
    HUNTER.found    = 0
    HUNTER.start_ts = time.time()
    HUNTER.last     = "starting..."
    # Send live counter message — this will be edited every attempt
    sent = await update.message.reply_text(build_counter(), parse_mode="HTML")
    HUNTER.counter_id = sent.message_id
    # Start background task
    HUNTER.task = asyncio.create_task(hunter_loop(ctx.application))
    await update.message.reply_text("Hunter started! Counter above will update live.")


async def cmd_huntstop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not HUNTER.running:
        await update.message.reply_text("Hunter not running.")
        return
    HUNTER.running = False
    if HUNTER.task:
        HUNTER.task.cancel()
    await update.message.reply_text("Hunter stopped.")


async def cmd_huntstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    # Re-edit the counter message
    if HUNTER.counter_id:
        try:
            await ctx.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=HUNTER.counter_id,
                text=build_counter(),
                parse_mode="HTML"
            )
            await update.message.reply_text("Counter updated above.")
        except:
            await update.message.reply_text(build_counter(), parse_mode="HTML")
    else:
        await update.message.reply_text(build_counter(), parse_mode="HTML")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global _processing
    if _processing:
        await update.message.reply_text("Already processing. Wait.")
        return
    entries = parse_entries(update.message.text)
    if not entries:
        await update.message.reply_text("No valid entries.\nFormat: mail|pass|refresh_token|client_id")
        return
    await run_pipeline(update, ctx, entries)


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global _processing
    if _processing:
        await update.message.reply_text("Already processing. Wait.")
        return
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Send a .txt file.")
        return
    file = await doc.get_file()
    raw  = await file.download_as_bytearray()
    entries = parse_entries(raw.decode("utf-8","ignore"))
    if not entries:
        await update.message.reply_text("No valid entries in file.")
        return
    await run_pipeline(update, ctx, entries)


async def run_pipeline(update: Update, ctx: ContextTypes.DEFAULT_TYPE, entries: list):
    global _processing
    _processing = True
    total  = len(entries)
    status = await update.message.reply_text(f"Processing {total} email(s)...")
    results = []
    try:
        for i, entry in enumerate(entries):
            try: await status.edit_text(f"{i+1}/{total} processing...")
            except: pass
            result = await process_one(entry, BASE_PORT)
            results.append(result)
            await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{i+1}.\nMail: {result['mail']}\nPass: {result['pass']}\nLink: {result['link']}"
            )
            if i < total-1: await asyncio.sleep(3)
        success = sum(1 for r in results if r["link"].startswith("https://"))
        try: await status.edit_text(f"Done — {success}/{total} successful")
        except: pass
    except Exception as e:
        try: await status.edit_text(f"Error: {e}")
        except: pass
    finally:
        _processing = False


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════
def main():
    print("=" * 40)
    print("  Netflix Bot v6.0 — Starting...")
    print("=" * 40)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("hunt",        cmd_hunt))
    app.add_handler(CommandHandler("huntstop",    cmd_huntstop))
    app.add_handler(CommandHandler("huntstatus",  cmd_huntstatus))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    print("  Bot live. /hunt to start offer hunting.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
