"""Remove burned-in captions/text from usable clips with video-subtitle-remover (runs in its own venv).
Logos and brands are never edited out; only text the detector finds is filled in."""
import os
import pathlib
import subprocess
import time

from . import config
from .db import connect

RUNNER = config.REPO / "vsr" / "vsr_runner.py"
ALLOWED_MODES = ("sttn-auto", "sttn-det", "lama")  # never propainter: its weights are non-commercial
DEFAULT_MODES = "sttn-det,sttn-auto"  # det: any caption style (boxed, stylised), faster; auto: cleaner on plain white captions


def available():
    return bool(config.VSR_PYTHON and pathlib.Path(config.VSR_PYTHON).exists()
                and config.VSR_SRC and pathlib.Path(config.VSR_SRC).exists())


def clean_clip(src, dest, mode="sttn-auto", timeout=3600):
    if mode not in ALLOWED_MODES:
        raise ValueError(f"mode must be one of {ALLOWED_MODES}")
    wd = pathlib.Path(config.VSR_WORKDIR)
    (wd / "config").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, VSR_SRC=str(config.VSR_SRC), PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
               PYTHONIOENCODING="utf-8")
    t0 = time.time()
    p = subprocess.run([config.VSR_PYTHON, str(RUNNER), "--input", str(src), "--output", str(dest), "--mode", mode],
                       cwd=wd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    log = (p.stdout or "") + (p.stderr or "")
    ok = p.returncode == 0 and pathlib.Path(dest).exists() and pathlib.Path(dest).stat().st_size > 0
    note = "no text found" if "no text detected" in log else f"{time.time() - t0:.0f}s"
    return ok, (note if ok else log[-500:])


def clean_pending(slug, limit=None, mode="sttn-auto", log=print, ids=None):
    """Clean clips with text. ids: "<content_id>_<nn>" list (from the gallery) to clean exactly those clips;
    otherwise the best `limit` pending clips. `mode` may list several methods ("sttn-auto,lama"): each is tried
    until the result passes the check."""
    if not available():
        raise SystemExit("Caption removal is not installed (vsr_python / vsr_src in config.json). See AGENTS.md.")
    modes = [m.strip() for m in mode.split(",") if m.strip()]
    if any(m not in ALLOWED_MODES for m in modes):
        raise SystemExit(f"--mode must be from {ALLOWED_MODES}")
    con = connect()
    if ids:
        keys = [tuple(i.rsplit("_", 1)) for i in ids]
        rows = [r for cid, idx in keys for r in con.execute(
            "SELECT * FROM shots WHERE content_id=? AND idx=? AND clip IS NOT NULL", (cid, int(idx))).fetchall()]
    else:
        rows = con.execute("""SELECT s.* FROM shots s JOIN links l ON l.content_id=s.content_id
                              WHERE l.slug=? AND s.clean_status='pending' AND COALESCE(s.fits_niche, 1)=1
                              ORDER BY CASE s.quality WHEN 'good' THEN 0 WHEN 'ok' THEN 1 ELSE 2 END,
                                       s.end - s.start, s.content_id, s.idx""", (slug,)).fetchall()
        rows = rows[:limit] if limit else rows
    log(f"[{slug}] removing text from {len(rows)} clips ({' then '.join(modes)}); minutes per clip")
    done = 0
    for n, r in enumerate(rows, 1):
        src = pathlib.Path(r["clip"])
        status, kept, notes = "failed", None, []
        for m in modes:
            dest = src.with_name(f"{src.stem}_clean.mp4")
            try:
                ok, note = clean_clip(src, dest, m)
            except subprocess.TimeoutExpired:
                ok, note = False, "timed out"
            if not ok:
                notes.append(f"{m}: {note}")
                continue
            if note == "no text found":
                status, kept = "done", str(dest)
                notes.append(note)
                break
            good, why = check(src, dest, r["start"], r["end"])
            notes.append(f"{m} {note}: {why}")
            if good:
                status, kept = "done", str(dest)
                break
            status = "failed_check"
            bad = dest.with_name(f"{src.stem}_clean_{m}_rejected.mp4")
            dest.replace(bad)  # kept for a look, never used
        note = " | ".join(notes)
        con.execute("UPDATE shots SET clean_status=?, clean_clip=?, clean_note=? WHERE content_id=? AND idx=?",
                    (status, kept, note[:1000], r["content_id"], r["idx"]))
        con.commit()
        done += status == "done"
        log(f"  [{n}/{len(rows)}] {src.name}: {status} ({note[:300]})")
    log(f"{done} of {len(rows)} clips cleaned")
    return done


CHECK_PROMPT = """Two rows of video frames. TOP row: the original clip. BOTTOM row: the same frames after an attempt to
remove the burned-in captions / on-screen text.
Judge the BOTTOM row only, against the top:
- text_gone: is all the caption text gone (no letters, no half-erased words)?
- clean_fill: does the area where the text was look natural (no leftover caption boxes or bars, no dark or blurry
  smears, no warped body parts or objects)?
Return ONLY JSON: {"text_gone": true/false, "clean_fill": true/false, "note": "short reason"}"""


def check(src, dest, start, end):
    """Look at before/after frames with the AI; the cleaned clip is used only if it really looks clean."""
    from . import ai, shots
    d = end - start
    strip_a = config.WORK / "check" / f"{pathlib.Path(src).stem}_a.jpg"
    strip_b = strip_a.with_name(f"{pathlib.Path(src).stem}_b.jpg")
    both = strip_a.with_name(f"{pathlib.Path(src).stem}_ab.jpg")
    strip_a.parent.mkdir(parents=True, exist_ok=True)
    shots.make_strip(src, 0, d, strip_a)
    shots.make_strip(dest, 0, d, strip_b)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(strip_a), "-i", str(strip_b),
                    "-filter_complex", "vstack=inputs=2", str(both)], capture_output=True, timeout=60)
    try:
        res = ai.ask_json(CHECK_PROMPT, [both])
    except Exception as e:  # noqa: BLE001 - no verdict: don't trust the clean
        return False, f"check failed: {str(e)[:120]}"
    good = res.get("text_gone") is True and res.get("clean_fill") is True
    return good, ("passed check" if good else f"rejected by check: {res.get('note', '')}")
