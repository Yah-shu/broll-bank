# B-roll bank: instructions for the AI agent (Codex, Claude Code, ...)

The person you're helping wants **B-roll for ads**: short real-footage clips of a niche ("stiff hands",
"lower back pain") taken from TikTok videos, cut into shots, tagged by what moment they show, with burned-in
captions removed. This repo does all of it from one command. Your job: install it on their computer, run it
for the niches they ask for, and show them the results. They may not be technical; explain in plain words.

## What it does

`python broll.py run <niche> --seed "<what to look for>"`:
1. **Search**: the AI first plans searches aimed at filmable moments ("back pain getting out of bed",
   "#backspasm"), then the tool grows the search like a researcher: it collects the hashtags and phrases the found
   videos use, the AI keeps only the ones that bring more of *the original niche* (it can't drift), and it
   searches those next round.
2. **Download**: the AI ranks every found video by its caption (real footage of the moment vs. talking-head
   tutorial) and the likeliest B-roll is downloaded first, without watermark. Every video ever found is remembered,
   so nothing is downloaded twice, and reposts (even mirrored) are caught.
3. **Cut + tag**: each video is cut at scene changes (pieces of 6 s max); the AI looks at every shot and tags the
   moment (`pain_struggle`, `daily_life`, `demo_exercise`, `product`, `result_relief`, `talking_head`,
   `text_graphic`), whether it's usable B-roll, whether it fits the niche, who/where, burned-in text, brand/logo.
   Usable shots become clips.
4. **Remove captions** from the best clips that have text (the slow step, ~2-5 min per clip), with an AI
   before/after check: a clip is only replaced if the result really looks clean.
5. **Gallery**: `<home>/niches/<niche>/index.html`, a page with every clip, filters per moment, search, and
   checkboxes to pick more clips for caption removal.

The AI work (judging search terms, tagging shots, checking cleaned clips) runs through the person's own
**Claude Code** (`claude`) or **Codex** (`codex`) command line, on their subscription. No API keys.

## Install (once)

Ask the person before installing system software, and tell them what each thing is for. Re-running any step is
safe. Commands are for a normal terminal (PowerShell on Windows, Terminal on Mac).

1. **Git, ffmpeg, Python 3.12** (Python 3.12 exactly, for caption removal; it can sit next to other versions):
   - Windows: `winget install -e --id Git.Git`, `winget install -e --id Gyan.FFmpeg`,
     `winget install -e --id Python.Python.3.12`, then open a NEW terminal so PATH updates.
   - Mac: install Homebrew if missing (https://brew.sh), then `brew install git ffmpeg python@3.12`.
2. **Docker Desktop** (runs the TikTok search/download service):
   - Windows: needs admin rights and WSL2. Run `wsl --install` in an admin PowerShell if `wsl --status` says it's
     not installed, then `winget install -e --id Docker.DockerDesktop`. A **reboot** is usually needed; tell
     the person before. After the reboot, open Docker Desktop once and accept its terms.
   - Mac (Apple Silicon or Intel): `brew install --cask docker`, then open Docker Desktop once.
   - Wait until Docker Desktop shows it's running (`docker info` works).
3. **This tool**: from this repo folder run `python install.py` (on Windows `py -3.12 install.py` also works).
   It takes 15-40 minutes the first time (several GB of downloads). It:
   - sets up the TikTok service in Docker (pinned version), creates its admin account and an API key
     (saved under `<home>/secrets/`),
   - installs caption removal for this machine's graphics (NVIDIA, AMD/Intel on Windows, Apple Silicon, or CPU),
   - writes `config.json` (settings, not committed) and runs `python broll.py doctor --ai-test`.
   The data folder defaults to `~/broll-bank`; use `--home <folder>` for a bigger drive (a niche uses 1-3 GB).
4. If `doctor` shows any FAIL, follow the fix it prints. `doctor` must say **all good** before you continue.

**Heads-up:** everything after install was tested end to end, but `install.py` itself has not yet been run on a
fresh machine (the TikTok service was first set up natively on the author's PC, not in Docker). If a step
fails: read the error, fix the cause (missing tool, Docker resources, a changed upstream setting), and re-run
`python install.py`; finished steps are skipped. The TikTok service's own docs are in
`<home>/dtk/documents/en/` (installation: 02, configuration: 03, identities: 06, API keys: 09), and its Docker
setup is `<home>/dtk/docker/compose.yml`. Keep fixes inside install.py so the next run works.

AI choice: `config.json` → `"ai": "auto"` uses `claude` if installed, else `codex`. Set `"claude"` or `"codex"`
to force one (use the one the person is logged in to).

## Everyday use

- After a restart, start Docker Desktop, then `python broll.py service start`.
- New niche: `python broll.py run <short-name> --seed "<what to look for>"`
  - e.g. `python broll.py run knee-pain --seed "knee pain" --tags kneepain,arthritis`
  - defaults: 2 search rounds, up to 300 videos found, 40 downloaded (the AI ranks found videos by caption and
    downloads the likeliest real footage first), 30 clips cleaned. Raise with
    `--rounds`, `--max-videos`, `--max-downloads`, `--max-clean`. Running `run` again on the same niche continues
    where it stopped (more rounds, more downloads) and never repeats work.
  - expect roughly: search 2-5 min, downloads ~3 s each, tagging ~15 s per video, caption removal 2-5 min per
    clip. Run long jobs in the background and check `python broll.py status <niche>`.
- Show results: open `<home>/niches/<niche>/index.html` in their browser (print the path / open it).
- More caption removal: in the gallery they tick "remove text" on clips and press **copy clean command**; they
  paste the command to you; run it (`python broll.py clean <niche> --ids ...`), then `python broll.py gallery <niche>`.
- Clips for an editor: `python broll.py export <niche> <folder> [--moment pain_struggle] [--no-text] [--no-brand]`
  copies clips into one folder per moment with readable names.
- Find clips across all niches: `python broll.py find "woman jar kitchen"`.
- Search terms: `python broll.py terms <niche>` shows what the AI approved/blocked and why. The person can
  overrule: `python broll.py set <niche> "#hashtag" accept|block` or a phrase.
- Delete a niche and its files: `python broll.py forget <niche>`.

## Rules

- **Never remove brand logos or product names from footage.** Only captions/on-screen text are removed; brands
  are tagged so the person can filter them out.
- Caption removal only uses the `sttn-det`, `sttn-auto` and `lama` methods. Never `propainter` (its model weights
  are licensed non-commercial; install.py doesn't even download them).
- Keep the search polite: the tool already pauses between TikTok calls; don't run several niches in parallel.
- Footage belongs to the people who posted it. The person decides how they use it.

## When something fails

- `doctor` first; it names the broken part and the fix.
- "TikTok service reachable: FAIL": Docker Desktop not running, or the service is stopped →
  start Docker Desktop, `python broll.py service start`, wait a minute.
- Keyword searches return few posts: a logged-out TikTok session only gets page 1 of keyword results; hashtag
  searches go deeper and do most of the work. That's expected.
- `UPSTREAM_CHANGED` errors on a page: TikTok changed something; the tool skips that page. If every search
  fails, the pinned TikTok service version may need updating (`DTK_TAG` in install.py), tell the person.
- Tagging failed for a video: `python broll.py tag <niche> --retry-failed`.
- Caption removal very slow: normal on CPU-only machines (no NVIDIA/AMD/Apple GPU). Clean fewer clips
  (`--max-clean 5`) or only the picked ones.
- Logs: `<home>/data/logs/<niche>.log`. The database is `<home>/data/bank.db` (SQLite).
