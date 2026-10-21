"""Turn downloaded videos into tagged B-roll clips.

Per video:
  1. find the cuts (ffmpeg scene detection), split long takes into pieces of at most MAX_SHOT seconds
  2. make one small strip of 3 frames (start / middle / end) per shot
  3. one AI call per video (up to SHOTS_PER_CALL shots per call) tags every shot: what moment it shows,
     whether it is usable B-roll, who and where, burned-in text, brand or logo, product in frame
  4. cut the usable shots into clips (no audio)
"""
import concurrent.futures as cf
import json
import pathlib
import re
import shutil
import subprocess

from . import ai, config
from .db import connect

SCENE_THRESHOLD = 8      # ffmpeg scdet threshold (percent change); lower finds more cuts
MIN_SHOT = 0.8           # seconds; shorter pieces are merged into the shot before them
MAX_SHOT = 6.0           # seconds; longer takes are split evenly
SHOTS_PER_CALL = 12
WORKERS = 3              # AI calls in parallel

MOMENTS = {
    "pain_struggle": "the problem on camera: discomfort, pain, struggling, failing at a task",
    "daily_life": "ordinary life of this audience with no obvious problem shown",
    "demo_exercise": "an exercise, stretch, technique or remedy being done",
    "product": "a product shown, held, applied or used",
    "result_relief": "relief, ease, doing the activity freely, happy after",
    "talking_head": "a person mainly talking to the camera",
    "text_graphic": "mostly text, slides, screenshots, diagrams or animation",
    "other": "anything else",
}

TAG_PROMPT = """You are tagging shots from a TikTok video for a B-roll library used to make ads.
Niche: {niche}
The video's caption: {caption}

Each image is ONE shot: 3 frames side by side (start, middle, end of the shot). Shots in order:
{shots}

For every shot return:
- shot: its number
- moment: one of {moments}
{moment_help}
- usable: true if it works as B-roll in an ad: real camera footage that shows something (a person, a body part,
  an action, a place, an object). false for: someone mainly talking to camera, text/graphics/slides, split-screen
  or picture-in-picture templates, frozen frames, too dark/blurry, the same frame repeated, or graphics drawn
  ON the footage other than captions (glowing pain spots, arrows, circles, stickers, emoji, filters/effects) -
  captions and on-screen text alone do NOT make a shot unusable, they can be removed
- why_not: short reason when usable is false, else ""
- fits_niche: true if the shot shows the niche itself (its problem, body part, audience or remedy); false if it
  shows something else (another body part or condition, unrelated people or scenes)
- description: one plain sentence of what is visible, specific enough to search for
  (e.g. "older woman's swollen knuckles as she tries to open a jar in a kitchen")
- people: who is visible ("woman 60s", "man 30s", "hands only", "none")
- setting: where ("kitchen", "bedroom", "clinic", "outdoors", "studio"...)
- burned_text: true if captions or any on-screen text appear in the frames
- brand: brand, shop or logo name if one is readable, else ""
- product_visible: true if a product is in frame
- quality: "good", "ok" or "poor"

Return ONLY a JSON array with one object per shot, no prose.
"""


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def detect_shots(path):
    """[(start, end)] covering the whole video."""
    dur = probe_duration(path)
    if dur <= 0:
        return []
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf", f"scdet=threshold={SCENE_THRESHOLD}",
                          "-an", "-f", "null", "-"], capture_output=True, text=True, errors="replace", timeout=900).stderr
    cuts = sorted({round(float(t), 3) for t in re.findall(r"lavfi\.scd\.time:\s*([0-9.]+)", err)})
    bounds = [0.0] + [c for c in cuts if 0 < c < dur] + [dur]
    shots = []
    for a, b in zip(bounds, bounds[1:]):
        if shots and b - a < MIN_SHOT:
            shots[-1] = (shots[-1][0], b)  # too short: merge into the previous shot
            continue
        shots.append((a, b))
    out = []
    for a, b in shots:
        n = max(1, int(-(-(b - a) // MAX_SHOT)))  # ceil
        step = (b - a) / n
        out += [(round(a + i * step, 3), round(a + (i + 1) * step, 3)) for i in range(n)]
    if len(out) > 1 and out[-1][1] - out[-1][0] < MIN_SHOT:
        out[-2] = (out[-2][0], out[-1][1])
        out.pop()
    return out


def make_strip(path, start, end, dest):
    """3 frames (start / middle / end of the shot), 320 px tall, side by side."""
    d = end - start
    ts = [start + d * f for f in (0.1, 0.5, 0.9)]
    args = ["ffmpeg", "-v", "error", "-y"]
    for t in ts:
        args += ["-ss", f"{t:.3f}", "-i", str(path)]
    args += ["-filter_complex",
             ";".join(f"[{i}:v]scale=-2:320,setsar=1,trim=end_frame=1[f{i}]" for i in range(3))
             + ";[f0][f1][f2]hstack=inputs=3,format=yuvj420p", "-frames:v", "1", "-q:v", "4", str(dest)]
    subprocess.run(args, capture_output=True, timeout=120)
    return dest.exists()


def tag_video(video, niche):
    """Detect shots, build strips, ask the AI. Returns (shots, tags) or raises."""
    path = video["file"]
    shots = detect_shots(path)
    if not shots:
        raise RuntimeError("could not read the video")
    wd = config.WORK / "strips" / video["content_id"]
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    strips = []
    for i, (a, b) in enumerate(shots, 1):
        s = wd / f"shot_{i:02d}.jpg"
        if not make_strip(path, a, b, s):
            raise RuntimeError(f"could not grab frames for shot {i}")
        strips.append(s)
    tags = {}
    for k in range(0, len(shots), SHOTS_PER_CALL):
        part = list(range(k, min(k + SHOTS_PER_CALL, len(shots))))
        prompt = TAG_PROMPT.format(
            niche=niche, caption=re.sub(r"\s+", " ", video["description"] or "")[:400] or "(none)",
            shots="\n".join(f"shot {i + 1}: image {n}, {shots[i][0]:.1f}-{shots[i][1]:.1f}s ({shots[i][1] - shots[i][0]:.1f}s)"
                            for n, i in enumerate(part, 1)),
            moments=", ".join(MOMENTS), moment_help="\n".join(f"    {m}: {h}" for m, h in MOMENTS.items()))
        res = ai.ask_json(prompt, [strips[i] for i in part])
        if isinstance(res, dict):
            res = res.get("shots") or [res]
        for t in res:
            try:
                tags[int(t.get("shot"))] = t
            except (TypeError, ValueError):
                continue
    return shots, tags


def cut_clip(src, start, end, dest):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
                    "-an", "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(dest)], capture_output=True, timeout=600)
    return dest.exists() and dest.stat().st_size > 0


def save(con, video, shots, tags):
    cid = video["content_id"]
    for old in con.execute("SELECT clip, clean_clip FROM shots WHERE content_id=?", (cid,)).fetchall():
        for f in (old["clip"], old["clean_clip"]):  # re-tagging: drop the old clips and their posters
            if f:
                pathlib.Path(f).unlink(missing_ok=True)
                pathlib.Path(f).with_suffix(".jpg").unlink(missing_ok=True)
    con.execute("DELETE FROM shots WHERE content_id=?", (cid,))
    kept = 0
    for i, (a, b) in enumerate(shots, 1):
        t = tags.get(i) or {}
        moment = t.get("moment") if t.get("moment") in MOMENTS else "other"
        usable = bool(t.get("usable")) and bool(t)
        clip = None
        if usable:
            dest = config.CLIPS / f"{cid}_{i:02d}.mp4"
            if cut_clip(video["file"], a, b, dest):
                clip, kept = str(dest), kept + 1
        con.execute("""INSERT INTO shots(content_id, idx, start, end, moment, usable, description, people, setting,
                       burned_text, brand, product_visible, quality, why_not, clip, clean_status, fits_niche)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, i, a, b, moment, int(usable), t.get("description") or "", t.get("people") or "",
                     t.get("setting") or "", int(bool(t.get("burned_text"))), t.get("brand") or "",
                     int(bool(t.get("product_visible"))), t.get("quality") or "",
                     (t.get("why_not") or ("not tagged" if not t else "")), clip,
                     ("pending" if t.get("burned_text") else "not_needed") if clip else None,
                     int(t.get("fits_niche", True) is not False)))
    con.execute("UPDATE videos SET shot_status='tagged', shot_note=? WHERE content_id=?",
                (f"{len(shots)} shots, {kept} usable", cid))
    con.commit()
    return kept


def process(slug, limit=None, retry_failed=False, redo=False, log=print):
    """Tag every downloaded video of the niche that isn't tagged yet (redo=True: all of them again)."""
    con = connect()
    niche = con.execute("SELECT seed FROM niches WHERE slug=?", (slug,)).fetchone()
    niche = niche["seed"] if niche else slug
    states = ("1=1" if redo else
              "(v.shot_status IS NULL OR v.shot_status='failed')" if retry_failed else "v.shot_status IS NULL")
    rows = con.execute(f"""SELECT v.* FROM links l JOIN videos v USING(content_id)
                           WHERE l.slug=? AND v.status='downloaded' AND {states} ORDER BY v.plays DESC""",
                       (slug,)).fetchall()
    rows = rows[:limit] if limit else rows
    log(f"[{slug}] tagging shots in {len(rows)} videos with {ai.backend()[0]}")
    total = 0
    with cf.ThreadPoolExecutor(WORKERS) as pool:
        futs = {pool.submit(tag_video, r, niche): r for r in rows}
        for n, f in enumerate(cf.as_completed(futs), 1):
            r = futs[f]
            try:
                shots, tags = f.result()
            except Exception as e:  # noqa: BLE001 - one bad video must not stop the run
                con.execute("UPDATE videos SET shot_status='failed', shot_note=? WHERE content_id=?",
                            (str(e)[:300], r["content_id"]))
                con.commit()
                log(f"  [{n}/{len(rows)}] {r['content_id']}: failed: {str(e)[:150]}")
                continue
            kept = save(con, r, shots, tags)
            total += kept
            moments = {}
            for t in tags.values():
                if t.get("usable"):
                    moments[t.get("moment")] = moments.get(t.get("moment"), 0) + 1
            log(f"  [{n}/{len(rows)}] {r['content_id']}: {len(shots)} shots, {kept} usable "
                + (json.dumps(moments) if moments else ""))
    log(f"{total} usable clips cut")
    return total
