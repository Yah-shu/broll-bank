"""One-time setup, safe to re-run (finished steps are skipped).

    python install.py                 # everything: TikTok service (Docker) + caption removal + config + check
    python install.py --only dtk      # just the TikTok service
    python install.py --only vsr      # just caption removal
    python install.py --home D:/broll # where data, the service and models go (default: ~/broll-bank)

Needs: git, ffmpeg, Docker Desktop (running), Python 3.12 (for caption removal). AGENTS.md explains how to get them.
Only the Python standard library is used here.
"""
import argparse
import http.cookiejar
import json
import os
import pathlib
import platform
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent
CONFIG = REPO / "config.json"

DTK_GIT = "https://github.com/Evil0ctal/Douyin_TikTok_Download_API"
DTK_TAG = "v5.1.7"              # the version this tool was tested with
DTK_IMAGE_TAG = DTK_TAG.lstrip("v")
DTK_PORT = 8840
VSR_GIT = "https://github.com/YaoFANGUK/video-subtitle-remover"
VSR_TAG = "1.4.0"
PIP_RETRY = ["--retries", "10", "--timeout", "120"]

OS = platform.system()          # Windows | Darwin | Linux


def say(msg):
    print(f"\n==> {msg}", flush=True)


def run(cmd, check=True, **kw):
    print("   $ " + " ".join(str(c) for c in cmd), flush=True)
    p = subprocess.run([str(c) for c in cmd], **kw)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed ({p.returncode}): {' '.join(str(c) for c in cmd)}")
    return p


def out(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def need(tool, how):
    if not shutil.which(tool):
        raise SystemExit(f"`{tool}` was not found. {how}")


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def write_text_lf(path, text):
    """UTF-8, no BOM, LF line endings (Docker Compose rejects a BOM'd .env)."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ------------------------------------------------------------------ TikTok service (DTK, Docker)

def compose_cmd(dtk_dir):
    cmd = ["docker", "compose", "-p", "dtk", "-f", "docker/compose.yml"]
    if (dtk_dir / "docker" / "compose.host.yml").exists():
        cmd += ["-f", "docker/compose.host.yml"]
    return cmd


def compose_env():
    return dict(os.environ, COMPOSE_ENV_FILES=".env", COMPOSE_PROFILES="browser,downloader")


def read_env(path):
    vals = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def http_json(opener, method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def install_dtk(home, port):
    say("TikTok service (Douyin_TikTok_Download_API) in Docker")
    need("git", "Install Git (Windows: winget install -e --id Git.Git, Mac: xcode-select --install).")
    need("docker", "Install Docker Desktop and start it (see AGENTS.md).")
    if run(["docker", "info"], check=False, capture_output=True).returncode != 0:
        raise SystemExit("Docker is installed but not running. Start Docker Desktop, wait until it says running, retry.")
    dtk = home / "dtk"
    if not (dtk / "docker" / "compose.yml").exists():
        run(["git", "clone", "--depth", "1", "--branch", DTK_TAG, DTK_GIT, dtk])

    env_path = dtk / ".env"
    old = read_env(env_path)
    pg = old.get("POSTGRES_PASSWORD") or secrets.token_hex(16)
    rd = old.get("REDIS_PASSWORD") or secrets.token_hex(16)
    sk = old.get("DTK_SECRET_KEY") or secrets.token_urlsafe(48)
    write_text_lf(env_path, "\n".join([
        "# written by broll-bank install.py",
        f"DTK_SECRET_KEY={sk}",
        f"POSTGRES_PASSWORD={pg}",
        f"REDIS_PASSWORD={rd}",
        f"DTK_DATABASE_URL=postgresql+asyncpg://dtk:{pg}@postgres:5432/dtk",
        f"DTK_REDIS_URL=redis://:{rd}@redis:6379/0",
        "DTK_LOG_LEVEL=info",
        "DTK_BROWSER_RPC_URL=http://browser-rpc:9000",
        "DTK_DOWNLOADER_URL=http://downloader:9100",
        "DTK_BIND_HOST=127.0.0.1",
        f"DTK_BIND_PORT={port}",
        "DTK_IMAGE=evil0ctal/douyin_tiktok_download_api",
        f"DTK_IMAGE_TAG={DTK_IMAGE_TAG}",
        "DTK_DOWNLOADER_IMAGE=evil0ctal/douyin_tiktok_download_api-downloader",
        "",
    ]))

    # Docker refuses to start a container that asks for more CPUs than the machine gives Docker
    info = out(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"]).split()
    ncpu = int(info[0]) if info else 4
    mem_gb = int(info[1]) / 2**30 if len(info) > 1 else 8
    host = dtk / "docker" / "compose.host.yml"
    if ncpu < 4 or mem_gb < 7.5:
        c = float(max(1, ncpu))
        write_text_lf(host, "\n".join([
            "# written by broll-bank install.py: this machine gives Docker "
            f"{ncpu} CPUs / {mem_gb:.1f} GB",
            "services:",
            "  browser-rpc:", f"    cpus: {min(4.0, c)}", "    mem_limit: 3g",
            "  api:", f"    cpus: {min(2.0, c)}",
            "  worker:", f"    cpus: {min(2.0, c)}",
            "  downloader:", f"    cpus: {min(2.0, c)}", ""]))
    elif host.exists():
        host.unlink()

    cmd, env = compose_cmd(dtk), compose_env()
    say("downloading the service images (a few GB, first time only)")
    for attempt in range(3):
        if run(cmd + ["pull", "postgres", "redis", "migrate", "api", "worker", "downloader"],
               cwd=dtk, env=env, check=False).returncode == 0:
            break
        time.sleep(10)
    else:
        raise SystemExit("could not download the service images; check the internet connection and retry")
    say("building the browser part (first time only, several minutes)")
    run(cmd + ["build", "browser-rpc"], cwd=dtk, env=env)
    say("starting the service")
    run(cmd + ["up", "-d", "--wait"], cwd=dtk, env=env)

    sec = home / "secrets"
    sec.mkdir(parents=True, exist_ok=True)
    pw_file, key_file = sec / "dtk_admin_password.txt", sec / "dtk_key.txt"
    base = f"http://127.0.0.1:{port}"
    if key_file.exists():
        code, _ = http_json(urllib.request.build_opener(), "GET", base + "/api/v1/system/capabilities",
                            headers={"Authorization": f"Bearer {key_file.read_text().strip()}"})
        if code == 200:
            print("   API key already works")
            return dtk, key_file
    if not pw_file.exists():
        pw_file.write_text(secrets.token_urlsafe(18), encoding="utf-8")
    pw = pw_file.read_text(encoding="utf-8").strip()
    say("creating the service's admin account and an API key")
    run(cmd + ["exec", "-T", "api", "dtk", "user", "create", "admin", "--role", "admin", "--stdin"],
        cwd=dtk, env=env, check=False, input=pw + "\n", text=True)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    code, d = http_json(opener, "POST", base + "/api/v1/auth/login", {"username": "admin", "password": pw})
    if code >= 400:
        raise SystemExit(f"could not log in to the service ({code}): {json.dumps(d)[:300]}")
    csrf = next((c.value for c in jar if "csrf" in c.name.lower()), None)
    code, d = http_json(opener, "POST", base + "/api/v1/admin/api-keys",
                        {"name": "broll-bank", "scopes": ["tiktok:read", "media:read", "media:write"]},
                        headers={"X-CSRF-Token": csrf} if csrf else None)
    data = (d or {}).get("data") or {}
    key = data.get("secret") or data.get("key") or data.get("api_key")
    if not key:
        raise SystemExit(f"could not create the API key ({code}): {json.dumps(d)[:300]}")
    key_file.write_text(key, encoding="utf-8")
    print(f"   API key saved to {key_file}")
    return dtk, key_file


# ------------------------------------------------------------------ caption removal (video-subtitle-remover)

def find_python312():
    if sys.version_info[:2] == (3, 12):
        return sys.executable
    if OS == "Windows" and shutil.which("py"):
        exe = out(["py", "-3.12", "-c", "import sys; print(sys.executable)"])
        if exe:
            return exe
    for name in ("python3.12", "python3", "python"):
        exe = shutil.which(name)
        if exe and out([exe, "-c", "import sys; print(sys.version_info[:2] == (3, 12))"]) == "True":
            return exe
    raise SystemExit("Caption removal needs Python 3.12. Windows: winget install -e --id Python.Python.3.12  "
                     "Mac: brew install python@3.12   Then re-run.")


def detect_gpu():
    """cuda | cuda-new (RTX 50) | directml | mps | cpu"""
    if shutil.which("nvidia-smi"):
        cap = out(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"]).splitlines()
        if cap:
            try:
                return "cuda-new" if float(cap[0]) >= 12.0 else "cuda"
            except ValueError:
                return "cuda"
    if OS == "Windows":
        names = out(["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_VideoController).Name -join ';'"]).lower()
        if any(v in names for v in ("amd", "radeon", "intel", "arc")):
            return "directml"
        return "cpu"
    if OS == "Darwin" and platform.machine() == "arm64":
        return "mps"
    return "cpu"


def install_vsr(home, gpu=None):
    say("caption removal (video-subtitle-remover)")
    need("git", "Install Git first.")
    py = find_python312()
    root = home / "vsr"
    src, venv = root / "src", root / "venv"
    if not (src / "backend").exists():
        # everything except the ProPainter model (its weights are licensed non-commercial; we never use it)
        run(["git", "clone", "--filter=blob:none", "--no-checkout", "--branch", VSR_TAG, VSR_GIT, src])
        run(["git", "-C", src, "sparse-checkout", "set", "--no-cone", "/*", "!/backend/models/propainter/"])
        run(["git", "-C", src, "checkout"])
    stub = src / "backend" / "models" / "propainter" / "ProPainter.pth"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.touch()  # VSR crashes at start if the folder is missing; the empty file is never loaded
    vpy = venv / ("Scripts/python.exe" if OS == "Windows" else "bin/python")
    if not vpy.exists():
        run([py, "-m", "venv", venv])
    pip = [vpy, "-m", "pip", "install"] + PIP_RETRY
    run([vpy, "-m", "pip", "install", "--upgrade", "pip"] + PIP_RETRY)
    gpu = gpu or detect_gpu()
    print(f"   graphics: {gpu}")
    # text detection runs on the CPU everywhere (as upstream's own Docker image does), so no CUDA toolkit is needed
    run(pip + ["paddlepaddle==3.0.0", "-i", "https://www.paddlepaddle.org.cn/packages/stable/cpu/"])
    if gpu in ("cuda", "cuda-new"):
        idx = "https://download.pytorch.org/whl/" + ("cu128" if gpu == "cuda-new" else "cu126")
        run(pip + ["torch==2.7.0", "torchvision==0.22.0", "--index-url", idx])
    elif gpu == "directml":
        run(pip + ["torch_directml==0.2.5.dev240914"])
    elif gpu == "mps":
        run(pip + ["torch==2.7.0", "torchvision==0.22.0"])
    else:
        run(pip + ["torch==2.7.0", "torchvision==0.22.0", "--index-url", "https://download.pytorch.org/whl/cpu"])
    run(pip + ["-r", src / "requirements.txt"])
    if OS == "Darwin":
        # the bundled macOS ffmpeg is Intel-only; point it at the system one
        sys_ff = shutil.which("ffmpeg")
        bundled = src / "backend" / "ffmpeg" / "macos" / "ffmpeg"
        if sys_ff and bundled.exists() and not bundled.is_symlink():
            bundled.rename(bundled.with_name("ffmpeg.intel"))
            bundled.symlink_to(sys_ff)
    check = run([vpy, "-c", "import torch, paddle, cv2; print('torch', torch.__version__)"], check=False)
    if check.returncode != 0:
        raise SystemExit("caption removal installed but its libraries don't import; see the error above")
    (root / "run" / "config").mkdir(parents=True, exist_ok=True)
    return vpy, src, root / "run", gpu


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", help="data folder (default: ~/broll-bank)")
    ap.add_argument("--only", choices=["dtk", "vsr"])
    ap.add_argument("--port", type=int, default=DTK_PORT)
    ap.add_argument("--gpu", choices=["cuda", "cuda-new", "directml", "mps", "cpu"], help="override detection")
    a = ap.parse_args()
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is needed.")
    need("ffmpeg", "Install ffmpeg (Windows: winget install -e --id Gyan.FFmpeg, Mac: brew install ffmpeg), "
                   "then open a new terminal.")
    cfg = load_config()
    home = pathlib.Path(a.home or cfg.get("home") or pathlib.Path.home() / "broll-bank").expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    cfg["home"] = home.as_posix()
    cfg.setdefault("ai", "auto")
    if a.only in (None, "dtk"):
        dtk, key_file = install_dtk(home, a.port)
        cfg.update(dtk_dir=dtk.as_posix(), dtk_url=f"http://127.0.0.1:{a.port}", dtk_key_file=key_file.as_posix())
        save_config(cfg)
    if a.only in (None, "vsr"):
        vpy, src, wd, gpu = install_vsr(home, a.gpu)
        cfg.update(vsr_python=vpy.as_posix(), vsr_src=src.as_posix(), vsr_workdir=wd.as_posix(), gpu=gpu)
        save_config(cfg)
    say(f"settings saved to {CONFIG}")
    say("checking the install")
    run([sys.executable, REPO / "broll.py", "doctor", "--ai-test"], check=False)


if __name__ == "__main__":
    main()
