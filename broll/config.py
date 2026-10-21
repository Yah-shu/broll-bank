"""Settings: config.json in the repo root (not committed), overridden by BROLL_<KEY> environment variables."""
import json
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
_FILE = REPO / "config.json"
_cfg = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else {}


def get(key, default=None):
    return os.environ.get("BROLL_" + key.upper()) or _cfg.get(key) or default


HOME = pathlib.Path(get("home", pathlib.Path.home() / "broll-bank"))
DATA = HOME / "data"
DB = DATA / "bank.db"
VIDEOS = DATA / "videos"      # downloaded TikToks, <content_id>.mp4
CLIPS = DATA / "clips"        # cut B-roll clips, <content_id>_<nn>.mp4 (+ _clean.mp4)
WORK = DATA / "work"          # frame strips and other scratch files
NICHES = HOME / "niches"      # per-niche gallery (index.html, clips.json, clips.csv)
LOGS = DATA / "logs"

DTK_URL = get("dtk_url", "http://127.0.0.1:8840").rstrip("/")
DTK_KEY_FILE = get("dtk_key_file", str(HOME / "secrets" / "dtk_key.txt"))
DTK_DIR = get("dtk_dir")            # the service's checkout when it runs in Docker (set by install.py)

VSR_PYTHON = get("vsr_python")      # python.exe of the video-subtitle-remover venv
VSR_SRC = get("vsr_src")            # its source folder
VSR_WORKDIR = get("vsr_workdir", str(HOME / "vsr-run"))

AI = get("ai", "auto")              # auto | claude | codex
AI_MODEL = get("ai_model")          # optional model name for the chosen CLI


def dtk_key():
    p = pathlib.Path(DTK_KEY_FILE)
    if not p.exists():
        raise SystemExit(f"DTK API key file not found: {p}. Run the install steps in AGENTS.md first.")
    return p.read_text(encoding="utf-8").strip()


def ensure_dirs():
    for d in (DATA, VIDEOS, CLIPS, WORK, NICHES, LOGS):
        d.mkdir(parents=True, exist_ok=True)
