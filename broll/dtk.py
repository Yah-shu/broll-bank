"""Minimal client for the local Douyin_TikTok_Download_API ("DTK") REST API."""
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

PAUSE = 1.5  # seconds between search calls, to stay polite with TikTok


def req(method, path, params=None, body=None, timeout=120):
    url = config.DTK_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {config.dtk_key()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"success": False, "error": {"code": "UNREACHABLE", "message": str(e)}}


def call(method, path, params=None, body=None, wait=300):
    """Do a request; if the API queues it as a task (202), poll the task until it finishes."""
    code, d = req(method, path, params, body)
    t0 = time.time()
    task = (d or {}).get("data") or {}
    if code == 202 and isinstance(task, dict) and task.get("task_id") and "download_id" not in task:
        tid = task["task_id"]
        while time.time() - t0 < wait:
            time.sleep(1.5)
            code, d = req("GET", f"/api/v1/tasks/{tid}")
            st = ((d or {}).get("data") or {}).get("state")
            if st not in ("queued", "running", "pending", "started"):
                break
    return code, d


def get(path, params):
    """GET with a pause before it and a retry on rate limits."""
    time.sleep(PAUSE)
    code, d = 0, None
    for attempt in range(3):
        code, d = call("GET", path, params)
        if code in (429, 503) and attempt < 2:
            time.sleep(10 * (attempt + 1))
            continue
        break
    return code, d


def unwrap(d):
    """(items, next_cursor, has_more, error) from either a finished task record or a direct envelope."""
    d = d or {}
    if d.get("success") is False or d.get("error"):
        err = d.get("error") or {}
        return [], None, False, (str(err.get("code") or err) if isinstance(err, dict) else str(err))[:200]
    x = d.get("data") or {}
    if isinstance(x, dict) and x.get("state") == "failed":
        return [], None, False, json.dumps(x.get("error") or x)[:200]
    inner = x.get("data") if isinstance(x.get("data"), dict) else x
    return inner.get("items") or [], inner.get("cursor"), bool(inner.get("has_more")), None


def health():
    try:
        with urllib.request.urlopen(config.DTK_URL + "/health", timeout=10) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def download(cid, dest, wait=300):
    """Download one TikTok post's mp4 (no watermark) to dest. Returns (ok, message)."""
    t0 = time.time()
    code, d = req("POST", "/api/v1/downloads", body={"platform": "tiktok", "content_id": cid})
    did = ((d or {}).get("data") or {}).get("download_id")
    if not did:
        return False, f"could not start ({code}) {json.dumps(d)[:200]}"
    row = {}
    while time.time() - t0 < wait:
        code, d = req("GET", f"/api/v1/downloads/{did}")
        row = (d or {}).get("data") or {}
        if row.get("state") in ("done", "failed", "partial", "cancelled", "stored", "completed"):
            break
        time.sleep(2)
    names = [f.get("name") if isinstance(f, dict) else f for f in row.get("files") or []]
    vid = next((n for n in names if n and n.endswith(".mp4")), None)
    if not vid:
        return False, f"state {row.get('state')}, no mp4 ({names})"
    r = urllib.request.Request(f"{config.DTK_URL}/api/v1/downloads/{did}/files/{vid}",
                               headers={"Authorization": f"Bearer {config.dtk_key()}"})
    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(r, timeout=300) as resp, open(tmp, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    tmp.replace(dest)
    return True, f"{row.get('state')} {dest.stat().st_size // 1024} KB {time.time() - t0:.1f}s"
