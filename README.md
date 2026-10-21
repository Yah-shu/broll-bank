# B-roll bank

Type a niche, get a folder of real B-roll clips for ads, pulled from TikTok:

- **Searches TikTok** for the niche and keeps widening the search with the hashtags and phrases those videos use,
  without drifting off-topic.
- **Remembers everything**: no video is downloaded twice, reposts are caught.
- **Cuts every video into shots** and has AI tag each one: pain/struggle, daily life, demo/exercise, product,
  result/relief, plus who, where, text on screen, brand visible, and whether it fits the niche.
- **Removes burned-in captions** from the clips you pick, with an AI check that the result looks clean.
- **Gallery page** to browse, filter, search and export clips.

The AI part runs on your own Claude Code or Codex subscription. No API keys.

## Setup

Easiest: open this repo in **Codex** or **Claude Code** and say *"install this and get me B-roll for &lt;niche&gt;"*.
The agent follows [AGENTS.md](AGENTS.md).

By hand: install Git, ffmpeg, Python 3.12 and Docker Desktop, start Docker, then

```
python install.py
python broll.py run knee-pain --seed "knee pain"
```

and open `~/broll-bank/niches/knee-pain/index.html`.

Works on Windows and macOS. Caption removal uses an NVIDIA, AMD/Intel (Windows) or Apple Silicon GPU when there
is one, otherwise the CPU (slow).

## Credits

- [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) (Apache-2.0) does the
  TikTok search and downloads.
- [video-subtitle-remover](https://github.com/YaoFANGUK/video-subtitle-remover) (Apache-2.0) removes captions.

The footage belongs to the people who posted it; how you use it is up to you.
