#!/usr/bin/env python3
"""GPU-VRAM-driven chassis fan controller for ASUS Pro WS WRX80E-SAGE SE (MegaRAC BMC).
Writes the CHA_FAN1 PWM zone based on max GPU VRAM temp.
User curve: maxVRAM <= 40C -> 15% ; >= 80C -> 100% (linear between).
No BMC automatic fallback exists, so on telemetry loss or clean stop -> 100% (fail-safe).
Run as root (gputemps requires root). Credentials via env only (never stored)."""
import ssl, os, sys, json, time, signal, subprocess, urllib.request, urllib.parse, http.cookiejar

BMC_IP  = os.environ.get("BMC_IP", "192.168.3.4")
USER    = os.environ.get("BMC_USER")
PASS    = os.environ.get("BMC_PASS")
GPU_BIN = os.environ.get("GPU_BIN", "/home/pepkos/gddr6-core-junction-vram-temps/gputemps")
ZONE    = int(os.environ.get("ZONE", "1"))          # zone 2 = "Zone2(CHA_FAN 1)"
INTERVAL= float(os.environ.get("INTERVAL", "1"))
MIN_DUTY= int(os.environ.get("MIN_DUTY", "15"))
MAX_DUTY= int(os.environ.get("MAX_DUTY", "100"))
FAIL_DUTY=int(os.environ.get("FAIL_DUTY", "100"))   # telemetry lost -> safe HIGH
RATE_UP = int(os.environ.get("RATE_UP", "12"))       # max %/cycle increase
RATE_DN = int(os.environ.get("RATE_DN", "5"))        # max %/cycle decrease
DEADBAND= int(os.environ.get("DEADBAND", "2"))       # hold if within +-2%
STALE_S = float(os.environ.get("STALE_S", "5"))      # max age of telemetry
MIN_GPUS= int(os.environ.get("MIN_GPUS", "1"))
ON_EXIT = os.environ.get("ON_EXIT", "high")          # high|keep on clean exit
APPLY   = "--apply" in sys.argv
ITERS   = 0
if "--iterations" in sys.argv:
    ITERS = int(sys.argv[sys.argv.index("--iterations")+1])

# maxVRAM (C) -> duty: <=40 -> 15%, >=80 -> 100%, linear between.
VRAM_CURVE = [(60, MIN_DUTY), (70, 50), (80, 100)]

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(cj))
_tok=[None]

def bmc(method, path, data=None, ct=None):
    body=None
    if data is not None:
        body=json.dumps(data).encode() if ct=="json" else urllib.parse.urlencode(data).encode()
    r=urllib.request.Request(f"https://{BMC_IP}{path}", data=body, method=method)
    r.add_header("X-Requested-With","XMLHttpRequest")
    if ct=="json": r.add_header("Content-Type","application/json")
    elif ct=="form": r.add_header("Content-Type","application/x-www-form-urlencoded")
    if _tok[0]: r.add_header("X-CSRFTOKEN",_tok[0])
    return op.open(r, timeout=15).read().decode()

def login():
    _tok[0]=None; cj.clear()
    r=json.loads(bmc("POST","/api/session",{"username":USER,"password":PASS},ct="form"))
    assert r.get("ok")==0, "bmc login failed: "+json.dumps(r)
    _tok[0]=r["CSRFToken"]

def read_gpu():
    out=subprocess.check_output([GPU_BIN,"--json","--once"], timeout=8).decode().strip()
    d=json.loads(out)
    gpus=d["gpus"]; ts=d["timestamp"]
    if len(gpus) < MIN_GPUS: raise ValueError("too few gpus")
    for g in gpus:
        for k in ("core","junction","vram"):
            v=g[k]
            if not isinstance(v,int) or v<5 or v>120: raise ValueError("bad %s=%s"%(k,v))
    age=time.time()-ts
    if age > STALE_S: raise ValueError("stale ts age=%.1f"%age)
    return ts, [g["vram"] for g in gpus]

def interp(curve,x):
    if x<=curve[0][0]: return curve[0][1]
    if x>=curve[-1][0]: return curve[-1][1]
    for (t0,d0),(t1,d1) in zip(curve,curve[1:]):
        if t0<=x<=t1: return d0+(d1-d0)*(x-t0)/(t1-t0)
    return curve[-1][1]

def set_duty(duty):
    d=int(max(MIN_DUTY,min(MAX_DUTY,round(duty))))
    payload={"PWMIndex":ZONE,"PWMSrc":0,
             "CurrentPWMdata":["0",str(d),"20",str(d),"65",str(d),"70",str(d),100,d]}
    bmc("PUT","/api/fanctrl/PWM",payload,ct="json")
    return d

current=MIN_DUTY; consec_bad=0; stopped=False
def handle(sig,frm):
    global stopped; stopped=True
signal.signal(signal.SIGINT,handle); signal.signal(signal.SIGTERM,handle)

if USER is None or PASS is None:
    sys.exit("set BMC_USER and BMC_PASS env vars")
login()

def log(msg): print(time.strftime("%H:%M:%S"), msg, flush=True)
log(f"controller start zone={ZONE} apply={APPLY} min={MIN_DUTY} max={MAX_DUTY} fail={FAIL_DUTY} interval={INTERVAL}s curve={VRAM_CURVE}")

while not stopped:
    cycle_start=time.time()
    try:
        ts,vrams=read_gpu(); consec_bad=0
        mv=max(vrams)
        target=int(max(MIN_DUTY,min(MAX_DUTY,round(interp(VRAM_CURVE,mv)))))
        if abs(target-current)<=DEADBAND:
            desired=current
        elif target>current:
            desired=min(target,current+RATE_UP)
        else:
            desired=max(target,current-RATE_DN)
        status="ok"
    except Exception as e:
        consec_bad+=1
        if consec_bad>=2:
            desired=FAIL_DUTY; status="FAILSAFE(%s)"%e
        else:
            desired=current; status="warn(%s)"%e
        mv=None
    if desired!=current:
        if APPLY:
            try:
                if consec_bad and desired==FAIL_DUTY: login()
                current=set_duty(desired)
            except Exception as e:
                try: login(); current=set_duty(desired)
                except Exception as e2: log("WRITE-FAIL %s (keeping duty=%d)"%(e2,current))
        else:
            current=desired
    log("maxVRAM=%s duty=%d%s [%s]"%(mv,current,"(DRY)" if not APPLY else "",status))
    if ITERS:
        ITERS-=1
        if ITERS<=0: break
    time.sleep(max(0,INTERVAL-(time.time()-cycle_start)))

if APPLY:
    try:
        if ON_EXIT=="high":
            login(); set_duty(FAIL_DUTY); log("on-exit set duty=%d (high/failsafe)"%FAIL_DUTY)
        else:
            log("on-exit kept duty=%d"%current)
    except Exception as e:
        log("on-exit write failed: %s"%e)
else:
    log("dry-run: no hardware writes performed")
