"""
Netflix Bot — tg_bot.py
=========================
/start        — show help
/hunt         — run the offer flow for all mails in mails.txt
                (uses cookies.json + workinglocal.py exact logic)
               sends mail|pass|link to Telegram

Send mail|pass|refresh_token|client_id text OR .txt file for single run.
"""

import asyncio, json, os, re, shutil, subprocess, sys, time, uuid
import requests as http_requests
import websockets, websockets.exceptions
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN    = "8871993832:AAFKe9Y60EGhynDWO3ETESOdw2xawdA04rE"
ADMIN_ID = 8725113938
HERE     = os.path.dirname(os.path.abspath(__file__))
MAILS_FILE   = os.path.join(HERE, "mails.txt")
RESULTS_FILE = os.path.join(HERE, "results.txt")
NETFLIX_URL  = "https://www.netflix.com/in/"
BASE_PORT    = 9400
MAX_RETRIES  = 3
PAGE_LOAD_WAIT = 8
BANNER_WAIT    = 15

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

GRAPH_TOKEN_URL    = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
WORKING_SCOPE      = "https://graph.microsoft.com/.default offline_access"

# ── Load cookies from cookies.json ────────────────────────────────
COOKIES_FILE = os.path.join(HERE, "cookies.json")
def _load_cookies():
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        idx = raw.find("["); raw = raw[idx:] if idx >= 0 else raw
        return [{"name":c["name"],"value":c["value"],"domain":c["domain"],
                 "path":c.get("path","/"),"secure":c.get("secure",False),
                 "httpOnly":c.get("httpOnly",False)} for c in json.loads(raw)]
    except Exception as e:
        print(f"[!] Could not load cookies.json: {e}")
        return []
HARD_COOKIES = _load_cookies()
print(f"Loaded {len(HARD_COOKIES)} cookies from cookies.json")

STEALTH_JS = r"""
(function(){
  try{delete Object.getPrototypeOf(navigator).webdriver;}catch(e){}
  Object.defineProperty(navigator,'webdriver',{get:()=>false,configurable:true});
  for(let k in window){if(k.match(/^cdc_/))try{delete window[k];}catch(e){}}
  for(let k in document){if(k.match(/^cdc_|^\$cdc_/))try{delete document[k];}catch(e){}}
  Object.defineProperty(navigator,'plugins',{get:()=>[
    {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'PDF',length:1},
    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:'',length:1},
    {name:'Native Client',filename:'internal-nacl-plugin',description:'',length:2}
  ]});
  Object.defineProperty(navigator,'languages',{get:()=>['en-IN','en-GB','en']});
  window.chrome=window.chrome||{};
  window.chrome.runtime=window.chrome.runtime||{PlatformOs:{WIN:'win'}};
  if(!window.chrome.csi)window.chrome.csi=()=>({startE:Date.now()});
  if(!window.chrome.loadTimes)window.chrome.loadTimes=()=>({commitLoadTime:Date.now()/1000});
  try{if(navigator.connection&&navigator.connection.rtt===0)
    Object.defineProperty(navigator.connection,'rtt',{get:()=>50,configurable:true});}catch(e){}
})();
"""

def find_chrome():
    for p in CHROME_PATHS:
        if os.path.exists(p): return p
    raise FileNotFoundError("Chrome not found!")

def parse_entries_text(text):
    entries = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = line.split("|")
        if len(parts) < 4: continue
        entries.append({"mail":parts[0].strip(),"pass":parts[1].strip(),
                        "refresh_token":parts[2].strip(),"client_id":parts[3].strip()})
    return entries

def parse_entries_file(filepath):
    with open(filepath,"r",encoding="utf-8") as f:
        return parse_entries_text(f.read())

def get_ws_url(port, retries=25):
    for _ in range(retries):
        try:
            tabs = http_requests.get(f"http://localhost:{port}/json", timeout=3).json()
            for t in tabs:
                if t.get("type") == "page": return t["webSocketDebuggerUrl"]
            if tabs: return tabs[0]["webSocketDebuggerUrl"]
        except: pass
        time.sleep(0.5)
    raise RuntimeError(f"Chrome port {port} not responding")


# ═══════════════════════════════════════════════════════════
#  CDP — exact same as workinglocal.py
# ═══════════════════════════════════════════════════════════
class CDP:
    def __init__(self, ws_url):
        self.ws_url=ws_url; self.ws=None; self._id=0; self._pending={}; self._dead=False
    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=50*1024*1024,
            ping_interval=None, ping_timeout=None, open_timeout=20)
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
        except: pass
        finally:
            self._dead = True
            for fut in self._pending.values():
                if not fut.done(): fut.set_exception(RuntimeError("ws dead"))
            self._pending.clear()
    async def send(self, method, params=None):
        if self._dead: return {}
        self._id += 1; mid = self._id
        fut = asyncio.get_running_loop().create_future(); self._pending[mid] = fut
        try: await self.ws.send(json.dumps({"id":mid,"method":method,"params":params or{}}))
        except: self._pending.pop(mid,None); return {}
        try:
            result = await asyncio.wait_for(fut, timeout=30); return result.get("result",{})
        except: self._pending.pop(mid,None); return {}
    async def goto(self, url):
        await self.send("Page.navigate",{"url":url}); await asyncio.sleep(2)
    async def wait_for(self, sel, timeout=20):
        deadline = time.time()+timeout; safe = sel.replace("'","\\'")
        while time.time() < deadline:
            r = await self.send("Runtime.evaluate",
                {"expression":f"!!document.querySelector('{safe}')","returnByValue":True})
            if r.get("result",{}).get("value"): return True
            await asyncio.sleep(0.5)
        return False
    async def click(self, sel, timeout=20):
        if not await self.wait_for(sel, timeout): return False
        safe = sel.replace('"','\\"')
        r = await self.send("Runtime.evaluate",{"expression":f'''(function(){{
            var el=document.querySelector("{safe}");
            if(el){{el.scrollIntoView({{block:"center"}});el.click();return true;}}
            return false;}})()''',"returnByValue":True})
        await asyncio.sleep(0.5); return r.get("result",{}).get("value",False)
    async def fill(self, sel, text, timeout=20):
        await self.wait_for(sel, timeout)
        escaped = text.replace("\\","\\\\").replace("'","\\'")
        safe_sel = sel.replace('"','\\"')
        r = await self.send("Runtime.evaluate",{"expression":f'''(function(){{
            var el=document.querySelector("{safe_sel}");
            if(!el) el=document.querySelector('[name="userLoginId"]');
            if(!el) el=document.querySelector('input[type="text"],input[type="email"]');
            if(!el) return 'not_found';
            el.scrollIntoView({{block:"center"}});el.focus();
            var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,"value").set;
            setter.call(el,"");el.dispatchEvent(new Event("input",{{bubbles:true}}));
            el.dispatchEvent(new Event("change",{{bubbles:true}}));
            setter.call(el,'{escaped}');el.dispatchEvent(new Event("input",{{bubbles:true}}));
            el.dispatchEvent(new Event("change",{{bubbles:true}}));
            el.dispatchEvent(new KeyboardEvent("keydown",{{bubbles:true}}));
            el.dispatchEvent(new KeyboardEvent("keyup",{{bubbles:true}}));
            return 'filled:'+el.value;}})()''',"returnByValue":True})
        val = (r.get("result") or {}).get("value","?")
        await asyncio.sleep(0.4); return val != "not_found"
    async def page_has_text(self, text):
        safe = text.replace('"','\\"')
        r = await self.send("Runtime.evaluate",
            {"expression":f'document.body&&document.body.innerText.includes("{safe}")',"returnByValue":True})
        return r.get("result",{}).get("value",False)
    async def close(self):
        if self.ws:
            try: await self.ws.close()
            except: pass


# ═══════════════════════════════════════════════════════════
#  Graph API helpers — exact same as workinglocal.py
# ═══════════════════════════════════════════════════════════
def get_access_token(refresh_token, client_id):
    try:
        r = http_requests.post(GRAPH_TOKEN_URL, data={
            "client_id":client_id,"refresh_token":refresh_token,
            "grant_type":"refresh_token","scope":WORKING_SCOPE,
        }, timeout=15)
        if r.status_code == 200:
            token = r.json().get("access_token","")
            if token: return token
        print(f"[TOKEN ERR] {r.status_code}")
    except Exception as e:
        print(f"[TOKEN ERR] {e}")
    return ""

def extract_netflix_link(body):
    m = re.search(r'https://www\.netflix\.com/epr\?code=[A-Za-z0-9_\-]+(?:#[A-Za-z0-9_\-]*)?', body)
    return m.group(0) if m else ""

async def get_latest_email_time(access_token):
    headers = {"Authorization":f"Bearer {access_token}"}
    try:
        r = http_requests.get(GRAPH_MESSAGES_URL, headers=headers,
            params={"$top":"1","$orderby":"receivedDateTime desc","$select":"receivedDateTime"}, timeout=10)
        if r.status_code == 200:
            msgs = r.json().get("value",[])
            if msgs: return msgs[0].get("receivedDateTime","")
    except: pass
    return ""

async def check_inbox(access_token, baseline_time):
    headers = {"Authorization":f"Bearer {access_token}"}
    try:
        r = http_requests.get(GRAPH_MESSAGES_URL, headers=headers,
            params={"$top":"15","$orderby":"receivedDateTime desc",
                    "$select":"subject,body,from,receivedDateTime"}, timeout=15)
        if r.status_code == 200:
            for msg in r.json().get("value",[]):
                msg_time = msg.get("receivedDateTime","")
                if baseline_time and msg_time <= baseline_time: continue
                body = msg.get("body",{}).get("content","")
                subj = msg.get("subject","")
                fr   = msg.get("from",{}).get("emailAddress",{}).get("address","")
                if ("netflix" in subj.lower() or "netflix" in fr.lower()
                        or "netflix.com/epr" in body.lower()
                        or "finish signing up" in subj.lower()):
                    link = extract_netflix_link(body)
                    if link: return link
    except: pass
    return ""


# ═══════════════════════════════════════════════════════════
#  Phase 1 — EXACT copy of workinglocal.py's phase1_one
#  with Telegram status messages added
# ═══════════════════════════════════════════════════════════
async def phase1_one(entry, idx, port, send_status):
    email = entry["mail"]
    result = {"mail":email,"pass":entry["pass"],"link":""}
    full_attempt = 0

    while full_attempt < MAX_RETRIES:
        full_attempt += 1
        profile_dir = os.path.join(HERE, f"p1_{uuid.uuid4().hex[:8]}")
        chrome_proc = None; cdp = None

        try:
            if full_attempt > 1:
                await send_status(f"[{idx}] Retry #{full_attempt} — {email}")

            token = get_access_token(entry["refresh_token"], entry["client_id"])
            if not token:
                await send_status(f"[{idx}] Token error — {email}")
                await asyncio.sleep(5); continue

            baseline_time = await get_latest_email_time(token)

            os.makedirs(profile_dir, exist_ok=True)
            chrome_proc = subprocess.Popen([
                find_chrome(),
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir}",
                "--incognito","--no-first-run","--no-default-browser-check",
                "--window-size=1920,1080","--start-maximized","about:blank",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(3)

            ws_url = get_ws_url(port); cdp = CDP(ws_url); await cdp.connect()
            await cdp.send("Page.addScriptToEvaluateOnNewDocument",{"source":STEALTH_JS})
            await cdp.send("Network.enable")
            for c in HARD_COOKIES:
                await cdp.send("Network.setCookie",{
                    "name":c["name"],"value":c["value"],"domain":c["domain"],
                    "path":c.get("path","/"),"secure":c.get("secure",False),
                    "httpOnly":c.get("httpOnly",False)})

            await cdp.goto(NETFLIX_URL); await asyncio.sleep(PAGE_LOAD_WAIT)

            # ── Banner click — exact from workinglocal.py ──
            banner_sels = [
                'button[data-uia="free-trial-banner"]',
                'button[aria-label*="free"]',
                'button[aria-label*="Try 30 days"]',
                '[data-uia="free-trial-banner"]'
            ]
            banner_ok = False
            for sel in banner_sels:
                if await cdp.wait_for(sel, timeout=BANNER_WAIT if not banner_ok else 3):
                    banner_ok = await cdp.click(sel, timeout=5)
                    if banner_ok: break
            if not banner_ok:
                r = await cdp.send("Runtime.evaluate",{"expression":'''(function(){
                    var btns=document.querySelectorAll("button");
                    for(var b of btns){
                        if(b.textContent.includes("30 days")||b.textContent.includes("\u20b90"))
                        {b.scrollIntoView({block:"center"});b.click();return "ok";}}
                    return "no";})()''',"returnByValue":True})
                if (r.get("result") or {}).get("value","") != "ok":
                    await send_status(f"[{idx}] Banner not found — retrying")
                    raise Exception("retry")

            await asyncio.sleep(3)

            # ── Email fill + Continue — exact from workinglocal.py ──
            email_sels = [
                '[data-uia="field-userLoginId"]','input[name="userLoginId"]',
                'input[autocomplete="email"]','input[type="email"]','input[type="text"]'
            ]
            cont_sels = ['button[data-uia="continue-button"]','button[type="submit"]']
            attempt = 0
            while True:
                attempt += 1
                if attempt > 1: await send_status(f"[{idx}] Fill retry #{attempt} — {email}")
                filled = False
                for sel in email_sels:
                    if await cdp.wait_for(sel, timeout=8):
                        filled = await cdp.fill(sel, email)
                        if filled: break
                if not filled:
                    await cdp.goto(NETFLIX_URL); await asyncio.sleep(PAGE_LOAD_WAIT)
                    for sel in ['button[data-uia="free-trial-banner"]','[data-uia="free-trial-banner"]']:
                        if await cdp.wait_for(sel, timeout=5):
                            await cdp.click(sel, timeout=3); break
                    await asyncio.sleep(3); continue
                await asyncio.sleep(1)
                clicked = False
                for sel in cont_sels:
                    if await cdp.wait_for(sel, timeout=5):
                        clicked = await cdp.click(sel, timeout=3)
                        if clicked: break
                if not clicked: await asyncio.sleep(2); continue
                await asyncio.sleep(5)
                if await cdp.page_has_text("something went wrong") or await cdp.page_has_text("went wrong"):
                    await asyncio.sleep(2); continue
                else: break

            await send_status(f"[{idx}] Offer sent — waiting 15s for email...")

            # ── Wait + check inbox ──
            await asyncio.sleep(15)
            link = await check_inbox(token, baseline_time)
            if link:
                result["link"] = link
                return result
            else:
                await send_status(f"[{idx}] Link not in inbox — retrying")
                raise Exception("retry")

        except Exception:
            pass
        finally:
            if cdp:
                try: await cdp.close()
                except: pass
            if chrome_proc:
                try:
                    subprocess.run(f"taskkill /F /T /PID {chrome_proc.pid}",
                        shell=True, capture_output=True, timeout=5)
                except: pass
            await asyncio.sleep(1)
            for _ in range(3):
                try:
                    if os.path.exists(profile_dir): shutil.rmtree(profile_dir, ignore_errors=True)
                    break
                except: await asyncio.sleep(1)
        await asyncio.sleep(3)

    result["link"] = "FAILED"
    return result


# ═══════════════════════════════════════════════════════════
#  Telegram Bot Handlers
# ═══════════════════════════════════════════════════════════
_processing = False

def is_admin(update): return update.effective_user and update.effective_user.id == ADMIN_ID

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text(
        "Netflix Bot\n\n"
        "/hunt — run offer flow for all mails in mails.txt\n\n"
        "Or send mail|pass|refresh_token|client_id\n"
        "(one per line, or .txt file)"
    )

async def cmd_hunt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global _processing
    if _processing:
        await update.message.reply_text("Already running. Wait.")
        return
    if not os.path.exists(MAILS_FILE):
        await update.message.reply_text("mails.txt not found in bot folder!")
        return
    entries = parse_entries_file(MAILS_FILE)
    if not entries:
        await update.message.reply_text("No valid entries in mails.txt")
        return
    await update.message.reply_text(f"Starting offer flow for {len(entries)} mail(s)...")
    await run_pipeline(update, ctx, entries)

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global _processing
    if _processing:
        await update.message.reply_text("Already running. Wait.")
        return
    entries = parse_entries_text(update.message.text)
    if not entries:
        await update.message.reply_text("Format: mail|pass|refresh_token|client_id")
        return
    await run_pipeline(update, ctx, entries)

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    global _processing
    if _processing:
        await update.message.reply_text("Already running. Wait.")
        return
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Send .txt file")
        return
    file = await doc.get_file()
    raw  = await file.download_as_bytearray()
    entries = parse_entries_text(raw.decode("utf-8","ignore"))
    if not entries:
        await update.message.reply_text("No valid entries in file")
        return
    await run_pipeline(update, ctx, entries)

async def run_pipeline(update: Update, ctx: ContextTypes.DEFAULT_TYPE, entries: list):
    global _processing
    _processing = True
    total  = len(entries)
    status = await update.message.reply_text(f"0/{total} done...")
    done   = 0

    async def send_status(msg):
        print(msg)

    try:
        for i, entry in enumerate(entries):
            try: await status.edit_text(f"{i+1}/{total} running — {entry['mail']}")
            except: pass

            port   = BASE_PORT + i
            result = await phase1_one(entry, i+1, port, send_status)
            done  += 1

            await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"{i+1}.\n"
                    f"Mail: {result['mail']}\n"
                    f"Pass: {result['pass']}\n"
                    f"Link: {result['link']}"
                )
            )
            if i < total-1: await asyncio.sleep(2)

        success = sum(1 for r in [result] if r["link"].startswith("https://"))
        try: await status.edit_text(f"Done — {done}/{total} processed")
        except: pass

    except Exception as e:
        try: await status.edit_text(f"Error: {e}")
        except: pass
    finally:
        _processing = False


def main():
    print("="*40)
    print("  Netflix Bot — Starting...")
    print(f"  Cookies loaded: {len(HARD_COOKIES)}")
    print("="*40)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("hunt",  cmd_hunt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    print("  Bot live.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
