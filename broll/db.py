"""The bank: one SQLite file that remembers every video ever found, every search, every term and every clip."""
import datetime as dt
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS niches(slug TEXT PRIMARY KEY, seed TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS videos(
    content_id TEXT PRIMARY KEY, url TEXT, author TEXT, author_uid TEXT, description TEXT,
    tags TEXT, duration_ms INTEGER, plays INTEGER, likes INTEGER, shares INTEGER, saves INTEGER,
    posted TEXT, first_seen TEXT,
    status TEXT DEFAULT 'found',      -- found | downloaded | duplicate | failed | skipped
    file TEXT, fingerprint TEXT, duplicate_of TEXT, note TEXT,
    shot_status TEXT,                 -- NULL | tagged | failed
    shot_note TEXT);
CREATE TABLE IF NOT EXISTS links(              -- which niche found which video, and how
    slug TEXT, content_id TEXT, round INTEGER, via TEXT, linked TEXT,
    PRIMARY KEY(slug, content_id));
CREATE TABLE IF NOT EXISTS queries(
    slug TEXT, kind TEXT, key TEXT, text TEXT, round INTEGER, pages INTEGER, results INTEGER,
    new INTEGER, error TEXT, ts TEXT, PRIMARY KEY(slug, kind, key));
CREATE TABLE IF NOT EXISTS terms(
    slug TEXT, kind TEXT, term TEXT,
    status TEXT,                       -- seed | candidate | accepted | rejected
    round INTEGER, videos INTEGER, authors INTEGER, reason TEXT,
    PRIMARY KEY(slug, kind, term));
CREATE TABLE IF NOT EXISTS rounds(slug TEXT, round INTEGER, n_before INTEGER, n_after INTEGER,
    queries INTEGER, accepted INTEGER, ts TEXT, PRIMARY KEY(slug, round));
CREATE TABLE IF NOT EXISTS shots(
    content_id TEXT, idx INTEGER, start REAL, end REAL,
    moment TEXT, usable INTEGER, description TEXT, people TEXT, setting TEXT,
    burned_text INTEGER, brand TEXT, product_visible INTEGER, quality TEXT, why_not TEXT,
    clip TEXT,                         -- cut clip file (usable shots only)
    clean_status TEXT,                 -- NULL | pending | done | failed | not_needed
    clean_clip TEXT, clean_note TEXT,
    fits_niche INTEGER,                -- the shot shows the niche itself, not something next door
    PRIMARY KEY(content_id, idx));
"""

# columns added after the first version; added in place to older bank files
LATE_COLUMNS = {"videos": [("shot_status", "TEXT"), ("shot_note", "TEXT"),
                           ("score", "INTEGER")],  # 0-3: how likely the caption says it holds B-roll
                "shots": [("fits_niche", "INTEGER")]}


def connect():
    config.ensure_dirs()
    con = sqlite3.connect(config.DB, timeout=60)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table, cols in LATE_COLUMNS.items():
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    con.commit()
    return con


def now():
    return dt.datetime.now().isoformat(timespec="seconds")
