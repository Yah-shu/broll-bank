"""Find videos for a niche and grow the search from what they use, like a researcher would.

Each round:
  1. search every approved term not searched yet: hashtag -> the posts under it (paged),
     phrase -> keyword search (TikTok gives a logged-out session page 1 only), then the
     "related" list of the round's most-played videos
  2. every post is stored once in the bank and linked to the niche
  3. discover: hashtags and 2-3 word phrases used by at least 2 different creators in this round's videos
  4. triage: the AI keeps terms that would bring MORE videos of the original niche (never judged
     against earlier expansions, so the search can't drift); rejected terms never come back
  5. stop on the round cap, the video cap, no new approved terms, or 2 rounds in a row that each
     grew the niche by <10% and <10 videos
Then new videos are downloaded once; a repost of a video already in the bank is caught by a frame
fingerprint (also when mirrored) and not kept.
"""
import json
import re
import subprocess

from . import ai, config, dtk
from .db import connect, now

PAGE = 30            # posts per page (TikTok's ceiling is 35)
RELATED_TOP = 5      # videos per round whose "related" list is read
MIN_AUTHORS = 2      # a term must be used by this many different creators to be a candidate
MAX_CANDIDATES = 40
MAX_ACCEPT = 12      # terms approved per round, so a round can't explode or drift
DUR_MIN, DUR_MAX = 3, 180  # seconds; videos outside this are not downloaded

STOP_TAGS = {
    "fyp", "fy", "fypシ", "fypage", "foryou", "foryoupage", "foryourpage", "fyppppppppppppppppppppppp",
    "viral", "viralvideo", "goviral", "blowthisup", "trending", "trend", "explore", "explorepage",
    "tiktok", "tiktokshop", "tiktokmademebuyit", "tiktokshopfinds", "xyzbca", "capcut", "duet", "stitch",
    "greenscreen", "parati", "reels", "shorts", "usa", "ad", "sponsored", "creatorsearchinsights",
    "learnontiktok", "edutok", "ttsacl", "relatable", "funny", "pov",
}
STOP_WORDS = set("""a an the and or but if so of to in on at by for with from up down out over under again
then once here there when where why how all any both each few more most other some such no nor not only own
same than too very can will just don should now is are was were be been being have has had do does did i me my
we our you your he him his she her it its they them their what which who whom this that these those am
im ive id youre its dont cant wont didnt doesnt isnt arent wasnt also get got go going like one two
really much many make made let lets see know think want need try way thing things day days time
yes yeah oh ok okay i a even without after before still every never always ever put take keep give
got gets getting into onto about around through while because since until""".split())

TRIAGE_PROMPT = """You are triaging hashtags and caption phrases found on TikTok videos, to expand a search for B-roll footage.

Niche: {seed}
Terms already in use: {known}

Goal: find MORE videos that show this audience and this problem on camera: people experiencing or showing it,
their daily life and struggles with it, demonstrations, remedies and exercises for it.

For EVERY candidate decide accept or reject. The test for each term: if we searched it on TikTok, would MOST of
the videos show THE ORIGINAL NICHE above ("{seed}")? Judge against that niche only, never against the terms already
in use: those are earlier expansions, and a term that is only close to an expansion drifts away from the niche.
If most videos would be about something wider or next door, reject it.
ACCEPT: names of the condition or problem, its symptoms in everyday words, closely related conditions that look
the same on camera, the audience's own slang and misspellings, specific activities or moments that show it.
REJECT:
- generic reach tags and broad categories: pain, painrelief, health, wellness, selfcare, motivation, mobility,
  physicaltherapy, chronicillness, the body part alone (hands, back, knees), an age group alone (over50)
- creator names and handles, brand or shop names, unrelated topics
- surgery, procedures, diagnosis and awareness-campaign tags, unless the niche is about them (they show hospitals,
  scars and talking heads, not the problem itself)
- anatomy and medical-science words (nerve and tendon names, cartilage): they bring explainer videos
- abbreviations with another common meaning (psa, ra on its own) - prefer the spelled-out term
- more than 2 variants of the same root in one round (e.g. sciatica, sciaticapain, sciaticarelief...):
  keep the 2 most specific to the niche
- phrases that are not a complete idea on their own ("hands feel", "back feels" are fragments;
  "hands feel stiff" is fine), calls to action
Accept at most {max_accept}: the most specific, highest-yield terms first. Reject the rest with a reason.

Return ONLY a JSON object, no prose:
{{"accept": ["term", ...], "reject": [["term", "short reason"], ...]}}

Candidates (kind | term | videos | creators | sample captions):
{cands}
"""


def norm_tag(t):
    return re.sub(r"[^\w]", "", (t or "").lower().lstrip("#"))


def phrase_key(p):
    """Same content words in any order or plural form count as the same keyword search."""
    ws = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9']+", p.lower())]
    return " ".join(sorted(w for w in ws if w not in STOP_WORDS)) or p.lower().strip()


def term_key(kind, term):
    return norm_tag(term) if kind == "hashtag" else (term if kind == "related" else phrase_key(term))


def label(kind, term):
    return {"hashtag": f"#{term}", "related": f"related:{term}"}.get(kind, f'"{term}"')


# ---------------------------------------------------------------- TikTok calls

def search_pages(kind, text, pages):
    """Yields (items, error) per page for one search."""
    if kind == "hashtag":
        code, d = dtk.get("/api/v1/tiktok/hashtag", {"name": text})
        x = (d or {}).get("data") or {}
        info = x.get("data") if isinstance(x.get("data"), dict) else x
        hid = info.get("hashtag_id")
        if not hid:
            yield [], f"hashtag lookup failed ({code}): {json.dumps(d)[:160]}"
            return
        path, params = "/api/v1/tiktok/hashtag/posts", {"hashtag_id": hid, "count": PAGE}
    elif kind == "phrase":
        path, params = "/api/v1/tiktok/search/posts", {"keyword": text, "count": PAGE}
    elif kind == "related":
        path, params, pages = "/api/v1/tiktok/video/related", {"aweme_id": text}, 1
    else:
        raise ValueError(kind)
    cursor = None
    for _ in range(pages):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        code, d = dtk.get(path, p)
        items, cursor, more, err = dtk.unwrap(d)
        yield items, (err if err or code < 400 else f"http {code}")
        if err or not more or not cursor or not items:
            return


# ---------------------------------------------------------------- storing

def store_item(con, it):
    cid = str(it.get("content_id") or "")
    if not cid:
        return None
    a = it.get("author") or {}
    st = it.get("stats") or {}
    vals = (it.get("web_url"), a.get("unique_id") or a.get("nickname"), str(a.get("uid") or ""),
            it.get("description") or it.get("title") or "", json.dumps([norm_tag(t) for t in it.get("tags") or []]),
            it.get("duration_ms"), st.get("play_count"), st.get("digg_count"), st.get("share_count"),
            st.get("collect_count"), it.get("created_at"))
    if con.execute("SELECT 1 FROM videos WHERE content_id=?", (cid,)).fetchone():
        con.execute("""UPDATE videos SET url=?, author=?, author_uid=?, description=?, tags=?, duration_ms=?,
                       plays=?, likes=?, shares=?, saves=?, posted=? WHERE content_id=?""", vals + (cid,))
        return cid
    status = "found" if (it.get("kind") or "video") == "video" else "skipped"
    con.execute("""INSERT INTO videos(url, author, author_uid, description, tags, duration_ms, plays, likes,
                   shares, saves, posted, content_id, first_seen, status, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                vals + (cid, now(), status, None if status == "found" else f"kind {it.get('kind')}"))
    return cid


def link(con, slug, cid, rnd, via):
    return con.execute("INSERT OR IGNORE INTO links(slug, content_id, round, via, linked) VALUES(?,?,?,?,?)",
                       (slug, cid, rnd, via, now())).rowcount == 1


def niche_count(con, slug):
    return con.execute("SELECT COUNT(*) FROM links WHERE slug=?", (slug,)).fetchone()[0]


def matches_niche(con, slug, it):
    """Related posts are recommendations, so keep only those that carry a seed or approved term."""
    terms = con.execute("SELECT kind, term FROM terms WHERE slug=? AND status IN ('seed','accepted')", (slug,)).fetchall()
    tags = {norm_tag(t) for t in it.get("tags") or []}
    text = (it.get("description") or "").lower()
    return any((t["kind"] == "hashtag" and t["term"] in tags) or (t["kind"] == "phrase" and t["term"] in text)
               for t in terms)


def run_query(con, slug, kind, text, rnd, pages, cap, log):
    key = term_key(kind, text)
    if con.execute("SELECT 1 FROM queries WHERE slug=? AND kind=? AND key=?", (slug, kind, key)).fetchone():
        return 0
    results = new = npages = 0
    error = None
    for items, err in search_pages(kind, text, pages):
        npages += 1
        error = err or error
        for it in items:
            if kind == "related" and not matches_niche(con, slug, it):
                continue
            cid = store_item(con, it)
            if not cid:
                continue
            results += 1
            if link(con, slug, cid, rnd, f"{kind}:{text}"):
                new += 1
        con.commit()
        if niche_count(con, slug) >= cap:
            break
    con.execute("INSERT OR REPLACE INTO queries VALUES(?,?,?,?,?,?,?,?,?,?)",
                (slug, kind, key, text, rnd, npages, results, new, error, now()))
    con.commit()
    log(f"  {label(kind, text)}: {results} posts, {new} new" + (f"  [{error}]" if error else ""))
    return new


# ---------------------------------------------------------------- learning loop

def clean_words(desc):
    desc = re.sub(r"https?://\S+|[#@]\w+", " ", (desc or "").lower())
    desc = desc.replace("'", "").replace("’", "")  # can't -> cant, so it counts as a stop word
    return re.findall(r"[a-z][a-z]*", desc)  # keep "i"/"a" so phrases can't skip a word ("matter what eat")


def discover(con, slug, rnd):
    rows = con.execute("""SELECT v.content_id, v.author_uid, v.tags, v.description FROM links l
                          JOIN videos v USING(content_id) WHERE l.slug=? AND l.round=?""", (slug, rnd)).fetchall()
    known = {(r["kind"], r["term"]) for r in con.execute("SELECT kind, term FROM terms WHERE slug=?", (slug,))}
    counts, authors, samples = {}, {}, {}
    for r in rows:
        au = r["author_uid"] or r["content_id"]
        found = set()
        for t in set(json.loads(r["tags"] or "[]")):
            if t and t not in STOP_TAGS and len(t) >= 3 and not t.isdigit():
                found.add(("hashtag", t))
        ws = clean_words(r["description"])
        for n in (2, 3):
            for i in range(len(ws) - n + 1):
                g = ws[i:i + n]
                if g[0] not in STOP_WORDS and g[-1] not in STOP_WORDS:
                    found.add(("phrase", " ".join(g)))
        for key in found:
            counts[key] = counts.get(key, 0) + 1
            authors.setdefault(key, set()).add(au)
            samples.setdefault(key, []).append(r["description"])
    cands = [(len(authors[k]), counts[k], k[0], k[1]) for k in counts
             if len(authors[k]) >= MIN_AUTHORS and k not in known]
    # drop a phrase inside a longer candidate phrase that is used at least as widely
    phrases = [c for c in cands if c[2] == "phrase"]
    cands = [c for c in cands if not (c[2] == "phrase" and any(
        o[3] != c[3] and c[3] in o[3] and o[0] >= c[0] for o in phrases))]
    cands.sort(key=lambda c: (-c[0], -c[1], c[3]))
    cands = cands[:MAX_CANDIDATES]
    for na, nv, kind, t in cands:
        con.execute("INSERT OR IGNORE INTO terms VALUES(?,?,?,?,?,?,?,?)", (slug, kind, t, "candidate", rnd, nv, na, None))
    con.commit()
    return [(kind, t, nv, na, [s for s in samples[(kind, t)] if s][:2]) for na, nv, kind, t in cands]


def triage(con, slug, seed, cands, rnd, use_ai, log):
    if not cands:
        return []
    acc, rej = set(), {}
    if use_ai:
        known = ", ".join(r["term"] for r in con.execute(
            "SELECT term FROM terms WHERE slug=? AND status IN ('seed','accepted')", (slug,)))
        lines = "\n".join(f"{k} | {t} | {nv} | {na} | " + " /// ".join(re.sub(r"\s+", " ", s)[:140] for s in sm)
                          for k, t, nv, na, sm in cands)
        try:
            res = ai.ask_json(TRIAGE_PROMPT.format(seed=seed, known=known, cands=lines, max_accept=MAX_ACCEPT))
            acc = set([s.lower().lstrip("#").strip() for s in res.get("accept", [])][:MAX_ACCEPT])
            rej = {(r[0] if isinstance(r, list) else r).lower().lstrip("#").strip():
                   (r[1] if isinstance(r, list) and len(r) > 1 else "") for r in res.get("reject", [])}
        except Exception as e:  # noqa: BLE001 - any AI failure falls back to the code rule
            log(f"  term check by AI failed ({str(e)[:200]}); using the code rule instead")
            use_ai = False
    accepted = []
    for k, t, nv, na, _ in cands:
        if use_ai:
            ok = t in acc  # exact: approving #carpaltunnel must not also approve the phrase "carpal tunnel"
            reason = "" if ok else rej.get(t, "not accepted")
        else:
            ok = na >= 3 and len(accepted) < MAX_ACCEPT
            reason = "" if ok else "code rule: <3 creators or round cap"
        con.execute("UPDATE terms SET status=?, reason=?, round=? WHERE slug=? AND kind=? AND term=?",
                    ("accepted" if ok else "rejected", reason, rnd, slug, k, t))
        if ok:
            accepted.append((k, t))
    con.commit()
    return accepted


PLAN_PROMPT = """We are collecting B-roll for ads about: {seed}

B-roll is real footage of MOMENTS: people experiencing the problem (pain, struggle, failing at an everyday task,
frustration), their daily life with it, trying remedies, and relief afterwards. Tutorials, talking heads and
explainers are the least useful.

Suggest TikTok searches that surface such footage for this niche:
- up to 6 hashtags people use when they film themselves living with it (no '#', lowercase, no spaces),
  e.g. the condition plus words like pov, flare, struggle, life, check, day
- up to 6 short keyword searches describing a filmable moment (2-5 words), e.g. "can't open jar arthritis"
Stay on the niche itself; no broad tags (pain, health, fyp), no brand names.

Return ONLY JSON: {{"hashtags": [...], "phrases": [...]}}
"""


def plan(con, slug, seed, log):
    """Before the first round: AI-suggested starting searches aimed at filmable moments, not tutorials."""
    try:
        res = ai.ask_json(PLAN_PROMPT.format(seed=seed))
    except Exception as e:  # noqa: BLE001 - planning is a bonus; the seed alone still works
        log(f"  (search planning skipped: {str(e)[:150]})")
        return
    added = []
    for kind, key in (("hashtag", "hashtags"), ("phrase", "phrases")):
        for t in (res.get(key) or [])[:6]:
            t = norm_tag(t) if kind == "hashtag" else str(t).lower().strip()
            if t and (kind != "hashtag" or t not in STOP_TAGS):
                con.execute("INSERT OR IGNORE INTO terms VALUES(?,?,?,?,?,?,?,?)",
                            (slug, kind, t, "seed", 0, None, None, "planned"))
                added.append(label(kind, t))
    con.commit()
    log(f"  planned starting searches: {', '.join(added)}")


def search(slug, seed=None, tags="", rounds=2, max_videos=300, pages=2, keyword_pages=1, use_ai=True, log=print):
    con = connect()
    row = con.execute("SELECT seed FROM niches WHERE slug=?", (slug,)).fetchone()
    seed = seed or (row["seed"] if row else None)
    if not seed:
        raise SystemExit("The first search for a niche needs --seed \"what to look for\".")
    con.execute("INSERT OR IGNORE INTO niches VALUES(?,?,?)", (slug, seed, now()))
    seeds = [("phrase", seed.lower().strip()), ("hashtag", norm_tag(seed))]
    seeds += [("hashtag", norm_tag(t)) for t in (tags or "").split(",") if norm_tag(t)]
    for k, t in seeds:
        con.execute("INSERT OR IGNORE INTO terms VALUES(?,?,?,?,?,?,?,?)", (slug, k, t, "seed", 0, None, None, None))
    con.commit()
    start = (con.execute("SELECT MAX(round) FROM rounds WHERE slug=?", (slug,)).fetchone()[0] or 0) + 1
    if start == 1 and use_ai and not con.execute(
            "SELECT 1 FROM terms WHERE slug=? AND reason='planned'", (slug,)).fetchone():
        plan(con, slug, seed, log)
    log(f"[{slug}] \"{seed}\": starting round {start}; {niche_count(con, slug)} videos so far")
    stop = f"round limit ({rounds}) reached"
    for rnd in range(start, start + rounds):
        before = niche_count(con, slug)
        done = {(r["kind"], r["key"]) for r in con.execute("SELECT kind, key FROM queries WHERE slug=?", (slug,))}
        pending = [(r["kind"], r["term"]) for r in con.execute(
            # phrases first: they are the targeted moment searches and cost one page each
            """SELECT kind, term FROM terms WHERE slug=? AND status IN ('seed','accepted')
               ORDER BY round, CASE kind WHEN 'phrase' THEN 0 ELSE 1 END, term""",
            (slug,)) if (r["kind"], term_key(r["kind"], r["term"])) not in done]
        if not pending:
            stop = "no new terms to search"
            break
        log(f"\nround {rnd}: {len(pending)} searches")
        for k, t in pending:
            run_query(con, slug, k, t, rnd, pages if k == "hashtag" else keyword_pages, max_videos, log)
            if niche_count(con, slug) >= max_videos:
                break
        top = con.execute("""SELECT v.content_id FROM links l JOIN videos v USING(content_id)
                             WHERE l.slug=? AND l.round=? ORDER BY v.plays DESC LIMIT ?""",
                          (slug, rnd, RELATED_TOP)).fetchall()
        for r in top:
            if niche_count(con, slug) >= max_videos:
                break
            run_query(con, slug, "related", r["content_id"], rnd, 1, max_videos, log)
        after = niche_count(con, slug)
        cands = discover(con, slug, rnd)
        acc = triage(con, slug, seed, cands, rnd, use_ai, log)
        con.execute("INSERT OR REPLACE INTO rounds VALUES(?,?,?,?,?,?,?)",
                    (slug, rnd, before, after, len(pending), len(acc), now()))
        con.commit()
        log(f"round {rnd}: {before} -> {after} videos (+{after - before}); {len(cands)} new terms seen, "
            f"{len(acc)} approved: {', '.join(label(k, t) for k, t in acc) or '-'}")
        last2 = con.execute("SELECT n_before, n_after FROM rounds WHERE slug=? ORDER BY round DESC LIMIT 2",
                            (slug,)).fetchall()
        if after >= max_videos:
            stop = f"video cap {max_videos} reached (approved terms not searched yet stay queued for next time)"
            break
        if not acc:
            stop = "no new terms approved"
            break
        if len(last2) == 2 and all((r["n_after"] - r["n_before"]) < max(10, 0.10 * r["n_before"]) for r in last2):
            stop = "saturated: the last 2 rounds each grew the niche by <10% and <10 videos"
            break
    log(f"\nsearch stopped: {stop}. The niche has {niche_count(con, slug)} videos.")
    return con


def retriage(slug, rnd, use_ai=True, log=print):
    """Re-judge one round's terms (e.g. after the rules changed). Terms already searched stay."""
    con = connect()
    seed = con.execute("SELECT seed FROM niches WHERE slug=?", (slug,)).fetchone()["seed"]
    done = {(r["kind"], r["key"]) for r in con.execute("SELECT kind, key FROM queries WHERE slug=?", (slug,))}
    reset = 0
    for r in con.execute("SELECT kind, term FROM terms WHERE slug=? AND round=? AND status!='seed'", (slug, rnd)).fetchall():
        if (r["kind"], term_key(r["kind"], r["term"])) not in done:
            con.execute("DELETE FROM terms WHERE slug=? AND kind=? AND term=?", (slug, r["kind"], r["term"]))
            reset += 1
    con.commit()
    cands = discover(con, slug, rnd)
    acc = triage(con, slug, seed, cands, rnd, use_ai, log)
    con.execute("UPDATE rounds SET accepted=? WHERE slug=? AND round=?", (len(acc), slug, rnd))
    con.commit()
    log(f"re-judged {len(cands)} terms from round {rnd} ({reset} reset); approved {len(acc)}: "
        + ", ".join(label(k, t) for k, t in acc))


def set_term(slug, term, decision):
    """The user's call overrides the AI: approve or block a term (a blocked term is never searched or suggested)."""
    con = connect()
    kind = "hashtag" if term.startswith("#") else "phrase"
    term = norm_tag(term) if kind == "hashtag" else term.lower().strip()
    status = "accepted" if decision == "accept" else "rejected"
    rnd = con.execute("SELECT COALESCE(MAX(round), 0) FROM rounds WHERE slug=?", (slug,)).fetchone()[0]
    con.execute("""INSERT INTO terms(slug, kind, term, status, round, reason) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(slug, kind, term) DO UPDATE SET status=excluded.status, reason=excluded.reason""",
                (slug, kind, term, status, rnd, "set by hand"))
    con.commit()
    return f"{label(kind, term)}: {'approved' if status == 'accepted' else 'blocked'}"


# ---------------------------------------------------------------- download + repost check

def frame_hashes(path, dur_s):
    """dHash (64 bit) of 3 frames, straight and mirrored, so flipped reposts still match."""
    out = []
    for frac in (0.15, 0.5, 0.85):
        t = max(0.5, (dur_s or 6) * frac)
        try:
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                                  "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-"],
                                 capture_output=True, timeout=60).stdout
        except subprocess.TimeoutExpired:
            raw = b""
        if len(raw) < 72:
            continue
        h = hm = 0
        for y in range(8):
            r = raw[y * 9:(y + 1) * 9]
            mr = r[::-1]
            for x in range(8):
                h = (h << 1) | (r[x] > r[x + 1])
                hm = (hm << 1) | (mr[x] > mr[x + 1])
        out.append((h, hm))
    return out


def is_dup(fp_a, fp_b, bits=8):
    """At least 2 of the 3 frames match (straight or mirrored) within `bits` differing bits."""
    hits = 0
    for h, hm in fp_a:
        if any(bin(h ^ g).count("1") <= bits or bin(hm ^ g).count("1") <= bits for g, _ in fp_b):
            hits += 1
    return hits >= 2


RANK_PROMPT = """We collect B-roll for ads about: {seed}
Useful: real footage of people experiencing it (pain, struggle, everyday tasks going wrong), daily life with it,
remedies being tried on real people, relief. Less useful: talking-head explainers, tutorials narrated to camera,
slideshows, memes, unrelated topics.

From each TikTok's caption and hashtags, score how likely the video contains useful footage:
3 = very likely (pov, day in my life, flare up, struggle, storytime with footage)
2 = likely (demos or remedies done on real people, product being used)
1 = unlikely (explainer, talking head, tips list, doctor speaking)
0 = no (unrelated, photo slideshow, pure graphics, giveaway)

Return ONLY JSON: an object mapping each id to its score, e.g. {{"7123": 2, "7456": 0}}

Videos (id | caption):
{rows}
"""
RANK_BATCH = 60


def rank(con, slug, seed, log):
    """Score not-yet-downloaded videos from their captions, so the likeliest B-roll is downloaded first."""
    rows = con.execute("""SELECT v.content_id, v.description, v.tags FROM links l JOIN videos v USING(content_id)
                          WHERE l.slug=? AND v.status='found' AND v.score IS NULL""", (slug,)).fetchall()
    if not rows:
        return
    log(f"  ranking {len(rows)} videos by caption")
    for k in range(0, len(rows), RANK_BATCH):
        part = rows[k:k + RANK_BATCH]
        lines = "\n".join(
            f"{r['content_id']} | " + re.sub(r"\s+", " ", r["description"] or "")[:220] + " "
            + " ".join("#" + t for t in json.loads(r["tags"] or "[]") if f"#{t}" not in (r["description"] or ""))[:120]
            for r in part)
        try:
            res = ai.ask_json(RANK_PROMPT.format(seed=seed, rows=lines))
        except Exception as e:  # noqa: BLE001 - unranked videos still download, by plays
            log(f"  (ranking a batch failed: {str(e)[:150]})")
            continue
        for r in part:
            sc = res.get(r["content_id"]) if isinstance(res, dict) else None
            if isinstance(sc, (int, float)):
                con.execute("UPDATE videos SET score=? WHERE content_id=?", (int(sc), r["content_id"]))
        con.commit()


def download_new(slug, limit, log=print, use_ai=True):
    con = connect()
    if use_ai:
        seed = (con.execute("SELECT seed FROM niches WHERE slug=?", (slug,)).fetchone() or {"seed": slug})["seed"]
        rank(con, slug, seed, log)
    rows = con.execute("""SELECT v.* FROM links l JOIN videos v USING(content_id)
                          WHERE l.slug=? AND v.status='found' AND COALESCE(v.score, 1) > 0
                          ORDER BY COALESCE(v.score, 1) DESC, v.plays DESC""", (slug,)).fetchall()
    known = [(r["content_id"], json.loads(r["fingerprint"])) for r in con.execute(
        "SELECT content_id, fingerprint FROM videos WHERE status='downloaded' AND fingerprint IS NOT NULL")]
    done = 0
    for r in rows:
        if done >= limit:
            break
        dur = (r["duration_ms"] or 0) / 1000
        if dur and not (DUR_MIN <= dur <= DUR_MAX):
            con.execute("UPDATE videos SET status='skipped', note=? WHERE content_id=?", (f"length {dur:.0f}s", r["content_id"]))
            con.commit()
            continue
        dest = config.VIDEOS / f"{r['content_id']}.mp4"
        ok, msg = dtk.download(r["content_id"], dest)
        if not ok:
            con.execute("UPDATE videos SET status='failed', note=? WHERE content_id=?", (msg[:300], r["content_id"]))
            con.commit()
            log(f"  {r['content_id']}: download failed: {msg[:120]}")
            continue
        fp = frame_hashes(dest, dur)
        dup = next((cid for cid, f in known if f and is_dup(fp, f)), None)
        if dup:
            dest.unlink(missing_ok=True)
            con.execute("UPDATE videos SET status='duplicate', duplicate_of=?, fingerprint=? WHERE content_id=?",
                        (dup, json.dumps(fp), r["content_id"]))
            log(f"  {r['content_id']}: a repost of {dup}, not kept")
        else:
            con.execute("UPDATE videos SET status='downloaded', file=?, fingerprint=? WHERE content_id=?",
                        (str(dest), json.dumps(fp), r["content_id"]))
            known.append((r["content_id"], fp))
            done += 1
            log(f"  {r['content_id']}: {msg}")
        con.commit()
    log(f"downloaded {done} new videos")
    return done
