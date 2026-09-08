"""Talk to the desktop's Chrome over CDP without navigating anywhere.

Same shape as `sandbox.browser._CHROME_RENDERER_PROBE`: a self-contained
Python script is base64'd into one shell command and run *on the desktop*
(through the action server, or through the Cloud Assistant when testing),
so the backend never needs a CDP client or a route to port 9333.

Three actions share one script:

* ``probe``  — browser-level ``Storage.getCookies`` (names, domains, expiry;
               values are dropped before anything is printed), then for
               level 2 a same-origin ``fetch`` of the site's own JSON endpoint
               inside an existing tab of that site, or, when there is none, in a
               background target opened straight at the JSON URL and closed
               within seconds. No site page is ever navigated to.
* ``open``   — open the login page in a new tab and bring it to the front.
* ``logout`` — delete the site's cookies one by one (``Network.deleteCookies``),
               never the whole cookie jar.

The pure helpers (`judge_cookies`, `judge_probe`, `parse_output`) are unit
tested; the script itself is exercised on a real desktop.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field

SCRIPT = r'''import json,sys,time,urllib.parse,urllib.request
from websockets.sync.client import connect
P=json.loads(__import__("base64").b64decode("__PAYLOAD__").decode())
B="http://127.0.0.1:9333"
NOW=time.time()
def http(path,method="GET"):
    req=urllib.request.Request(B+path,method=method)
    with urllib.request.urlopen(req,timeout=4) as r:
        return json.load(r)
class Sock:
    def __init__(s,url): s.ws=connect(url,max_size=50_000_000,open_timeout=4,close_timeout=1); s.n=0
    def call(s,method,params=None,sid=None,timeout=8):
        s.n+=1; m={"id":s.n,"method":method,"params":params or {}}
        if sid: m["sessionId"]=sid
        s.ws.send(json.dumps(m)); t0=time.time()
        while time.time()-t0<timeout:
            try: r=json.loads(s.ws.recv(timeout=timeout))
            except TimeoutError: break
            if r.get("id")==s.n:
                if "error" in r: raise RuntimeError(str(r["error"])[:160])
                return r.get("result",{})
        raise TimeoutError(method)
    def close(s):
        try: s.ws.close()
        except Exception: pass
def dom_match(cookie_domain,site_domains):
    cd=cookie_domain.lower().lstrip(".")
    for d in site_domains:
        if d.startswith("."):
            base=d[1:].lower()
            if cd==base or cd.endswith("."+base): return True
        elif cd==d.lower(): return True
    return False
def dig(obj,path):
    cur=obj
    for part in (path or "").split("."):
        if not part: continue
        if isinstance(cur,dict) and part in cur: cur=cur[part]
        else: return "__MISSING__"
    return cur
JS=r"""(async()=>{const u=%s;try{const r=await fetch(u,{credentials:'include',headers:{'accept':'application/json'},redirect:'manual'});const t=await r.text();let b=null;try{b=JSON.parse(t)}catch(e){}return JSON.stringify({status:r.status,type:r.type,json:b,head:b?null:t.slice(0,120)})}catch(e){return JSON.stringify({error:String(e).slice(0,160)})}})()"""
def fetch_in(sock,sid,url):
    r=sock.call("Runtime.evaluate",{"expression":JS%json.dumps(url),"awaitPromise":True,"returnByValue":True},sid=sid,timeout=12)
    v=r.get("result",{}).get("value")
    return json.loads(v) if v else {"error":"no value"}
def read_probe(body,probe):
    if not isinstance(body,dict): return {"code":"__NOBODY__"}
    out={"code":dig(body,probe.get("code_path") or "")}
    if probe.get("nickname_path"):
        v=dig(body,probe["nickname_path"]); out["nickname"]=v if isinstance(v,(str,int)) else None
    if probe.get("uid_path"):
        v=dig(body,probe["uid_path"]); out["uid"]=str(v) if isinstance(v,(str,int)) and v!="__MISSING__" else None
    disp={}
    for k,pth in (probe.get("display_paths") or {}).items():
        v=dig(body,pth)
        if isinstance(v,(str,int,float,bool)) and v!="__MISSING__": disp[k]=v
    if disp: out["display"]=disp
    return out
out={"action":P["action"],"sites":{},"error":None}
try:
    ver=http("/json/version"); out["chrome"]=ver.get("Browser","")
    targets=[t for t in http("/json/list") if t.get("type")=="page"]
    out["targets"]=len(targets)
    if P["action"]=="open":
        site=P["sites"][0]
        t=http("/json/new?"+urllib.parse.quote(site["login_url"],safe=""),"PUT")
        try: http("/json/activate/"+t["id"])
        except Exception: pass
        out["sites"][site["key"]]={"opened":True,"target":t.get("id")}
        raise SystemExit(0)
    bws=Sock(ver["webSocketDebuggerUrl"])
    try: cookies=bws.call("Storage.getCookies").get("cookies",[])
    finally: bws.close()
    if P["action"]=="logout":
        page=targets[0] if targets else http("/json/new?about:blank","PUT")
        created=None if targets else page
        ps=Sock(page["webSocketDebuggerUrl"])
        try:
            for site in P["sites"]:
                n=0
                for c in cookies:
                    if dom_match(c.get("domain",""),site["logout_domains"]):
                        try: ps.call("Network.deleteCookies",{"name":c["name"],"domain":c["domain"],"path":c.get("path","/")}); n+=1
                        except Exception: pass
                out["sites"][site["key"]]={"deleted":n}
        finally:
            ps.close()
            if created:
                try: http("/json/close/"+created["id"])
                except Exception: pass
        raise SystemExit(0)
    level=int(P.get("level",1))
    for site in P["sites"]:
        mine=[c for c in cookies if dom_match(c.get("domain",""),site["cookie_domains"])]
        names={}
        for c in mine:
            e=c.get("expires",-1)
            names.setdefault(c["name"],[]).append(None if (e is None or e<0) else round(e))
        missing=[n for n in site["session_cookies"] if n not in names]
        expired=[n for n in site["session_cookies"] if n in names and all(x is not None and x<NOW+3600 for x in names[n])]
        earliest=min([x for n in site["session_cookies"] for x in names.get(n,[]) if x],default=None)
        rec={"cookie_ok":not missing and not expired,"missing":missing,"expired":expired,"cookie_count":len(mine),
             "session_cookies":{n:names.get(n) for n in site["session_cookies"]},"earliest_expiry":earliest}
        if level>=2 and rec["cookie_ok"] and site.get("session_probe"):
            host=site["host"]
            tab=next((t for t in targets if (t.get("url","").split("/")+["",""])[2].endswith(host)),None)
            created=None; sock=None; sid=None
            try:
                if tab:
                    sock=Sock(tab["webSocketDebuggerUrl"]); rec["via"]="tab"
                else:
                    bws=Sock(ver["webSocketDebuggerUrl"])
                    created=bws.call("Target.createTarget",{"url":site["session_probe"]["url"],"background":True})["targetId"]
                    sid=bws.call("Target.attachToTarget",{"targetId":created,"flatten":True})["sessionId"]
                    sock=bws; rec["via"]="background"
                    t0=time.time()
                    while time.time()-t0<6:
                        st=sock.call("Runtime.evaluate",{"expression":"document.readyState","returnByValue":True},sid=sid).get("result",{}).get("value")
                        if st=="complete": break
                        time.sleep(0.3)
                res=fetch_in(sock,sid,site["session_probe"]["url"])
                rec["probe"]={"status":res.get("status"),"error":res.get("error"),**read_probe(res.get("json"),site["session_probe"])}
                if site.get("profile_probe") and P.get("profile"):
                    res2=fetch_in(sock,sid,site["profile_probe"]["url"])
                    rec["profile"]={"status":res2.get("status"),"error":res2.get("error"),**read_probe(res2.get("json"),site["profile_probe"])}
            except Exception as e:
                rec["probe"]={"error":f"{type(e).__name__}: {str(e)[:120]}"}
            finally:
                try:
                    if created and sock: sock.call("Target.closeTarget",{"targetId":created})
                except Exception: pass
                if sock: sock.close()
        out["sites"][site["key"]]=rec
except SystemExit:
    pass
except Exception as e:
    out["error"]=f"{type(e).__name__}: {str(e)[:200]}"
print(json.dumps(out,ensure_ascii=False,separators=(",",":")))
'''


def build_payload(action: str, sites: list[dict], *, level: int = 1, profile: bool = False) -> dict:
    return {"action": action, "sites": sites, "level": level, "profile": profile}


def build_script(payload: dict) -> str:
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    return SCRIPT.replace("__PAYLOAD__", encoded)


def build_command(payload: dict) -> str:
    """One shell line: the no-op prefix lets the action server classify it."""
    script = base64.b64encode(build_script(payload).encode()).decode()
    return f": obx-login-{payload['action']}; printf %s {script} | base64 -d | python3"


# ── Pure judgement helpers (unit tested) ───────────────────────────────────
@dataclass
class SiteVerdict:
    key: str
    #: bound | expired | unknown
    status: str
    cookie_ok: bool
    reason: str = ""
    nickname: str | None = None
    uid: str | None = None
    earliest_expiry: int | None = None
    display: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)


def judge_probe(probe: dict | None, site_probe) -> str:
    """ok | expired | unknown from one level-2 response, per the site rule."""
    if not probe or probe.get("error"):
        return "unknown"
    code = probe.get("code", "__MISSING__")
    if code == "__MISSING__":
        code = None
    if site_probe is None:
        return "unknown"
    if code in site_probe.ok_values:
        return "ok"
    if code in site_probe.expired_values:
        return "expired"
    return "unknown"


def judge_site(site, rec: dict) -> SiteVerdict:
    """Combine the cookie snapshot and (optional) level-2 result into a verdict."""
    cookie_ok = bool(rec.get("cookie_ok"))
    verdict = SiteVerdict(
        key=site.key,
        status="unknown",
        cookie_ok=cookie_ok,
        earliest_expiry=rec.get("earliest_expiry"),
        detail={
            "cookie_count": rec.get("cookie_count"),
            "session_cookies": rec.get("session_cookies"),
            "missing": rec.get("missing"),
            "expired": rec.get("expired"),
            "via": rec.get("via"),
        },
    )
    probe = rec.get("probe")
    probed = judge_probe(probe, site.session_probe) if probe else None
    if probed == "expired":
        verdict.status, verdict.reason = "expired", f"session probe: {probe.get('code')}"
    elif probed == "ok":
        verdict.status, verdict.reason = "bound", "session probe ok"
    elif not site.session_cookies:
        verdict.status, verdict.reason = "unknown", "site not reconnoitred"
    elif not cookie_ok:
        verdict.status = "expired"
        verdict.reason = "session cookies missing: " + ",".join(rec.get("missing") or rec.get("expired") or [])
    else:
        verdict.status, verdict.reason = "bound", "session cookies present"
    for source in (probe, rec.get("profile")):
        if not source or source.get("error"):
            continue
        if source.get("nickname") and not verdict.nickname:
            verdict.nickname = str(source["nickname"])
        if source.get("uid") and not verdict.uid:
            verdict.uid = str(source["uid"])
        if source.get("display"):
            verdict.display.update(source["display"])
    if probe:
        verdict.detail["probe"] = {k: v for k, v in probe.items() if k in ("status", "code", "error")}
    return verdict


def parse_output(stdout: str) -> dict:
    """The last JSON object the script printed, or raise."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError("desktop login script printed no JSON")


def now_ts() -> float:
    return time.time()
