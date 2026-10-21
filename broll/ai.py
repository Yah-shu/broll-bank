"""Ask the AI through the user's own subscription CLI: Claude Code (`claude -p`) or Codex (`codex exec`).
No API keys: whichever CLI is installed and logged in does the work."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

from . import config


def backend():
    want = (config.AI or "auto").lower()
    have = {name: shutil.which(name) for name in ("claude", "codex")}
    if want in have:
        if not have[want]:
            raise SystemExit(f"AI is set to '{want}' but the `{want}` command was not found on PATH.")
        return want, have[want]
    for name in ("claude", "codex"):
        if have[name]:
            return name, have[name]
    raise SystemExit("Neither `claude` (Claude Code) nor `codex` (Codex CLI) was found. Install and log in to one.")


def ask(prompt, images=(), timeout=900):
    """Send a prompt (plus optional image files) and return the reply text."""
    name, exe = backend()
    images = [str(pathlib.Path(i).resolve()) for i in images]
    if name == "claude":
        if images:
            prompt += "\n\nImages (open each with the Read tool, in this order):\n" + "\n".join(
                f"{n}. {p}" for n, p in enumerate(images, 1))
        args = [exe, "-p", "--allowedTools", "Read"]
        for d in sorted({str(pathlib.Path(p).parent) for p in images}):
            args += ["--add-dir", d]
        args += ["--model", config.AI_MODEL or "sonnet"]
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        p = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
        if p.returncode != 0 and not p.stdout.strip():
            raise RuntimeError(f"claude failed ({p.returncode}): {p.stderr[-400:]}")
        return p.stdout
    # codex
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "reply.txt"
        args = [exe, "exec", "--skip-git-repo-check", "-o", str(out)]
        if config.AI_MODEL:
            args += ["-m", config.AI_MODEL]
        for i in images:
            args += ["-i", i]
        if images:
            prompt += "\n\nThe attached images are in this order: " + ", ".join(
                f"{n}. {pathlib.Path(p).name}" for n, p in enumerate(images, 1))
        args.append("-")  # read the prompt from stdin
        p = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=td)
        if out.exists() and out.read_text(encoding="utf-8").strip():
            return out.read_text(encoding="utf-8")
        raise RuntimeError(f"codex failed ({p.returncode}): {(p.stderr or p.stdout)[-400:]}")


def ask_json(prompt, images=(), tries=2):
    """ask() and parse the first JSON object or array in the reply."""
    last = None
    dec = json.JSONDecoder()
    for _ in range(tries):
        text = ask(prompt, images)
        for m in re.finditer(r"[\[{]", text):  # the first place a complete JSON value starts; text around it is ignored
            try:
                return dec.raw_decode(text, m.start())[0]
            except ValueError:
                continue
        last = f"no JSON in reply: {text[:300]}"
    raise RuntimeError(last)
