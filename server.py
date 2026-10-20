#!/usr/bin/env python3
"""GPU Switchboard: one NVIDIA GPU, one model at a time, switched from a web dashboard.

Stdlib only. Engines run as Docker containers; models download on demand from Hugging Face.

  python3 server.py                 serve the dashboard (SB_PORT, default 8090)
  python3 server.py set-password    set the dashboard password (stored hashed in data/config.json)

API (all behind the dashboard password; POSTs need the X-Requested-With header):
  GET  /api/state       GPU, disk, current mode, models, download progress, log
  POST /api/switch      {"to": "<llm id>" | "comfy"}   downloads missing files first
  POST /api/stop        free the GPU
  POST /api/install     {"id": ...}   download only
  POST /api/uninstall   {"id": ...}   delete files no other installed model uses
  POST /api/token       {"token": ...}   Hugging Face token for gated models
"""
import base64, getpass, hashlib, hmac, json, os, secrets, shutil, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get("SB_DATA", os.path.join(ROOT, "data")))
PORT = int(os.environ.get("SB_PORT", "8090"))
CONFIG = os.path.join(DATA, "config.json")
RECIPES = json.load(open(os.path.join(ROOT, "recipes.json")))
IMAGES = RECIPES["images"]
MODELS = {m["id"]: {**m, "group": "llm"} for m in RECIPES["llm"]} | {m["id"]: {**m, "group": "comfy"} for m in RECIPES["comfy"]}
PORTS = {"llm": 8000, "comfy": 8188, "webui": 3000}
HEALTH = {"llm": f"http://127.0.0.1:{PORTS['llm']}/v1/models", "comfy": f"http://127.0.0.1:{PORTS['comfy']}/"}

state = {"busy": None, "target": None, "error": None, "log": [], "dl": None}
lock = threading.Lock()


# ---------- config ----------
def config():
    try:
        return json.load(open(CONFIG))
    except (OSError, ValueError):
        return {}


def save_config(**kv):
    os.makedirs(DATA, exist_ok=True)
    c = {**config(), **kv}
    with open(CONFIG + ".tmp", "w") as f:
        json.dump(c, f)
    os.chmod(CONFIG + ".tmp", 0o600)
    os.replace(CONFIG + ".tmp", CONFIG)


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def log(line):
    state["log"] = (state["log"] + [time.strftime("%H:%M:%S ") + line])[-200:]
    print(line, flush=True)


# ---------- files ----------
def expand(spec):
    """One recipe file spec -> [(url, absolute dest path, size or None, replace or None)]."""
    if "repo" in spec:
        tree = json.load(hf_open(f"https://huggingface.co/api/models/{spec['repo']}/tree/main?recursive=true"))
        return [(f"https://huggingface.co/{spec['repo']}/resolve/main/{urllib.parse.quote(f['path'])}",
                 os.path.join(DATA, "models", spec["repo"], f["path"]), f.get("size"), None)
                for f in tree if f["type"] == "file"]
    to = spec.get("to") or os.path.join("comfyui/basedir/models", spec["dir"], spec["url"].rsplit("/", 1)[-1])
    return [(spec["url"], os.path.join(DATA, to), None, spec.get("replace"))]


def marker(m):
    return os.path.join(DATA, "installed", m["id"])


def installed(m):
    return os.path.exists(marker(m))


def local_paths(m):
    """Paths a model owns on disk, without touching the network: repo dirs and single files."""
    out = []
    for s in m["files"]:
        out.append(os.path.join(DATA, "models", s["repo"]) if "repo" in s else expand(s)[0][1])
    return out


class NoAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """HF redirects downloads to a CDN with a presigned URL. Don't send the token there."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new and urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


opener = urllib.request.build_opener(NoAuthOnRedirect)


def hf_open(url, start=0):
    req = urllib.request.Request(url, headers={"User-Agent": "gpu-switchboard"})
    if config().get("hf_token"):
        req.add_header("Authorization", "Bearer " + config()["hf_token"])
    if start:
        req.add_header("Range", f"bytes={start}-")
    return opener.open(req, timeout=60)


def fetch(url, dest, replace, mid):
    if os.path.exists(dest):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    try:
        r = hf_open(url, have)
    except urllib.error.HTTPError as e:
        if e.code != 416:   # 416 = the .part is already complete
            raise
        os.replace(part, dest)
        return
    with r:
        if r.status == 200:
            have = 0   # server ignored Range, start over
        total = have + int(r.headers.get("Content-Length") or 0)
        state["dl"] = {"id": mid, "file": os.path.basename(dest), "done": have, "total": total}
        with open(part, "ab" if have else "wb") as f:
            while chunk := r.read(8 << 20):
                f.write(chunk)
                state["dl"]["done"] += len(chunk)
    if replace:
        s = open(part).read().replace(*replace)
        open(part, "w").write(s)
    os.replace(part, dest)


def download(m):
    """Fetch every missing file of a model. Raises with a readable message on failure."""
    if installed(m):
        return
    need = m.get("disk_gb", 0) * 1e9
    free = shutil.disk_usage(DATA).free
    if free < need + 5e9:
        raise RuntimeError(f"not enough disk: {m['name']} needs ~{m['disk_gb']} GB, {free / 1e9:.0f} GB free")
    log(f"downloading {m['name']} (~{m.get('disk_gb')} GB)")
    url = ""
    try:
        files = [f for s in m["files"] for f in expand(s)]
        for url, dest, _, replace in files:
            if not os.path.exists(dest):
                log("  get " + os.path.relpath(dest, DATA))
            fetch(url, dest, replace, m["id"])
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            repo = m.get("gated") or url.split("huggingface.co/")[-1].split("/resolve/")[0]
            raise RuntimeError(f"Hugging Face refused the download ({e.code}). Accept the license at https://huggingface.co/{repo} "
                               "and save your Hugging Face token in Settings.") from None
        raise RuntimeError(f"download failed: HTTP {e.code} for {url}") from None
    finally:
        state["dl"] = None
    os.makedirs(os.path.dirname(marker(m)), exist_ok=True)
    open(marker(m), "w").write(time.strftime("%Y-%m-%d"))
    log(f"{m['name']} downloaded")


def uninstall(m):
    keep = {p for o in MODELS.values() if o["id"] != m["id"] and installed(o) for p in local_paths(o)}
    freed = 0
    for p in local_paths(m):
        if p in keep or not os.path.exists(p):
            continue
        if os.path.isdir(p):
            freed += sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)
            shutil.rmtree(p)
        else:
            freed += os.path.getsize(p)
            os.remove(p)
    if os.path.exists(marker(m)):
        os.remove(marker(m))
    log(f"removed {m['name']}, freed {freed / 1e9:.1f} GB")


# ---------- docker ----------
def sh(*cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-600:])
    return r.stdout.strip()


def running():
    """{'llm': id} / {'comfy': 'comfy'} for our containers that are up."""
    out = sh("docker", "ps", "--filter", "label=sb.mode", "--format", '{{.Names}} {{.Label "sb.mode"}}', check=False)
    return {name.removeprefix("sb-"): mode for name, mode in (l.split() for l in out.splitlines() if l.strip())}


def up(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def stop_gpu():
    sh("docker", "stop", "-t", "30", "sb-llm", "sb-comfy", check=False)   # ComfyUI needs >10s to release VRAM cleanly
    sh("docker", "rm", "-f", "sb-llm", "sb-comfy", check=False)
    time.sleep(3)


def run_cmd(mid):
    os.makedirs(os.path.join(DATA, "models"), exist_ok=True)
    if mid == "comfy":
        for d in ("comfyui/run", "comfyui/basedir"):
            os.makedirs(os.path.join(DATA, d), exist_ok=True)
        return ["docker", "run", "-d", "--name", "sb-comfy", "--label", "sb.mode=comfy", "--gpus", "all", "-p", f"{PORTS['comfy']}:8188",
                "-v", f"{DATA}/comfyui/run:/comfy/mnt", "-v", f"{DATA}/comfyui/basedir:/basedir",
                "-e", f"WANTED_UID={os.getuid()}", "-e", f"WANTED_GID={os.getgid()}", "-e", "BASE_DIRECTORY=/basedir",
                "-e", "SECURITY_LEVEL=normal", "-e", "USE_UV=true", IMAGES["comfyui"]]
    m = MODELS[mid]
    base = ["docker", "run", "-d", "--name", "sb-llm", "--label", f"sb.mode={mid}", "--gpus", "all", "--ipc", "host",
            "-p", f"{PORTS['llm']}:8000", "-v", f"{DATA}/models:/models:ro"]
    if m["engine"] == "vllm":
        os.makedirs(os.path.join(DATA, "cache/vllm"), exist_ok=True)
        return base + ["-v", f"{DATA}/cache/vllm:/root/.cache/vllm", "-e", "HF_HUB_OFFLINE=1", IMAGES["vllm"],
                       "--model", f"/models/{m['files'][0]['repo']}", "--served-model-name", mid, *m["args"]]
    return base + [IMAGES["llamacpp"], *m["args"], "--alias", mid, "--host", "0.0.0.0", "--port", "8000"]


def switch(mid):
    group = "comfy" if mid == "comfy" else "llm"
    name = "ComfyUI" if mid == "comfy" else MODELS[mid]["name"]
    if mid != "comfy":
        download(MODELS[mid])
    log("stopping whatever holds the GPU")
    stop_gpu()
    cmd = run_cmd(mid)
    image = next(a for a in cmd if a in IMAGES.values())
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        log(f"pulling {image} (one time, several GB; vLLM is ~28 GB)")
        sh("docker", "pull", "-q", image)
    log(f"starting {name}")
    cid = sh(*cmd)
    # ComfyUI installs itself on the first start (several minutes); big vLLM models compile on theirs.
    limit = 1800 if group == "comfy" else 1200
    deadline = time.time() + limit
    while not up(HEALTH[group]):
        if sh("docker", "inspect", "-f", "{{.State.Running}}", cid, check=False) != "true":
            r = subprocess.run(["docker", "logs", "--tail", "25", cid], capture_output=True, text=True)
            tail = r.stdout + r.stderr
            raise RuntimeError(f"{name} exited while loading. Last log lines:\n{tail[-1500:]}")
        if time.time() > deadline:
            raise RuntimeError(f"{name} did not answer within {limit // 60} min; see docker logs {cid[:12]}")
        time.sleep(5)
    log(f"{name} is ready on port {PORTS[group]}")


def job(kind, mid, fn):
    """Run one long task at a time in the background; the dashboard polls /api/state."""
    with lock:
        if state["busy"]:
            raise RuntimeError(f"busy: {state['busy']} {state['target']}")
        state.update(busy=kind, target=mid, error=None)

    def work():
        try:
            fn()
        except Exception as e:
            state["error"] = str(e)
            log("ERROR: " + str(e))
        finally:
            state.update(busy=None, target=None)
    threading.Thread(target=work, daemon=True).start()


# ---------- status ----------
def gpu():
    try:
        out = sh("nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits")
        name, used, total, util, temp, power = [x.strip() for x in out.splitlines()[0].split(",")]
        return {"name": name, "mem_used": int(used), "mem_total": int(total), "util": int(util), "temp": int(temp), "power": round(float(power))}
    except Exception:
        return None


def snapshot():
    g, run = gpu(), running()
    mode = run.get("llm") or ("comfy" if "comfy" in run else None)
    healthy = bool(mode) and up(HEALTH["comfy" if mode == "comfy" else "llm"])
    du = shutil.disk_usage(DATA if os.path.isdir(DATA) else "/")
    tier = None if not g else 24 if g["mem_total"] >= 22000 else 16 if g["mem_total"] >= 15000 else 12
    models = [{k: v for k, v in m.items() if k not in ("files", "args")} | {"installed": installed(m)} for m in MODELS.values()]
    return {"gpu": g, "tier": tier, "mode": mode, "healthy": healthy, "disk": {"free_gb": round(du.free / 1e9), "total_gb": round(du.total / 1e9)},
            "models": models, "ports": PORTS, "hf_token": bool(config().get("hf_token")), "now": time.time(),
            **{k: state[k] for k in ("busy", "target", "error", "log", "dl")}}


# ---------- http ----------
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        c = config()
        if "password_hash" not in c:
            return True   # installer always sets one; a bare dev checkout runs open
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, pw = base64.b64decode(auth[6:]).decode().split(":", 1)
                if hmac.compare_digest(hash_pw(pw, c["password_salt"]), c["password_hash"]):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="GPU Switchboard"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self.authed():
            return
        path = self.path.split("?")[0]
        if path == "/api/state":
            return self.send(200, snapshot())
        if path in STATIC:
            f, ctype = STATIC[path]
            return self.send(200, open(os.path.join(ROOT, "web", f), "rb").read(), ctype)
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.authed():
            return
        if self.headers.get("X-Requested-With") != "switchboard":   # blocks cross-site form posts
            return self.send(403, {"error": "missing X-Requested-With"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            path = self.path.split("?")[0]
            mid = body.get("to") or body.get("id")
            if path == "/api/token":
                save_config(hf_token=body.get("token", "").strip())
                return self.send(200, {"ok": True})
            if path == "/api/stop":
                job("stopping", None, stop_gpu)
            elif mid != "comfy" and mid not in MODELS:
                return self.send(400, {"error": f"unknown model {mid!r}"})
            elif path == "/api/switch":
                if mid != "comfy" and MODELS[mid]["group"] != "llm":
                    return self.send(400, {"error": "image models run inside ComfyUI; switch to comfy"})
                job("switching", mid, lambda: switch(mid))
            elif path == "/api/install":
                job("downloading", mid, lambda: download(MODELS[mid]))
            elif path == "/api/uninstall":
                if running().get("llm") == mid:
                    return self.send(409, {"error": "that model is running; switch away first"})
                job("removing", mid, lambda: uninstall(MODELS[mid]))
            else:
                return self.send(404, {"error": "not found"})
            self.send(202, {"ok": True})
        except RuntimeError as e:
            self.send(409, {"error": str(e)})
        except (ValueError, KeyError) as e:
            self.send(400, {"error": str(e)})

    def log_message(self, *a):
        pass


def set_password():
    pw = os.environ.get("SB_PASSWORD") or getpass.getpass("Dashboard password: ")
    if len(pw) < 8:
        sys.exit("Use at least 8 characters.")
    salt = secrets.token_hex(16)
    save_config(password_salt=salt, password_hash=hash_pw(pw, salt))
    print("Password saved to " + CONFIG)


if __name__ == "__main__":
    if sys.argv[1:] == ["set-password"]:
        set_password()
    else:
        os.makedirs(DATA, exist_ok=True)
        print(f"GPU Switchboard on :{PORT}, data in {DATA}", flush=True)
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
