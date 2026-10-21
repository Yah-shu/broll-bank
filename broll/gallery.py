"""Per-niche gallery (index.html you open in a browser), clips.json / clips.csv, export and search."""
import csv
import html
import json
import os
import pathlib
import re
import shutil
import subprocess

from . import config
from .db import connect
from .shots import MOMENTS


def clip_rows(con, slug=None):
    where, args = ("JOIN links l ON l.content_id=s.content_id AND l.slug=?", (slug,)) if slug else ("", ())
    return con.execute(f"""SELECT s.*, v.url, v.author, v.plays, v.description AS caption FROM shots s
                           {where} JOIN videos v ON v.content_id=s.content_id
                           WHERE s.usable=1 AND s.clip IS NOT NULL
                           ORDER BY CASE s.quality WHEN 'good' THEN 0 WHEN 'ok' THEN 1 ELSE 2 END, v.plays DESC""",
                       args).fetchall()


def best_file(r):
    """The clip to use: the cleaned one if captions were removed, else the cut clip."""
    return r["clean_clip"] if r["clean_status"] == "done" and r["clean_clip"] else r["clip"]


def caption_state(r):
    if not r["burned_text"]:
        return "no text"
    return {"done": "text removed", "failed": "has text (removal failed)",
            "failed_check": "has text (removal didn't look clean)"}.get(r["clean_status"], "has text")


def poster(clip):
    p = pathlib.Path(clip).with_suffix(".jpg")
    if not p.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.3", "-i", str(clip), "-frames:v", "1",
                        "-vf", "scale=-2:360", "-q:v", "5", str(p)], capture_output=True, timeout=60)
    return p


def record(r, rel_to):
    f = best_file(r)
    return {
        "id": f"{r['content_id']}_{r['idx']:02d}", "file": f,
        "rel": os.path.relpath(f, rel_to).replace("\\", "/"),
        "poster": os.path.relpath(poster(f), rel_to).replace("\\", "/"),
        "moment": r["moment"], "description": r["description"], "people": r["people"], "setting": r["setting"],
        "seconds": round(r["end"] - r["start"], 1), "quality": r["quality"], "text": caption_state(r),
        "brand": r["brand"], "product": bool(r["product_visible"]), "source": r["url"], "author": r["author"],
        "fits": r["fits_niche"] is None or bool(r["fits_niche"]),
        "plays": r["plays"],
    }


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>B-roll: {title}</title>
<style>
body{{font:14px system-ui,sans-serif;margin:0;background:#111;color:#eee}}
header{{position:sticky;top:0;background:#1b1b1b;padding:12px 16px;border-bottom:1px solid #333;z-index:2}}
h1{{font-size:18px;margin:0 0 8px}} button{{background:#2a2a2a;color:#ddd;border:1px solid #444;border-radius:14px;
padding:4px 10px;margin:2px;cursor:pointer}} button.on{{background:#e8e8e8;color:#111}}
input[type=search]{{background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:5px 8px;width:260px}}
label{{margin-left:12px;color:#aaa}} #grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
gap:12px;padding:16px}} .card{{background:#1c1c1c;border-radius:8px;overflow:hidden}}
video{{width:100%;aspect-ratio:9/16;object-fit:cover;background:#000;display:block}}
.meta{{padding:8px;font-size:12px;line-height:1.4}} .m{{font-weight:600;color:#9fd}} .flag{{color:#fb7}}
a{{color:#8bf}} .count{{color:#888;margin-left:8px}}
</style></head><body><header><h1>B-roll: {title} <span class="count" id="count"></span></h1>
<div id="moments"></div>
<input type="search" id="q" placeholder="search: hands jar kitchen...">
<label><input type="checkbox" id="nobrand"> hide clips with a brand/logo</label>
<label><input type="checkbox" id="notext"> only clips without text</label>
<label><input type="checkbox" id="fits" checked> only shots that fit the niche</label>
<button id="copy" title="copies the command; paste it to your agent or terminal">copy clean command for selected (0)</button></header>
<div id="grid"></div>
<script>
const CLIPS = {data};
const MOMENTS = {moments};
const NICHE = {niche};
const picked = new Set();
function pick(id, box) {{ box.checked ? picked.add(id) : picked.delete(id);
  document.getElementById('copy').textContent = 'copy clean command for selected (' + picked.size + ')'; }}
document.getElementById('copy').onclick = () => {{
  if (!picked.size) {{ alert('Tick the clips whose text you want removed first.'); return; }}
  const cmd = 'python broll.py clean ' + NICHE + ' --ids ' + [...picked].join(',');
  (navigator.clipboard ? navigator.clipboard.writeText(cmd) : Promise.reject()).then(
    () => alert('Copied: ' + cmd), () => prompt('Copy this command:', cmd));
}};
let on = new Set();
const mbox = document.getElementById('moments');
for (const m of Object.keys(MOMENTS)) {{
  const n = CLIPS.filter(c => c.moment === m).length; if (!n) continue;
  const b = document.createElement('button'); b.textContent = m.replace('_', ' / ') + ' (' + n + ')';
  b.title = MOMENTS[m]; b.onclick = () => {{ on.has(m) ? on.delete(m) : on.add(m); b.classList.toggle('on'); draw(); }};
  mbox.appendChild(b);
}}
function esc(s) {{ return (s || '').replace(/[&<>"]/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[ch]); }}
function draw() {{
  const q = document.getElementById('q').value.toLowerCase().split(/\\s+/).filter(Boolean);
  const nb = document.getElementById('nobrand').checked, nt = document.getElementById('notext').checked;
  const fi = document.getElementById('fits').checked;
  const list = CLIPS.filter(c => (!on.size || on.has(c.moment)) && (!nb || !c.brand)
    && (!nt || !c.text.startsWith('has text')) && (!fi || c.fits)
    && q.every(w => (c.description + ' ' + c.people + ' ' + c.setting + ' ' + c.moment).toLowerCase().includes(w)));
  document.getElementById('count').textContent = list.length + ' of ' + CLIPS.length + ' clips';
  document.getElementById('grid').innerHTML = list.map(c => `<div class="card">
    <video src="${{esc(c.rel)}}" poster="${{esc(c.poster)}}" preload="none" muted loop playsinline controls
      onmouseenter="this.play()" onmouseleave="this.pause()"></video>
    <div class="meta"><span class="m">${{esc(c.moment.replace('_', ' / '))}}</span> · ${{c.seconds}}s · ${{esc(c.quality)}}<br>
    ${{esc(c.description)}}<br>${{esc(c.people)}} · ${{esc(c.setting)}}<br>
    <span class="flag">${{c.text !== 'no text' ? esc(c.text) : ''}} ${{c.brand ? 'brand: ' + esc(c.brand) : ''}}</span><br>
    <a href="${{esc(c.rel)}}" download>file</a> · <a href="${{esc(c.source || '#')}}" target="_blank">source</a> @${{esc(c.author)}}
    ${{c.text.startsWith('has text') ? `<br><label><input type="checkbox" ${{picked.has(c.id) ? 'checked' : ''}}
      onchange="pick('${{c.id}}', this)"> remove text</label>` : ''}}</div></div>`).join('');
}}
for (const id of ['q', 'nobrand', 'notext', 'fits']) document.getElementById(id).addEventListener('input', draw);
draw();
</script></body></html>"""


ORDER = ["pain_struggle", "daily_life", "result_relief", "product", "demo_exercise", "other",
         "talking_head", "text_graphic"]  # most useful for ads first


def interleave(recs):
    """Most useful moments first; within a moment, videos take turns so one video can't fill the page."""
    out = []
    for m in ORDER + sorted({r["moment"] for r in recs} - set(ORDER)):
        groups = {}
        for r in recs:
            if r["moment"] == m:
                groups.setdefault(r["id"].rsplit("_", 1)[0], []).append(r)
        while groups:
            for k in list(groups):
                out.append(groups[k].pop(0))
                if not groups[k]:
                    del groups[k]
    return out


def build(slug, log=print):
    con = connect()
    out = config.NICHES / slug
    out.mkdir(parents=True, exist_ok=True)
    recs = interleave([record(r, out) for r in clip_rows(con, slug)])
    (out / "clips.json").write_text(json.dumps(recs, indent=1, ensure_ascii=False), encoding="utf-8")
    with open(out / "clips.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[k for k in recs[0] if k != "rel"] if recs else ["id"], extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    (out / "index.html").write_text(PAGE.format(
        title=html.escape(slug), data=json.dumps(recs, ensure_ascii=False).replace("</", "<\\/"),
        moments=json.dumps(MOMENTS), niche=json.dumps(slug)), encoding="utf-8")
    log(f"gallery: {out / 'index.html'} ({len(recs)} clips)")
    return out / "index.html"


def export(slug, dest, moments=None, no_brand=False, no_text=False, off_niche=False, log=print):
    """Copy the niche's clips into dest/<moment>/ with readable names, for an editor."""
    con = connect()
    dest = pathlib.Path(dest)
    n = 0
    for r in clip_rows(con, slug):
        if moments and r["moment"] not in moments:
            continue
        if (no_brand and r["brand"]) or (no_text and caption_state(r).startswith("has text")):
            continue
        if r["fits_niche"] == 0 and not off_niche:
            continue
        words = "-".join(re.findall(r"[a-z0-9]+", (r["description"] or "").lower())[:8]) or "clip"
        d = dest / r["moment"]
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(best_file(r), d / f"{words}_{r['content_id']}_{r['idx']:02d}.mp4")
        n += 1
    log(f"exported {n} clips to {dest}")
    return n


def find(words, slug=None, limit=20):
    con = connect()
    ws = [w.lower() for w in words.split()]
    hits = []
    for r in clip_rows(con, slug):
        hay = " ".join(str(r[k] or "") for k in ("description", "people", "setting", "moment")).lower()
        score = sum(w in hay for w in ws)
        if score:
            hits.append((score, r))
    hits.sort(key=lambda h: -h[0])
    return [(r, best_file(r)) for _, r in hits[:limit]]
