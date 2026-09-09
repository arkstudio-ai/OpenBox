"""Load one page in the desktop's Chrome and evaluate JavaScript in it.

The login-state probe (`platforms.desktop.cdp`) deliberately never navigates.
Hot-list collection must: 热点宝's data endpoints are signed with page-issued
parameters (msToken / X-Bogus / _signature), so the only honest way to call
them is from inside the loaded page, replaying a URL the page itself signed.
Measured 2026-09-10: the signature covers the URL only, so a POST to a
captured URL with our own JSON body (window, board, page size, category
tags) is accepted.

Same delivery shape as `cdp.py`: a self-contained Python script, base64'd
into one shell line, run *on the desktop* against Chrome's debug port. It
opens a background target, waits for `document.readyState == "complete"` and,
optionally, for a resource whose URL matches `wait_resource`, evaluates the
caller's expression (which must resolve to a JSON string), and always closes
the target it created. The desktop user sees nothing.

Pure helpers (`build_command`, `parse_output`) are unit tested; the script is
exercised on a real desktop.
"""
from __future__ import annotations

import base64
import json

SCRIPT = r'''import json,sys,time,urllib.request
from websockets.sync.client import connect
P=json.loads(__import__("base64").b64decode("__PAYLOAD__").decode())
B="http://127.0.0.1:9333"
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
def ev(sock,sid,expr,timeout=10,await_promise=False):
    r=sock.call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_promise},sid=sid,timeout=timeout)
    if r.get("exceptionDetails"):
        d=r["exceptionDetails"]; raise RuntimeError("js: "+str((d.get("exception") or {}).get("description") or d.get("text"))[:200])
    return r.get("result",{}).get("value")
out={"action":"page_eval","ok":False,"error":None}
created=None; sock=None
try:
    ver=http("/json/version"); out["chrome"]=ver.get("Browser","")
    sock=Sock(ver["webSocketDebuggerUrl"])
    created=sock.call("Target.createTarget",{"url":P["url"],"background":True})["targetId"]
    sid=sock.call("Target.attachToTarget",{"targetId":created,"flatten":True})["sessionId"]
    t0=time.time(); deadline=t0+float(P.get("timeout_s",40))
    host=P["url"].split("/")[2].lower()
    while time.time()<deadline:
        # The fresh target starts as about:blank, which is already "complete":
        # wait until the real document is loaded.
        try:
            if ev(sock,sid,"location.host.toLowerCase()+' '+document.readyState")==host+" complete": break
        except Exception: pass
        time.sleep(0.4)
    out["load_s"]=round(time.time()-t0,1)
    wr=P.get("wait_resource")
    if wr:
        expr="performance.getEntriesByType('resource').some(e=>new RegExp(%s).test(e.name))"%json.dumps(wr)
        seen=False
        while time.time()<deadline:
            try:
                if ev(sock,sid,expr): seen=True; break
            except Exception: pass
            time.sleep(0.5)
        out["resource_seen"]=seen
    time.sleep(float(P.get("settle_s",1.0)))
    out["final_url"]=ev(sock,sid,"location.href")
    out["title"]=ev(sock,sid,"document.title")
    v=ev(sock,sid,P["expression"],timeout=float(P.get("eval_timeout_s",25)),await_promise=True)
    out["value"]=json.loads(v) if isinstance(v,str) else v
    out["ok"]=True
except Exception as e:
    out["error"]=f"{type(e).__name__}: {str(e)[:220]}"
finally:
    try:
        if created and sock: sock.call("Target.closeTarget",{"targetId":created})
    except Exception: pass
    if sock: sock.close()
print(json.dumps(out,ensure_ascii=False,separators=(",",":")))
'''


def build_payload(*, url: str, expression: str, wait_resource: str | None = None, timeout_s: float = 40,
                  settle_s: float = 1.0, eval_timeout_s: float = 25) -> dict:
    return {"action": "page_eval", "url": url, "expression": expression, "wait_resource": wait_resource,
            "timeout_s": timeout_s, "settle_s": settle_s, "eval_timeout_s": eval_timeout_s}


def build_script(payload: dict) -> str:
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    return SCRIPT.replace("__PAYLOAD__", encoded)


def build_command(payload: dict) -> str:
    """One shell line; the no-op prefix lets the action server classify it."""
    script = base64.b64encode(build_script(payload).encode()).decode()
    return f": obx-trends-page-eval; printf %s {script} | base64 -d | python3"


def parse_output(stdout: str) -> dict:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError("desktop page script printed no JSON")
