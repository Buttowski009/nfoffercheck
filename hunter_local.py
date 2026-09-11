"""
Netflix India Offer Cookie Hunter — hunter_local.py
====================================================
Run: python hunter_local.py

Looks for the EXACT same banner workinglocal.py used.
Sends real working offer cookies to Telegram when found.
"""

import asyncio, json, os, shutil, subprocess, time, uuid, random, threading, base64, warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timezone
from pathlib import Path
import requests, websockets

HERE      = Path(__file__).parent
NF_URL    = "https://www.netflix.com/in/"
BASE_PORT = 9600
DELAY     = 4.0

TG_TOKEN  = "8871993832:AAFKe9Y60EGhynDWO3ETESOdw2xawdA04rE"
TG_CHAT   = "8725113938"

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

# Exact same banner detection as workinglocal.py + button text check
BANNER_CHECK_JS = """
(function(){
  var R = {found:false, how:'', text:''};

  // 1. Exact selectors from workinglocal.py — these are the real ones
  var sels = [
    'button[data-uia="free-trial-banner"]',
    'button[aria-label*="free"]',
    'button[aria-label*="Try 30 days"]',
    '[data-uia="free-trial-banner"]'
  ];
  for(var s of sels){
    var el = document.querySelector(s);
    if(el && el.offsetParent !== null){
      R.found = true;
      R.how   = 'selector:' + s;
      R.text  = el.textContent.trim().substring(0,100);
      return R;
    }
  }

  // 2. Button text — "30 days" or "₹0" (also from workinglocal.py)
  var btns = document.querySelectorAll('button');
  for(var b of btns){
    var txt = b.textContent || '';
    if(b.offsetParent !== null && (txt.includes('30 days') || txt.includes('\u20b90'))){
      R.found = true;
      R.how   = 'btn_text';
      R.text  = txt.trim().substring(0,100);
      return R;
    }
  }

  return R;
})()
"""


# ── Helpers ──────────────────────────────────────────────────────
def find_chrome():
    for p in CHROME_PATHS:
        if os.path.exists(p): return p
    raise FileNotFoundError("Chrome not found!")

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
        f"&datestamp={datetime.now(timezone.utc).strftime('%a+%b+%d+%Y+%H%%3A%M%%3A%S+GMT%%2B0000')}"
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


# ── CDP ──────────────────────────────────────────────────────────
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


# ── Telegram ─────────────────────────────────────────────────────
class S:
    running    = True
    attempt    = 0
    found      = 0
    start      = time.time()
    last       = "starting..."
    mid        = 0   # live counter message_id

def tg_send(t):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":t,"parse_mode":"HTML"}, timeout=15)
        return r.json().get("result",{}).get("message_id",0)
    except: return 0

def tg_edit(mid, t):
    if not mid: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText",
            json={"chat_id":TG_CHAT,"message_id":mid,"text":t,"parse_mode":"HTML"}, timeout=15)
    except: pass

def tg_file(path, cap):
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id":TG_CHAT,"caption":cap,"parse_mode":"HTML"},
                files={"document":(os.path.basename(path),f,"application/json")}, timeout=30)
    except: pass

def counter():
    e = int(time.time()-S.start); h,m,s = e//3600,(e%3600)//60,e%60
    rate = S.attempt/max(1,e/60)
    status = "HUNTING" if S.running else "PAUSED"
    return (
        "<b>Netflix Offer Hunter</b>\n"
        "-----------------------------\n"
        f"Status:   {status}\n"
        f"Attempts: <code>{S.attempt}</code>\n"
        f"Found:    <code>{S.found}</code>\n"
        f"Speed:    <code>{rate:.1f}/min</code>\n"
        f"Uptime:   <code>{h}h {m}m {s}s</code>\n"
        "-----------------------------\n"
        f"Last: <code>{str(S.last)[:55]}</code>\n"
        "-----------------------------\n"
        "<i>/stop /start /status</i>"
    )

def start_poll():
    def _loop():
        off = 0
        while True:
            try:
                r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                    params={"timeout":30,"offset":off}, timeout=35)
                for u in r.json().get("result",[]):
                    off = u["update_id"]+1
                    msg = u.get("message") or u.get("channel_post") or {}
                    cmd = (msg.get("text") or "").strip().lower()
                    cid = str((msg.get("chat") or {}).get("id",""))
                    if cid != TG_CHAT: continue
                    if cmd in ["/start","/resume"]:  S.running=True;  tg_edit(S.mid, counter())
                    elif cmd in ["/stop","/pause"]:  S.running=False; tg_edit(S.mid, counter())
                    elif cmd == "/status":            tg_edit(S.mid, counter())
            except: pass
            time.sleep(2)
    threading.Thread(target=_loop, daemon=True).start()


# ── Single Attempt ────────────────────────────────────────────────
async def hunt_once(n, port):
    profile = str(HERE / f"_h_{uuid.uuid4().hex[:7]}")
    proc = None
    try:
        kill_port(port)
        os.makedirs(profile, exist_ok=True)
        proc = subprocess.Popen([
            find_chrome(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            f"--user-agent={random.choice(UAS)}",
            "--headless=new",
            "--incognito",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1366,768",
            "about:blank"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        time.sleep(3)
        cdp = CDP(get_ws(port))
        await cdp.connect()
        await cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
        await cdp.cmd("Network.enable")

        # Fresh anonymous cookies — new device fingerprint every attempt
        for c in fresh_cookies():
            await cdp.cmd("Network.setCookie", {
                "name":c["name"],"value":c["value"],"domain":c["domain"],
                "path":c["path"],"secure":c["secure"],"httpOnly":c["httpOnly"]
            })

        # Load Netflix India
        await cdp.cmd("Page.navigate", {"url": NF_URL})
        await asyncio.sleep(9)  # full render wait

        # Check for REAL offer banner (same as workinglocal.py)
        r = await cdp.js(BANNER_CHECK_JS)
        if not r or not r.get("found"):
            await cdp.close()
            return {"ok": False, "reason": "no_banner"}

        # Banner found — grab cookies NOW before anything changes
        all_cookies = await cdp.all_cookies()
        nf_cookies  = [c for c in all_cookies if "netflix.com" in c.get("domain","")]
        how         = r.get("how","")
        text        = r.get("text","")
        await cdp.close()

        print(f"  [+] #{n} BANNER FOUND! [{how}] '{text[:60]}'", flush=True)
        return {"ok": True, "cookies": nf_cookies, "how": how, "text": text}

    except Exception as e:
        print(f"  [!] #{n} error: {e}", flush=True)
        return {"ok": False, "reason": str(e)}
    finally:
        kill_port(port)
        if proc:
            try: proc.terminate(); proc.wait(timeout=5)
            except: pass
        shutil.rmtree(profile, ignore_errors=True)


# ── Save & Deliver ────────────────────────────────────────────────
def deliver(result, n):
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    path   = HERE / f"OFFER_{ts_str}.json"
    with open(path,"w") as f:
        json.dump(result["cookies"], f, indent=2)
    with open(HERE/"best_offer.json","w") as f:
        json.dump({"found_at":ts_str,"attempt":n,
                   "how":result.get("how"),"text":result.get("text"),
                   "cookies":result["cookies"]}, f, indent=2)
    names = [c.get("name","?") for c in result["cookies"]]
    tg_send(
        f"<b>OFFER BANNER DETECTED — COOKIES CAPTURED</b>\n\n"
        f"Attempt: #{n}\n"
        f"Banner: <code>{result.get('how','')}</code>\n"
        f"Text: <code>{result.get('text','')[:80]}</code>\n"
        f"Cookies ({len(names)}): <code>{', '.join(names)}</code>\n\n"
        f"<i>JSON attached below</i>"
    )
    tg_file(str(path), f"Offer cookies — attempt #{n}")
    print(f"\n{'='*55}", flush=True)
    print(f"  OFFER FOUND! Saved: {path.name}", flush=True)
    print(f"{'='*55}\n", flush=True)


# ── Main ─────────────────────────────────────────────────────────
async def main():
    print("="*55)
    print("  Netflix Offer Cookie Hunter — LOCAL")
    print("  Token/Chat hardcoded. Just run: python hunter_local.py")
    print("="*55+"\n")

    find_chrome()  # fail fast if no chrome

    start_poll()
    tg_send(
        "<b>Offer Hunter STARTED</b>\n\n"
        "Looking for: real offer banner [data-uia=free-trial-banner]\n"
        "Sends cookies only when banner confirmed.\n\n"
        "/stop /start /status"
    )
    S.mid = tg_send(counter())

    n = 0
    while True:
        if not S.running:
            await asyncio.sleep(3); continue

        n += 1
        S.attempt = n
        port = BASE_PORT + (n % 150)
        print(f"  [-] #{n} checking port {port}...", flush=True)

        result = await hunt_once(n, port)

        if result.get("ok"):
            S.found += 1
            S.last   = f"FOUND #{n}"
            deliver(result, n)
        else:
            S.last = result.get("reason","?")
            print(f"  [-] #{n} {S.last}", flush=True)

        tg_edit(S.mid, counter())
        await asyncio.sleep(DELAY + random.uniform(0, 1.5))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        tg_send("Hunter stopped.")
        print("\nStopped.")
