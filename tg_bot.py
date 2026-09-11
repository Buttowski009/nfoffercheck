"""
Netflix Offer Hunter v7 — tg_bot.py
====================================
THE REAL DETECTION:
  Step 1: Load netflix.com/in/
  Step 2: Check if homepage itself shows FREE TRIAL text
          (not just "Get Started" — that's normal for everyone)
          Look for: "free", "try free", "Rs 0", "1 month", "30 days"
          in the actual CTA button or hero text
  Step 3: ONLY if free offer found on homepage → click → verify
  Step 4: Fix sameSite in saved cookies for Cookie-Editor

NORMAL FLOW (no offer): "Get Started" → Rs 149/month → NOT an offer
OFFER FLOW: "Try free for 1 month" / "Get 30 days free" → Rs 0
"""

import asyncio, json, os, re, shutil, subprocess, sys, time, uuid, io, threading, random, base64
from datetime import datetime
from pathlib import Path
import requests as http_requests
import websockets, websockets.exceptions
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN    = "8871993832:AAFKe9Y60EGhynDWO3ETESOdw2xawdA04rE"
ADMIN_ID = 8725113938
HERE     = Path(__file__).parent
NF_URL   = "https://www.netflix.com/in/"
BASE_PORT = 9400
HUNT_PORT = 9700

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
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
  window.chrome=window.chrome||{}; window.chrome.runtime=window.chrome.runtime||{};
  if(!window.chrome.csi)window.chrome.csi=()=>({startE:performance.now()});
})();
"""

# ════════════════════════════════════════════════════════
#  STEP 1: Check homepage for actual FREE offer text
#  This runs BEFORE clicking anything.
#  Normal homepage = "Get Started" + Rs 149 = NOT an offer
#  Offer homepage  = "Try free" / "Get 30 days free" / Rs 0
# ════════════════════════════════════════════════════════
CHECK_HOMEPAGE_JS = """
(function(){
  var body = document.body ? document.body.innerText : '';
  var bodyL = body.toLowerCase();

  var R = {has_offer: false, offer_text: '', offer_price: ''};

  // Price signals — Rs 0 or free
  var freeKeywords = [
    'try free', 'free trial', '30 days free', '1 month free', 'first month free',
    'join free', 'watch free', 'rs. 0', 'rs 0', '\u20b90', '\u20b9 0',
    '0 rupee', 'no charge', 'no cost', 'get 1 month', 'get 30'
  ];
  for (var kw of freeKeywords) {
    if (bodyL.includes(kw)) {
      R.has_offer = true;
      R.offer_text = kw;
      break;
    }
  }

  // Also check specific CTA buttons
  var btns = document.querySelectorAll('button, a');
  for (var b of btns) {
    var t = (b.textContent || '').toLowerCase().trim();
    if (t.includes('free') && (t.includes('try') || t.includes('get') || t.includes('watch') || t.includes('month') || t.includes('day'))) {
      R.has_offer = true;
      R.offer_text = b.textContent.trim().substring(0, 80);
      break;
    }
  }

  // Check for Rs/INR 0 in any element
  var allText = document.body.innerHTML || '';
  if (/[\u20b9][ ]*0|Rs[ ]*0|INR[ ]*0/i.test(allText)) {
    R.has_offer = true;
    R.offer_text = 'price Rs 0 found';
  }

  // Get what pricing IS shown (for logging)
  var priceMatch = bodyL.match(/\u20b9\s*\d+/);
  if (priceMatch) R.offer_price = priceMatch[0];

  return R;
})()
"""

# ════════════════════════════════════════════════════════
#  STEP 2: Click CTA only after offer confirmed on homepage
# ════════════════════════════════════════════════════════
CLICK_FREE_CTA_JS = """
(function(){
  // Try to click a free-specific button first
  var btns = document.querySelectorAll('button, a');
  for (var b of btns) {
    var t = (b.textContent || '').toLowerCase().trim();
    if (b.offsetParent !== null && (
      t.includes('free') || t.includes('try') || t === 'get started'
    )) {
      var rect = b.getBoundingClientRect();
      if (rect.top < 700 && rect.width > 50) {
        b.scrollIntoView({block:'center'}); b.click();
        return {clicked:true, text:b.textContent.trim().substring(0,80)};
      }
    }
  }
  return {clicked:false, text:''};
})()
"""

# ════════════════════════════════════════════════════════
#  STEP 3: After click — confirm no payment required
# ════════════════════════════════════════════════════════
VERIFY_FLOW_JS = """
(function(){
  var url  = window.location.href;
  var urlL = url.toLowerCase();
  var body = document.body ? document.body.innerText.toLowerCase() : '';

  // payment = definitely not free
  var payInputs = document.querySelectorAll(
    'input[name="cardnumber"],[data-uia*="payment"],[data-uia*="credit"]'
  );
  if (payInputs.length > 0 || urlL.includes('/creditoption') || urlL.includes('/payment')
      || body.includes('credit card') || body.includes('debit card')) {
    return {ok: false, reason: 'payment_required'};
  }
  if (urlL.includes('/login')) return {ok:false, reason:'login_redirect'};

  var emailInputs = document.querySelectorAll(
    'input[type="email"], input[name="userLoginId"], input[autocomplete="email"]'
  );
  if (emailInputs.length > 0) {
    return {ok:true, url:url, reason:'email_form_no_payment'};
  }

  return {ok:false, reason:'no_email_form'};
})()
"""


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
                subprocess.run(f'taskkill /F /PID {parts[-1]}', shell=True, capture_output=True)
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

def fix_samesite(cookies):
    """Fix sameSite values for Cookie-Editor compatibility."""
    mapping = {
        "None":        "no_restriction",
        "Lax":         "lax",
        "Strict":      "strict",
        "no_restriction": "no_restriction",
        "lax":         "lax",
        "strict":      "strict",
        "":            "unspecified",
    }
    fixed = []
    for c in cookies:
        c = dict(c)
        ss = c.get("sameSite", "")
        c["sameSite"] = mapping.get(ss, "no_restriction")
        fixed.append(c)
    return fixed

def make_fresh_cookies():
    val = "BQFmAAEBE" + base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")[:78] + "%3D%3D"
    cid = str(uuid.uuid4())
    ts_ms = int(time.time() * 1000)
    consent = (
        f"landingPath=https%3A%2F%2Fwww.netflix.com%2Fin%2F"
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
    async def navigate(self, url): await self.cmd("Page.navigate",{"url":url}); await asyncio.sleep(2)
    async def all_cookies(self): return (await self.cmd("Network.getAllCookies")).get("cookies",[])
    async def close(self):
        if self.ws:
            try: await self.ws.close()
            except: pass


# ════ Old pipeline (kept) ════
MAX_RETRIES = 3; PAGE_LOAD_WAIT = 8; BANNER_WAIT = 15
MAIL_POLL_INTERVAL = 5; MAIL_POLL_MAX_WAIT = 15
GRAPH_TOKEN_URL    = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
_processing = False

COOKIES_FILE = HERE / "cookies.json"
def _load_cookies():
    try:
        raw = COOKIES_FILE.read_text(encoding="utf-8").strip()
        idx = raw.find("["); raw = raw[idx:] if idx >= 0 else raw
        return [{"name":c["name"],"value":c["value"],"domain":c["domain"],
                 "path":c.get("path","/"),"secure":c.get("secure",False),
                 "httpOnly":c.get("httpOnly",False)} for c in json.loads(raw)]
    except: return []
HARD_COOKIES = _load_cookies()

def parse_entries(text):
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
    headers = {"Authorization":f"Bearer {access_token}"}
    deadline = time.time() + MAIL_POLL_MAX_WAIT
    while time.time() < deadline:
        try:
            r = http_requests.get(GRAPH_MESSAGES_URL, headers=headers,
                params={"$top":10,"$orderby":"receivedDateTime desc",
                        "$select":"subject,body,from"}, timeout=15)
            for msg in r.json().get("value",[]):
                body = msg.get("body",{}).get("content","")
                subj = msg.get("subject","")
                frm  = msg.get("from",{}).get("emailAddress",{}).get("address","")
                if "netflix" in subj.lower() or "netflix" in frm.lower():
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
        await cdp.navigate(NF_URL); await asyncio.sleep(PAGE_LOAD_WAIT)
        clicked = False
        for sel in ['button[data-uia="free-trial-banner"]','[data-uia="free-trial-banner"]']:
            v = await cdp.js(f"(function(){{var el=document.querySelector('{sel}');if(el&&el.offsetParent!==null){{el.click();return true;}}return false;}})()")
            if v: clicked = True; break
        if not clicked:
            v = await cdp.js("(function(){var btns=document.querySelectorAll('button');for(var b of btns){if(b.textContent.includes('30 days')||b.textContent.includes('₹0')){b.click();return true;}}return false;})()")
            if v: clicked = True
        if not clicked: return False
        await asyncio.sleep(3)
        for sel in ['[data-uia="field-userLoginId"]','input[name="userLoginId"]','input[type="email"]']:
            ok = await cdp.js(f"""(function(){{var el=document.querySelector('{sel}');if(!el)return false;
              var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
              s.call(el,'');el.dispatchEvent(new Event('input',{{bubbles:true}}));
              s.call(el,'{email}');el.dispatchEvent(new Event('input',{{bubbles:true}}));
              el.dispatchEvent(new Event('change',{{bubbles:true}}));return true;}})()""")
            if ok: break
        await asyncio.sleep(1)
        for sel in ['button[data-uia="continue-button"]','button[type="submit"]']:
            await cdp.js(f"(function(){{var el=document.querySelector('{sel}');if(el)el.click();}})()")
        await asyncio.sleep(5)
        return True
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
    result["link"] = link if link else "FAILED — link not found"
    return result


# ════ Hunter ════
class HunterState:
    running=False; task=None; attempt=0; found=0
    start_ts=None; last="idle"; counter_id=0

HUNTER = HunterState()

def build_counter():
    if not HUNTER.start_ts:
        return "<b>Hunter</b>\nStatus: IDLE\n/hunt to start"
    el = int(time.time()-HUNTER.start_ts)
    h,m,s = el//3600,(el%3600)//60,el%60
    rate = HUNTER.attempt/max(1,el/60)
    st = "HUNTING" if HUNTER.running else "STOPPED"
    last = str(HUNTER.last)[:55].replace("<","&lt;")
    return (
        "<b>Netflix Offer Hunter v7</b>\n"
        "--------------------------------\n"
        f"Status:   {st}\n"
        f"Attempts: <code>{HUNTER.attempt}</code>\n"
        f"Found:    <code>{HUNTER.found}</code>\n"
        f"Speed:    <code>{rate:.1f}/min</code>\n"
        f"Uptime:   <code>{h}h {m}m {s}s</code>\n"
        "--------------------------------\n"
        f"Last: <code>{last}</code>\n"
        "--------------------------------\n"
        "<i>/hunt /huntstop /huntstatus</i>"
    )

async def hunt_one(attempt, port):
    profile = str(HERE / f"_h_{uuid.uuid4().hex[:7]}")
    proc = None; ua = random.choice(UA_LIST)
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

        await cdp.navigate(NF_URL)
        await asyncio.sleep(8)

        # ── STEP 1: Check homepage for FREE offer text ──────────
        hp = await cdp.js(CHECK_HOMEPAGE_JS)
        if not hp or not hp.get("has_offer"):
            price = hp.get("offer_price","?") if hp else "?"
            await cdp.close()
            return {"found":False, "reason":f"homepage_no_offer (price:{price})"}

        print(f"[{attempt}] Offer text on homepage: '{hp.get('offer_text','')}' CLICKING...")

        # ── STEP 2: Click the free offer CTA ───────────────────
        click = await cdp.js(CLICK_FREE_CTA_JS)
        if not click or not click.get("clicked"):
            await cdp.close()
            return {"found":False, "reason":"no_cta_found"}

        await asyncio.sleep(7)

        # ── STEP 3: Verify no payment required ─────────────────
        flow = await cdp.js(VERIFY_FLOW_JS)
        if not flow or not flow.get("ok"):
            reason = flow.get("reason","unknown") if flow else "js_fail"
            await cdp.close()
            return {"found":False, "reason":reason}

        # ── OFFER 100% REAL ─────────────────────────────────────
        print(f"[{attempt}] REAL OFFER CONFIRMED!")
        cookies = await cdp.all_cookies()
        nf = [c for c in cookies if "netflix.com" in c.get("domain","")]
        nf_fixed = fix_samesite(nf)  # Fix for Cookie-Editor
        await cdp.close()
        return {"found":True, "cookies":nf_fixed,
                "offer_text":hp.get("offer_text",""),
                "landing_url":flow.get("url","")}

    except Exception as e:
        return {"found":False, "reason":str(e)}
    finally:
        kill_port(port)
        if proc:
            try: proc.terminate(); proc.wait(timeout=6)
            except: pass
        shutil.rmtree(profile, ignore_errors=True)

async def hunter_loop(app):
    attempt = 0
    while HUNTER.running:
        attempt += 1; HUNTER.attempt = attempt
        port = HUNT_PORT + (attempt % 150)
        result = await hunt_one(attempt, port)
        found  = result.get("found",False)

        if found:
            HUNTER.found += 1
            HUNTER.last  = f"OFFER FOUND #{attempt}"
            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = HERE / f"OFFER_{ts_str}.json"
            with open(out,"w") as f: json.dump(result["cookies"], f, indent=2)
            best = HERE / "best_offer.json"
            with open(best,"w") as f:
                json.dump({"found_at":ts_str,"attempt":attempt,
                           "offer_text":result.get("offer_text",""),
                           "landing_url":result.get("landing_url",""),
                           "cookies":result["cookies"]}, f, indent=2)
            names = [c.get("name","?") for c in result["cookies"]]
            await app.bot.send_message(chat_id=ADMIN_ID, parse_mode="HTML", text=(
                f"<b>REAL OFFER FOUND!</b>\n\n"
                f"Attempt: #{attempt}\n"
                f"Offer text: <code>{result.get('offer_text','')[:60]}</code>\n"
                f"Landed on: <code>{result.get('landing_url','')[:80]}</code>\n"
                f"Cookies ({len(names)}) — sameSite FIXED for Cookie-Editor:\n"
                f"<code>{', '.join(names)}</code>"
            ))
            with open(out,"rb") as f:
                await app.bot.send_document(chat_id=ADMIN_ID, document=f,
                    filename=out.name, caption="Offer cookies — sameSite fixed, ready for Cookie-Editor")
        else:
            HUNTER.last = result.get("reason","?")

        if HUNTER.counter_id:
            try:
                await app.bot.edit_message_text(
                    chat_id=ADMIN_ID, message_id=HUNTER.counter_id,
                    text=build_counter(), parse_mode="HTML")
            except: pass

        await asyncio.sleep(4 + random.uniform(0,2))

    HUNTER.running = False
    if HUNTER.counter_id:
        try:
            await app.bot.edit_message_text(
                chat_id=ADMIN_ID, message_id=HUNTER.counter_id,
                text=build_counter(), parse_mode="HTML")
        except: pass


# ════ Telegram handlers ════
def is_admin(u): return u.effective_user and u.effective_user.id == ADMIN_ID

async def cmd_start(u, ctx):
    if not is_admin(u): return
    await u.message.reply_text(
        "Netflix Bot v7\n\n"
        "Hunter (find free trial offer cookies):\n"
        "/hunt — start hunting\n"
        "/huntstop — stop\n"
        "/huntstatus — refresh counter\n\n"
        "Old pipeline:\nSend mail|pass|refresh|client_id"
    )

async def cmd_hunt(u, ctx):
    if not is_admin(u): return
    if HUNTER.running:
        await u.message.reply_text("Already hunting! /huntstop to stop.")
        return
    HUNTER.running=True; HUNTER.attempt=0; HUNTER.found=0
    HUNTER.start_ts=time.time(); HUNTER.last="starting..."
    sent = await u.message.reply_text(build_counter(), parse_mode="HTML")
    HUNTER.counter_id = sent.message_id
    HUNTER.task = asyncio.create_task(hunter_loop(ctx.application))
    await u.message.reply_text(
        "Hunter started!\n\n"
        "Now checks homepage for FREE offer text FIRST.\n"
        "Only sends cookies when actual free trial confirmed."
    )

async def cmd_huntstop(u, ctx):
    if not is_admin(u): return
    HUNTER.running = False
    if HUNTER.task: HUNTER.task.cancel()
    await u.message.reply_text("Hunter stopped.")

async def cmd_huntstatus(u, ctx):
    if not is_admin(u): return
    if HUNTER.counter_id:
        try:
            await ctx.bot.edit_message_text(
                chat_id=u.effective_chat.id,
                message_id=HUNTER.counter_id,
                text=build_counter(), parse_mode="HTML")
            await u.message.reply_text("Counter refreshed above.")
        except:
            await u.message.reply_text(build_counter(), parse_mode="HTML")
    else:
        await u.message.reply_text(build_counter(), parse_mode="HTML")

async def handle_text(u, ctx):
    if not is_admin(u): return
    global _processing
    if _processing: await u.message.reply_text("Busy. Wait."); return
    entries = parse_entries(u.message.text)
    if not entries:
        await u.message.reply_text("Format: mail|pass|refresh_token|client_id"); return
    await run_pipeline(u, ctx, entries)

async def handle_file(u, ctx):
    if not is_admin(u): return
    global _processing
    if _processing: await u.message.reply_text("Busy. Wait."); return
    doc = u.message.document
    if not doc.file_name.endswith(".txt"):
        await u.message.reply_text("Send .txt file"); return
    file = await doc.get_file()
    raw  = await file.download_as_bytearray()
    entries = parse_entries(raw.decode("utf-8","ignore"))
    if not entries: await u.message.reply_text("No valid entries."); return
    await run_pipeline(u, ctx, entries)

async def run_pipeline(u, ctx, entries):
    global _processing
    _processing = True
    total = len(entries)
    status = await u.message.reply_text(f"Processing {total} email(s)...")
    try:
        for i, entry in enumerate(entries):
            try: await status.edit_text(f"{i+1}/{total} processing...")
            except: pass
            result = await process_one(entry, BASE_PORT)
            await ctx.bot.send_message(chat_id=u.effective_chat.id,
                text=f"{i+1}.\nMail: {result['mail']}\nPass: {result['pass']}\nLink: {result['link']}")
            if i < total-1: await asyncio.sleep(3)
        ok = sum(1 for r in entries if True)
        try: await status.edit_text(f"Done — {total} processed")
        except: pass
    except Exception as e:
        try: await status.edit_text(f"Error: {e}")
        except: pass
    finally:
        _processing = False


def main():
    print("="*40); print("  Netflix Bot v7 — Starting..."); print("="*40)
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
