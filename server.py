#!/usr/bin/env python3
"""RigUp: one NVIDIA GPU, one model at a time, switched from a web dashboard.

Stdlib only. Engines run as Docker containers; models download on demand from Hugging Face.

  python3 server.py                 serve the dashboard (RIGUP_PORT, default 8090)
  python3 server.py set-password    set the dashboard password (stored hashed in data/config.json)

API (all behind the dashboard password; POSTs need the X-Requested-With header):
  GET  /api/state       GPU, disk, current mode, models, download progress, log
  POST /api/switch      {"to": "<llm id>" | "comfy"}   downloads missing files first
  POST /api/stop        free the GPU
  POST /api/install     {"id": ...}   download only
  POST /api/uninstall   {"id": ...}   delete files no other installed model uses
  POST /api/token       {"hf_token": ...} and/or {"api_key": ...}   Hugging Face token; key the LLM API requires (empty = none)
  POST /api/autotune    check for new releases, then find the best settings for each installed Lab model on this GPU
"""
import base64, getpass, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get("RIGUP_DATA", os.path.join(ROOT, "data")))
PORT = int(os.environ.get("RIGUP_PORT", "8090"))
CONFIG = os.path.join(DATA, "config.json")
CATALOG = os.path.join(DATA, "catalog.json")     # newest recipes.json from GitHub, fetched by auto-tune
RELEASES = os.path.join(DATA, "releases.json")   # recent Hugging Face models from the watch list
TUNED = os.path.join(DATA, "tuned.json")         # auto-tune results for this GPU
REPO = "guscanales/rigup"
MAC = sys.platform == "darwin"   # Apple Silicon: llama.cpp (Metal) as a native process instead of Docker, LLMs only for now
ENGINE = os.path.join(DATA, "engine.json")    # macOS: pid and model of the running engine
ENGINE_LOG = os.path.join(DATA, "engine.log")
LLAMA_SERVER = shutil.which("llama-server") or "/opt/homebrew/bin/llama-server"
TIERS = [16, 24, 32, 48, 64, 128] if MAC else [24, 16, 12]   # GB of unified memory on a Mac, of VRAM on NVIDIA
GROUPS = {"mac": "llm"} if MAC else {"llm": "llm", "comfy": "comfy"}   # recipes.json list -> dashboard group
PORTS = {"llm": 8000} if MAC else {"llm": 8000, "comfy": 8188, "webui": 3000}
HEALTH = {"llm": "http://127.0.0.1:8000/v1/models", "comfy": "http://127.0.0.1:8188/"}



def read_json(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def read_text(path):
    try:
        return open(path, errors="replace").read()
    except OSError:
        return ""


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(path + ".tmp", path)


def load_catalog():
    """recipes.json from this checkout, plus models that only the newer GitHub catalog has (marked new).
    Existing recipes change only with `git pull`, so a fetched catalog never overrides what you run today."""
    global RECIPES, IMAGES, MODELS
    RECIPES = json.load(open(os.path.join(ROOT, "recipes.json")))
    IMAGES = RECIPES["images"]
    MODELS = {m["id"]: {**m, "group": g} for key, g in GROUPS.items() for m in RECIPES.get(key, [])}
    up = read_json(CATALOG, {})
    for key, g in GROUPS.items():
        for m in up.get(key, []):
            if m["id"] not in MODELS:
                image = up.get("images", {}).get(m.get("engine") or "comfyui")
                MODELS[m["id"]] = {**m, "group": g, "new": True, **({"image": image} if image and not MAC else {})}


load_catalog()

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
    req = urllib.request.Request(url, headers={"User-Agent": "rigup"})
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
def sh(*cmd, check=True, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-600:])
    return r.stdout.strip()


def running():
    """{'llm': id} / {'comfy': 'comfy'} for our containers that are up."""
    if MAC:
        e = read_json(ENGINE, {})
        cmd = sh("ps", "-o", "command=", "-p", str(e.get("pid", 0)), check=False)
        return {"llm": e["id"]} if "llama-server" in cmd else {}
    out = sh("docker", "ps", "--filter", "label=rigup.mode", "--format", '{{.Names}} {{.Label "rigup.mode"}}', check=False)
    return {name.removeprefix("rigup-"): mode for name, mode in (l.split() for l in out.splitlines() if l.strip())}


def local(url, body=None):
    """Request to our own engines, with the LLM API key when one is set."""
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    if config().get("api_key"):
        req.add_header("Authorization", "Bearer " + config()["api_key"])
    return req


def up(url):
    try:
        with urllib.request.urlopen(local(url), timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def stop_gpu():
    if MAC:
        if running():
            pid = read_json(ENGINE, {})["pid"]
            os.killpg(pid, 15)
            for _ in range(40):
                if not running():
                    break
                time.sleep(0.5)
            else:
                os.killpg(pid, 9)
        if os.path.exists(ENGINE):
            os.remove(ENGINE)
        return
    sh("docker", "stop", "-t", "30", "rigup-llm", "rigup-comfy", check=False)   # ComfyUI needs >10s to release VRAM cleanly
    sh("docker", "rm", "-f", "rigup-llm", "rigup-comfy", check=False)
    time.sleep(3)


def run_cmd(mid, args=None):
    os.makedirs(os.path.join(DATA, "models"), exist_ok=True)
    if mid == "comfy":
        for d in ("comfyui/run", "comfyui/basedir"):
            os.makedirs(os.path.join(DATA, d), exist_ok=True)
        return ["docker", "run", "-d", "--name", "rigup-comfy", "--label", "rigup.mode=comfy", "--gpus", "all", "-p", f"{PORTS['comfy']}:8188",
                "-v", f"{DATA}/comfyui/run:/comfy/mnt", "-v", f"{DATA}/comfyui/basedir:/basedir",
                "-e", f"WANTED_UID={os.getuid()}", "-e", f"WANTED_GID={os.getgid()}", "-e", "BASE_DIRECTORY=/basedir",
                "-e", "SECURITY_LEVEL=normal", "-e", "USE_UV=true", IMAGES["comfyui"]]
    m = MODELS[mid]
    args = args or args_for(m)
    base = ["docker", "run", "-d", "--name", "rigup-llm", "--label", f"rigup.mode={mid}", "--gpus", "all", "--ipc", "host",
            "-p", f"{PORTS['llm']}:8000", "-v", f"{DATA}/models:/models:ro"]
    if config().get("api_key"):   # both engines read it from the environment, which keeps it out of `ps`
        base += ["-e", "VLLM_API_KEY" if m["engine"] == "vllm" else "LLAMA_API_KEY"]
    if m["engine"] == "vllm":
        os.makedirs(os.path.join(DATA, "cache/vllm"), exist_ok=True)
        return base + ["-v", f"{DATA}/cache/vllm:/root/.cache/vllm", "-e", "HF_HUB_OFFLINE=1", m.get("image") or IMAGES["vllm"],
                       "--model", f"/models/{m['files'][0]['repo']}", "--served-model-name", mid, *args]
    return base + [m.get("image") or IMAGES["llamacpp"], *args, "--alias", mid, "--host", "0.0.0.0", "--port", "8000"]


def start_native(mid, args):
    """macOS: run the engine as a plain process in its own session, so it survives a dashboard restart."""
    m = MODELS[mid]
    models = os.path.join(DATA, "models")
    args = [models + a[len("/models"):] if a.startswith("/models/") else a for a in args]
    cmd = [LLAMA_SERVER, *args, "--alias", mid, "--host", "0.0.0.0", "--port", "8000"]
    with open(ENGINE_LOG, "w") as out:
        key = {"LLAMA_API_KEY": config()["api_key"]} if config().get("api_key") else {}
        p = subprocess.Popen(cmd, cwd=DATA, stdout=out, stderr=subprocess.STDOUT, start_new_session=True, env={**os.environ, **key})
    write_json(ENGINE, {"pid": p.pid, "id": mid})
    return p


def switch(mid, args=None):
    group = "comfy" if mid == "comfy" else "llm"
    name = "ComfyUI" if mid == "comfy" else MODELS[mid]["name"]
    if mid != "comfy":
        download(MODELS[mid])
    log("stopping whatever holds the GPU")
    stop_gpu()
    if MAC:
        log(f"starting {name}")
        p = start_native(mid, args or args_for(MODELS[mid]))
        alive, logs, where = lambda: p.poll() is None, engine_logs, ENGINE_LOG
    else:
        cmd = run_cmd(mid, args)
        images = set(IMAGES.values()) | {m.get("image") for m in MODELS.values()}
        image = next(a for a in cmd if a in images)
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
            log(f"pulling {image} (one time, several GB; vLLM is ~28 GB)")
            sh("docker", "pull", "-q", image)
        log(f"starting {name}")
        key = config().get("api_key", "")
        cid = sh(*cmd, env={**os.environ, "VLLM_API_KEY": key, "LLAMA_API_KEY": key})
        alive = lambda: sh("docker", "inspect", "-f", "{{.State.Running}}", cid, check=False) == "true"

        def logs():
            r = subprocess.run(["docker", "logs", "--tail", "40", cid], capture_output=True, text=True)
            return r.stdout + r.stderr
        where = f"docker logs {cid[:12]}"
    # ComfyUI installs itself on the first start (several minutes); big vLLM models compile on theirs.
    limit = 1800 if group == "comfy" else 1200
    deadline = time.time() + limit
    while not up(HEALTH[group]):
        if not alive():
            raise RuntimeError(f"{name} exited while loading. Last log lines:\n{logs()[-1500:]}")
        if time.time() > deadline:
            raise RuntimeError(f"{name} did not answer within {limit // 60} min; see {where}")
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


# ---------- new releases ----------
TASKS = {None, "text-generation", "image-text-to-text", "any-to-any", "text-to-image", "image-to-image", "text-to-video", "image-to-video"}


def base_name(repo):
    """'unsloth/Foo-27B-GGUF' -> 'foo-27b': one entry per model, whatever the packaging."""
    return re.sub(r"(-(gguf|fp8|nvfp4|awq|gptq|int4|int8|w4a16|bnb-4bit|comfy|mlx.*))+$", "", repo.split("/")[-1].lower())


def check_releases():
    """Pull the newest RigUp catalog (new recipes appear in the dashboard marked new), then list recent
    Hugging Face models from the watch list that have no recipe yet, with a rough size check for this card."""
    log("checking for new releases")
    try:
        req = urllib.request.Request(f"https://raw.githubusercontent.com/{REPO}/main/recipes.json", headers={"User-Agent": "rigup"})
        with urllib.request.urlopen(req, timeout=30) as r:
            write_json(CATALOG, json.load(r))
        load_catalog()
        new = [m["name"] for m in MODELS.values() if m.get("new")]
        log(f"  RigUp catalog: {', '.join(new) + ' new' if new else 'up to date'}")
    except Exception as e:
        log(f"  couldn't read the RigUp catalog: {e}")
    g = gpu()
    vram = g["mem_total"] / 1024 if g else 0
    known = {base_name(s.get("repo") or s["url"].split("huggingface.co/")[-1].split("/resolve/")[0]) for m in MODELS.values() for s in m["files"]}
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 60 * 86400))
    found = []
    for author in RECIPES.get("watch", []):
        try:
            url = (f"https://huggingface.co/api/models?author={author}&sort=createdAt&direction=-1&limit=30"
                   "&expand[]=createdAt&expand[]=safetensors&expand[]=gguf&expand[]=pipeline_tag")
            for x in json.load(hf_open(url)):
                if any(base_name(x["id"]).startswith(k) for k in known) or x.get("createdAt", "") < cutoff or x.get("pipeline_tag") not in TASKS:
                    continue
                known.add(base_name(x["id"]))   # newest first, so other variants of the same model are skipped
                # Split GGUFs under-report their size, so also trust the "120B" in the name.
                named = max([float(n) * 1e9 for n in re.findall(r"(\d+(?:\.\d+)?)B(?![a-z])", x["id"].split("/")[-1])] or [0])
                params = max((x.get("safetensors") or {}).get("total") or 0, (x.get("gguf") or {}).get("total") or 0, named) or None
                q4 = params * 0.56 / 1e9 if params else None   # ~4.5 bits per weight, the usual 4-bit GGUF/AWQ size
                if q4 and vram and q4 + 2 > vram:
                    continue   # won't fit this card even at 4-bit
                found.append({"id": x["id"], "created": x["createdAt"][:10], "task": x.get("pipeline_tag"),
                              "params_b": round(params / 1e9, 1) if params else None, "q4_gb": round(q4, 1) if q4 else None})
        except Exception as e:
            log(f"  couldn't list {author} on Hugging Face: {e}")
    found.sort(key=lambda x: x["created"], reverse=True)
    write_json(RELEASES, {"checked": time.strftime("%Y-%m-%d %H:%M"), "hf": found[:15]})
    log(f"  Hugging Face: {len(found)} recent model(s) without a recipe that could fit this card")


# ---------- auto-tune ----------
CTX_FLAG = {"vllm": "--max-model-len", "llamacpp": "-c"}


def get_flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def set_flag(args, name, value):
    a = list(args)
    if name in a:
        a[a.index(name) + 1] = str(value)
    else:
        a += [name, str(value)]
    return a


def tier_of(g):
    if MAC:
        return max([t for t in TIERS if t <= g["ram_gb"]] or TIERS[:1])
    return None if not g else 24 if g["mem_total"] >= 22000 else 16 if g["mem_total"] >= 15000 else 12


def args_for(m):
    """Tuned args when auto-tune found working ones on this exact GPU, else the recipe's."""
    t, g = read_json(TUNED, {}).get(m["id"]), gpu()
    return t["args"] if t and t.get("args") and g and t["gpu"] == g["name"] else m["args"]


def model_max_ctx(m):
    """Longest context the model supports, from its config.json (vLLM repos only)."""
    c = read_json(os.path.join(DATA, "models", m["files"][0].get("repo", "-"), "config.json"), {})
    return c.get("max_position_embeddings") or c.get("text_config", {}).get("max_position_embeddings")


def engine_logs():
    if MAC:
        return read_text(ENGINE_LOG)
    r = subprocess.run(["docker", "logs", "rigup-llm"], capture_output=True, text=True)
    return r.stdout + r.stderr


def next_ctx(logs, ctx):
    """After a failed start: the context to retry with, or None if the failure isn't about memory."""
    est = re.search(r"estimated maximum model length is (\d+)", logs)
    if est:
        return int(est[1]) * 95 // 100 // 1024 * 1024
    if re.search(r"out of memory|cudaMalloc failed|failed to allocate|larger than the available KV cache", logs, re.I):
        return ctx // 2 // 1024 * 1024
    return None


def measure(mid):
    """Generation speed in tokens/s: best of two runs of a ~400 token answer (the first warms up)."""
    body = json.dumps({"model": mid, "max_tokens": 400, "temperature": 0,
                       "messages": [{"role": "user", "content": "Write a 300 word story about a lighthouse keeper."}]}).encode()
    best = 0
    for _ in range(2):
        t = time.time()
        with urllib.request.urlopen(local(f"http://127.0.0.1:{PORTS['llm']}/v1/chat/completions", body), timeout=600) as r:
            n = json.load(r)["usage"]["completion_tokens"]
        best = max(best, n / (time.time() - t))
    return round(best)


def tune(m):
    args, cf = list(m["args"]), CTX_FLAG[m["engine"]]
    for _ in range(5):
        try:
            switch(m["id"], args)
            break
        except RuntimeError:
            ctx = int(get_flag(args, cf))
            new = next_ctx(engine_logs(), ctx)
            if not new:
                raise
            if new < 4096:
                raise RuntimeError("doesn't fit on this card, even with a 4K context") from None
            log(f"  doesn't fit with {ctx} context, retrying with {new}")
            args = set_flag(args, cf, new)
    else:
        raise RuntimeError("still not starting after 5 tries")
    # vLLM reports how many full-length requests its KV cache holds. Spare room means a longer context fits.
    conc = re.search(r"Maximum concurrency for [\d,]+ tokens per request: ([\d.]+)x", engine_logs())
    ctx, top = int(get_flag(args, cf)), model_max_ctx(m)
    if conc and top and float(conc[1]) >= 1.25 and ctx < top:
        bigger = min(top, int(ctx * float(conc[1]) * 0.9) // 1024 * 1024)
        log(f"  room to spare, trying {bigger} context")
        try:
            switch(m["id"], set_flag(args, cf, bigger))
            args = set_flag(args, cf, bigger)
        except RuntimeError:
            log(f"  {bigger} didn't start, keeping {ctx}")
            switch(m["id"], args)
    tps, g = measure(m["id"]), gpu()
    result = {"gpu": g["name"], "args": args, "ctx": int(get_flag(args, cf)), "tps": tps,
              "vram": round(g["mem_used"] / 1024, 1), "image": mac_info()["engine"] if MAC else m.get("image") or IMAGES[m["engine"]], "date": time.strftime("%Y-%m-%d")}
    write_json(TUNED, {**read_json(TUNED, {}), m["id"]: result})
    log(f"  {m['name']}: {result['ctx']} context, {tps} tok/s, {result['vram']} GB VRAM")


def autotune():
    check_releases()
    g = gpu()
    if not g:
        raise RuntimeError("no GPU found")
    todo = [m for m in MODELS.values() if m["group"] == "llm" and installed(m) and str(tier_of(g)) in m["fit"]]
    if not todo:
        log("no installed Lab models to tune; get one first")
        return
    run = running()
    before = run.get("llm") or ("comfy" if "comfy" in run else None)
    try:
        for i, m in enumerate(todo, 1):
            log(f"tuning {m['name']} ({i} of {len(todo)})")
            try:
                tune(m)
            except Exception as e:
                log(f"  {m['name']}: {e}")
                write_json(TUNED, {**read_json(TUNED, {}), m["id"]: {"gpu": g["name"], "error": str(e)[:300], "date": time.strftime("%Y-%m-%d")}})
    finally:
        log("auto-tune done, putting back what was running")
        switch(before) if before else stop_gpu()


# ---------- status ----------
_mac = {}


def mac_info():
    """Chip, RAM and how much of it macOS lets the GPU use (asked once; llama.cpp reports the Metal limit)."""
    if not _mac:
        ram = int(sh("sysctl", "-n", "hw.memsize"))
        limit = re.search(r"MTL\d+: .*?\((\d+) MiB", sh(LLAMA_SERVER, "--list-devices", check=False))
        ver = subprocess.run([LLAMA_SERVER, "--version"], capture_output=True, text=True)   # prints to stderr
        build = re.search(r"build (\d+)", ver.stdout + ver.stderr)
        _mac.update(name=sh("sysctl", "-n", "machdep.cpu.brand_string"), ram_gb=round(ram / 2**30),
                    mem_total=int(limit[1]) if limit else int(ram * 0.75) // 2**20,   # macOS usually lets the GPU use ~75%
                    driver="macOS " + sh("sw_vers", "-productVersion"), engine=f"llama.cpp b{build[1]}" if build else "llama.cpp")
    return _mac


def gpu():
    if MAC:
        # Memory in use = the engine's footprint; model weights and cache live in unified memory.
        pid = read_json(ENGINE, {}).get("pid") if running() else None
        fp = sh("footprint", "-f", "bytes", "-p", str(pid), check=False) if pid else ""
        used = re.search(r"Footprint:\s*(\d+)", fp)
        return {**mac_info(), "mem_used": int(used[1]) // 2**20 if used else 0, "util": None, "temp": None, "power": None}
    try:
        out = sh("nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw,driver_version",
                 "--format=csv,noheader,nounits")
        name, used, total, util, temp, power, driver = [x.strip() for x in out.splitlines()[0].split(",")]
        return {"name": name, "mem_used": int(used), "mem_total": int(total), "util": int(util), "temp": int(temp), "power": round(float(power)),
                "driver": driver}
    except Exception:
        return None


def snapshot():
    g, run = gpu(), running()
    mode = run.get("llm") or ("comfy" if "comfy" in run else None)
    healthy = bool(mode) and up(HEALTH["comfy" if mode == "comfy" else "llm"])
    du = shutil.disk_usage(DATA if os.path.isdir(DATA) else "/")
    tier = tier_of(g)
    tuned = {k: {f: v for f, v in t.items() if f != "args"} for k, t in read_json(TUNED, {}).items() if g and t.get("gpu") == g["name"]}
    models = [{k: v for k, v in m.items() if k not in ("files", "args")} | {"installed": installed(m)} for m in MODELS.values()]
    return {"gpu": g, "tier": tier, "mode": mode, "healthy": healthy, "disk": {"free_gb": round(du.free / 1e9), "total_gb": round(du.total / 1e9)},
            "models": models, "ports": PORTS, "hf_token": bool(config().get("hf_token")), "api_key": bool(config().get("api_key")), "now": time.time(),
            "tuned": tuned, "releases": read_json(RELEASES, {}), "repo": REPO, "platform": "mac" if MAC else "linux", "tiers": TIERS,
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
        self.send_header("WWW-Authenticate", 'Basic realm="RigUp"')
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
        if self.headers.get("X-Requested-With") != "rigup":   # blocks cross-site form posts
            return self.send(403, {"error": "missing X-Requested-With"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            path = self.path.split("?")[0]
            mid = body.get("to") or body.get("id")
            if path == "/api/token":
                save_config(**{k: body[k].strip() for k in ("hf_token", "api_key") if isinstance(body.get(k), str)})
                return self.send(200, {"ok": True})
            if path == "/api/stop":
                job("stopping", None, stop_gpu)
            elif path == "/api/autotune":
                job("tuning", None, autotune)
            elif mid != "comfy" and mid not in MODELS or MAC and mid == "comfy":
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
    pw = os.environ.get("RIGUP_PASSWORD") or getpass.getpass("Dashboard password: ")
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
        print(f"RigUp on :{PORT}, data in {DATA}", flush=True)
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
