"""Command line. `python broll.py <command> --help` for details."""
import argparse
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys

from . import ai, clean, config, dtk, gallery, search, shots
from .db import connect, now


def log_to(slug):
    config.ensure_dirs()
    fh = open(config.LOGS / f"{slug}.log", "a", encoding="utf-8")

    def log(msg):
        print(msg, flush=True)
        fh.write(msg + "\n")
        fh.flush()
    return log


def cmd_run(a):
    log = log_to(a.niche)
    log(f"\n=== run {a.niche} {now()} ===")
    search.search(a.niche, a.seed, a.tags, a.rounds, a.max_videos, a.pages, use_ai=not a.no_ai, log=log)
    log("\n--- download")
    search.download_new(a.niche, a.max_downloads, log, use_ai=not a.no_ai)
    log("\n--- cut + tag shots")
    shots.process(a.niche, log=log)
    if not a.skip_clean and clean.available():
        log("\n--- remove captions")
        clean.clean_pending(a.niche, a.max_clean, clean.DEFAULT_MODES, log=log)
    elif not a.skip_clean:
        log("\n(caption removal is not installed; clips with text are kept and marked 'has text')")
    log("\n--- gallery")
    gallery.build(a.niche, log)
    status(a.niche)


def status(slug=None):
    con = connect()
    slugs = [slug] if slug else [r["slug"] for r in con.execute("SELECT slug FROM niches ORDER BY created")]
    tot = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM videos GROUP BY status")}
    print(f"\nbank: {sum(tot.values())} videos known ({', '.join(f'{k} {v}' for k, v in sorted(tot.items()))})")
    for s in slugs:
        v = {r["status"]: r["n"] for r in con.execute(
            "SELECT v.status, COUNT(*) n FROM links l JOIN videos v USING(content_id) WHERE l.slug=? GROUP BY v.status", (s,))}
        tagged = con.execute("""SELECT COUNT(*) FROM links l JOIN videos v USING(content_id)
                                WHERE l.slug=? AND v.shot_status='tagged'""", (s,)).fetchone()[0]
        c = con.execute("""SELECT COUNT(*) n, SUM(s.usable) u, SUM(s.clean_status='pending') p, SUM(s.clean_status='done') d
                           FROM shots s JOIN links l ON l.content_id=s.content_id WHERE l.slug=?""", (s,)).fetchone()
        moments = {r["moment"]: r["n"] for r in con.execute(
            """SELECT s.moment, COUNT(*) n FROM shots s JOIN links l ON l.content_id=s.content_id
               WHERE l.slug=? AND s.usable=1 GROUP BY s.moment ORDER BY n DESC""", (s,))}
        t = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM terms WHERE slug=? GROUP BY status", (s,))}
        print(f"[{s}] videos {sum(v.values())} ({', '.join(f'{k} {n}' for k, n in sorted(v.items()))}); "
              f"{tagged} tagged; shots {c['n']}, usable {c['u'] or 0} {moments}; "
              f"text to remove {c['p'] or 0}, removed {c['d'] or 0}; terms {t}")
        page = config.NICHES / s / "index.html"
        if page.exists():
            print(f"   gallery: {page}")


def cmd_terms(a):
    con = connect()
    for st in ("seed", "accepted", "rejected", "candidate"):
        rows = con.execute("SELECT * FROM terms WHERE slug=? AND status=? ORDER BY round, authors DESC", (a.niche, st)).fetchall()
        if rows:
            print(f"\n{ {'accepted': 'approved'}.get(st, st)} ({len(rows)}):")
            for r in rows:
                print(f"  r{r['round']} {search.label(r['kind'], r['term'])}  [{r['videos'] or '-'} videos / "
                      f"{r['authors'] or '-'} creators]" + (f"  {r['reason']}" if r["reason"] else ""))


def cmd_register(a):
    """Mark mp4s named <content_id>.mp4 as already pulled, so they are never downloaded again."""
    con = connect()
    n = 0
    for f in sorted(pathlib.Path(a.dir).glob("*.mp4")):
        m = re.match(r"(\d{15,})", f.stem)
        if not m:
            continue
        dur = shots.probe_duration(f)
        fp = json.dumps(search.frame_hashes(f, dur))
        con.execute("""INSERT INTO videos(content_id, first_seen, status, file, fingerprint, duration_ms, note)
                       VALUES(?,?,?,?,?,?,?) ON CONFLICT(content_id) DO UPDATE SET status='downloaded',
                       file=excluded.file, fingerprint=excluded.fingerprint""",
                    (m.group(1), now(), "downloaded", str(f.resolve()), fp, int(dur * 1000) or None, f"registered from {a.dir}"))
        if a.niche:
            search.link(con, a.niche, m.group(1), 0, "registered")
        n += 1
    con.commit()
    print(f"registered {n} videos from {a.dir}")


def cmd_forget(a):
    """Delete a niche: its searches and terms, and every video, clip and file only it uses."""
    con = connect()
    if not con.execute("SELECT 1 FROM niches WHERE slug=?", (a.niche,)).fetchone() and not con.execute(
            "SELECT 1 FROM links WHERE slug=?", (a.niche,)).fetchone():
        raise SystemExit(f"no niche called {a.niche}")
    only = [r["content_id"] for r in con.execute(
        """SELECT content_id FROM links WHERE slug=? AND content_id NOT IN
           (SELECT content_id FROM links WHERE slug!=?)""", (a.niche, a.niche))]
    files = 0
    for cid in only:
        paths = [config.VIDEOS / f"{cid}.mp4"]
        for r in con.execute("SELECT clip, clean_clip FROM shots WHERE content_id=?", (cid,)):
            for f in (r["clip"], r["clean_clip"]):
                if f:
                    paths += [pathlib.Path(f), pathlib.Path(f).with_suffix(".jpg")]
        paths += list(config.CLIPS.glob(f"{cid}_*"))
        for f in paths:
            if f.exists():
                f.unlink()
                files += 1
        shutil.rmtree(config.WORK / "strips" / cid, ignore_errors=True)
        con.execute("DELETE FROM shots WHERE content_id=?", (cid,))
        con.execute("DELETE FROM videos WHERE content_id=?", (cid,))
    for table in ("links", "queries", "terms", "rounds", "niches"):
        con.execute(f"DELETE FROM {table} WHERE slug=?", (a.niche,))
    con.commit()
    shutil.rmtree(config.NICHES / a.niche, ignore_errors=True)
    (config.LOGS / f"{a.niche}.log").unlink(missing_ok=True)
    print(f"forgot {a.niche}: {len(only)} videos and {files} files deleted "
          "(videos other niches also use were kept)")


def cmd_service(a):
    """Start / stop / show the TikTok service (Docker). Docker Desktop must be running."""
    if not config.DTK_DIR:
        raise SystemExit("The TikTok service wasn't installed by install.py (no dtk_dir in config.json).")
    d = pathlib.Path(config.DTK_DIR)
    cmd = ["docker", "compose", "-p", "dtk", "-f", "docker/compose.yml"]
    if (d / "docker" / "compose.host.yml").exists():
        cmd += ["-f", "docker/compose.host.yml"]
    env = dict(os.environ, COMPOSE_ENV_FILES=".env", COMPOSE_PROFILES="browser,downloader")
    args = {"start": ["up", "-d", "--wait"], "stop": ["stop"], "status": ["ps"]}[a.action]
    p = subprocess.run(cmd + args, cwd=d, env=env)
    if a.action == "start" and p.returncode == 0:
        print("TikTok service is up" if dtk.health() else "started, but not answering yet; wait a minute")
    sys.exit(p.returncode)


def cmd_find(a):
    for r, f in gallery.find(a.words, a.niche, a.limit):
        print(f"{r['moment']:<14} {r['end'] - r['start']:4.1f}s  {r['description']}\n               {f}")


def cmd_doctor(a):
    ok = True

    def check(name, good, fix=""):
        nonlocal ok
        ok &= bool(good)
        print(f"[{'OK' if good else 'FAIL'}] {name}" + ("" if good else f"\n       fix: {fix}"))

    check(f"python {platform.python_version()}", sys.version_info >= (3, 10), "install Python 3.10 or newer")
    for tool in ("ffmpeg", "ffprobe"):
        check(tool, shutil.which(tool), f"install ffmpeg and put {tool} on PATH")
    if shutil.which("ffmpeg"):
        filt = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        check("ffmpeg has the scdet (scene cut) filter", " scdet " in filt, "install a recent full ffmpeg build (5.0+)")
    check(f"data folder {config.HOME}", config.HOME.exists() or config.HOME.parent.exists(), "set 'home' in config.json")
    check(f"TikTok service reachable at {config.DTK_URL}", dtk.health(),
          "start Docker Desktop, then: python broll.py service start")
    try:
        key_ok = bool(config.dtk_key())
    except SystemExit as e:
        key_ok = False
        print(f"       {e}")
    check("TikTok service API key", key_ok, "run: python install.py --only dtk")
    if key_ok and dtk.health():
        code, d = dtk.call("GET", "/api/v1/tiktok/hashtag", {"name": "cat"})
        check("TikTok search answers", code < 400 and (d or {}).get("success") is not False,
              f"got {code}: {json.dumps(d)[:200]}")
    try:
        name, exe = ai.backend()
        check(f"AI: {name} ({exe})", True)
        if a.ai_test:
            reply = ai.ask_json('Reply with exactly this JSON and nothing else: {"ok": true}')
            check("AI answers", reply.get("ok") is True, f"log in to {name} and try again")
    except SystemExit as e:
        check("AI: claude or codex installed", False, str(e))
    vsr = clean.available()
    check("caption removal installed", vsr, "run: python install.py --only vsr")
    if vsr:
        p = subprocess.run([config.VSR_PYTHON, "-c", "import torch, paddle; print(torch.__version__)"],
                           capture_output=True, text=True)
        check("caption removal python imports", p.returncode == 0, (p.stderr or "")[-300:])
    free = shutil.disk_usage(config.HOME if config.HOME.exists() else config.HOME.parent).free / 2**30
    check(f"free disk {free:.0f} GB", free > 10, "free up space; a niche uses about 1-3 GB")
    print("\nall good" if ok else "\nsome checks failed (see fixes above)")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(prog="broll", description="Find TikTok B-roll for a niche, cut it into tagged clips.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="everything: search, download, cut + tag, remove captions, gallery")
    r.add_argument("niche", help="short name, e.g. stiff-hands")
    r.add_argument("--seed", help='what to look for, e.g. "stiff hands" (needed the first time)')
    r.add_argument("--tags", default="", help="extra starting hashtags, comma separated")
    r.add_argument("--rounds", type=int, default=2, help="search rounds this time (default 2)")
    r.add_argument("--max-videos", type=int, default=300, help="stop searching at this many videos in the niche")
    r.add_argument("--max-downloads", type=int, default=40, help="new videos to download this time")
    r.add_argument("--max-clean", type=int, default=30, help="clips to remove captions from this time (slow step)")
    r.add_argument("--pages", type=int, default=2, help="pages per hashtag, 30 posts each")
    r.add_argument("--no-ai", action="store_true", help="approve search terms by a code rule instead of the AI")
    r.add_argument("--skip-clean", action="store_true", help="don't remove captions this time")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("search", help="only the search rounds")
    s.add_argument("niche")
    s.add_argument("--seed")
    s.add_argument("--tags", default="")
    s.add_argument("--rounds", type=int, default=2)
    s.add_argument("--max-videos", type=int, default=300)
    s.add_argument("--pages", type=int, default=2)
    s.add_argument("--no-ai", action="store_true")
    s.set_defaults(fn=lambda a: search.search(a.niche, a.seed, a.tags, a.rounds, a.max_videos, a.pages,
                                              use_ai=not a.no_ai, log=log_to(a.niche)))

    d = sub.add_parser("download", help="download videos found but not pulled yet")
    d.add_argument("niche")
    d.add_argument("--max", type=int, default=40)
    d.set_defaults(fn=lambda a: search.download_new(a.niche, a.max, log_to(a.niche)))

    t = sub.add_parser("tag", help="cut + tag shots of downloaded videos")
    t.add_argument("niche")
    t.add_argument("--max", type=int)
    t.add_argument("--retry-failed", action="store_true")
    t.add_argument("--redo", action="store_true", help="tag every video again (drops their old clips)")
    t.set_defaults(fn=lambda a: shots.process(a.niche, a.max, a.retry_failed, a.redo, log_to(a.niche)))

    c = sub.add_parser("clean", help="remove captions from usable clips that have text")
    c.add_argument("niche")
    c.add_argument("--max", type=int, help="clean the best N clips that still have text")
    c.add_argument("--ids", help="exactly these clips, comma separated (the gallery's 'copy clean command' gives this)")
    c.add_argument("--mode", default=clean.DEFAULT_MODES,
                   help=f"method(s) tried in order until the result passes the check; from {clean.ALLOWED_MODES}")
    c.set_defaults(fn=lambda a: clean.clean_pending(a.niche, a.max, a.mode, log_to(a.niche),
                                                    ids=[i for i in (a.ids or "").split(",") if i.strip()] or None))

    g = sub.add_parser("gallery", help="rebuild the niche's index.html")
    g.add_argument("niche")
    g.set_defaults(fn=lambda a: gallery.build(a.niche))

    e = sub.add_parser("export", help="copy clips into folders per moment, for an editor")
    e.add_argument("niche")
    e.add_argument("dest")
    e.add_argument("--moment", action="append", choices=list(shots.MOMENTS), help="only these moments (repeatable)")
    e.add_argument("--no-brand", action="store_true", help="skip clips showing a brand or logo")
    e.add_argument("--no-text", action="store_true", help="skip clips that still have text on them")
    e.add_argument("--off-niche", action="store_true", help="also shots that don't show the niche itself")
    e.set_defaults(fn=lambda a: gallery.export(a.niche, a.dest, a.moment, a.no_brand, a.no_text, a.off_niche))

    f = sub.add_parser("find", help="search clips by words, e.g. find \"woman jar kitchen\"")
    f.add_argument("words")
    f.add_argument("--niche")
    f.add_argument("--limit", type=int, default=20)
    f.set_defaults(fn=cmd_find)

    sv = sub.add_parser("service", help="start / stop / status of the TikTok service (Docker)")
    sv.add_argument("action", choices=["start", "stop", "status"])
    sv.set_defaults(fn=cmd_service)

    fo = sub.add_parser("forget", help="delete a niche and every video/clip only it uses")
    fo.add_argument("niche")
    fo.set_defaults(fn=cmd_forget)

    st = sub.add_parser("status", help="what the bank holds")
    st.add_argument("niche", nargs="?")
    st.set_defaults(fn=lambda a: status(a.niche))

    te = sub.add_parser("terms", help="search terms approved / blocked and why")
    te.add_argument("niche")
    te.set_defaults(fn=cmd_terms)

    se = sub.add_parser("set", help="approve or block a search term yourself")
    se.add_argument("niche")
    se.add_argument("term", help='"#hashtag" or a phrase')
    se.add_argument("decision", choices=["accept", "block"])
    se.set_defaults(fn=lambda a: print(search.set_term(a.niche, a.term, a.decision)))

    rt = sub.add_parser("retriage", help="re-judge a round's terms that weren't searched yet")
    rt.add_argument("niche")
    rt.add_argument("round", type=int)
    rt.set_defaults(fn=lambda a: search.retriage(a.niche, a.round))

    rg = sub.add_parser("register", help="mark existing <content_id>.mp4 files as already pulled")
    rg.add_argument("dir")
    rg.add_argument("--niche")
    rg.set_defaults(fn=cmd_register)

    dr = sub.add_parser("doctor", help="check the install")
    dr.add_argument("--ai-test", action="store_true", help="also send one tiny prompt to the AI")
    dr.set_defaults(fn=cmd_doctor)

    a = ap.parse_args()
    a.fn(a)
