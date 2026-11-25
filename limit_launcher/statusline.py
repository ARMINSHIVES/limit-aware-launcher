"""Claude Code status line: prints a short line and saves `rate_limits` for the launcher (limits.py).

Install it in ~/.claude/settings.json: {"statusLine": {"type": "command", "command": "python /path/to/statusline.py"}},
with LIMIT_LAUNCHER_DATA set to the same folder the launcher uses (default: ~/.limit-launcher).

Claude Code pipes the status-line JSON on stdin. `rate_limits.five_hour|seven_day` carry `used_percentage` (0-100)
and `resets_at` (Unix epoch seconds); they are absent before the first API response and for API-key logins, and
then the saved file is left as it was. Must stay fast and silent on errors: it runs after every assistant message.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

DATA = Path(os.environ.get("LIMIT_LAUNCHER_DATA") or Path.home() / ".limit-launcher")
OUT = DATA / "rate_limits.json"


def save(limits: dict):
    keep = {k: {"used_percentage": w.get("used_percentage"), "resets_at": w.get("resets_at")}
            for k, w in limits.items() if k in ("five_hour", "seven_day") and isinstance(w, dict)}
    if not keep:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(f".{os.getpid()}.tmp")   # several sessions can write at once
    tmp.write_text(json.dumps({"rate_limits": keep, "updated_at": time.time()}), encoding="utf-8")
    os.replace(tmp, OUT)


def line(d: dict) -> str:
    parts = [(d.get("model") or {}).get("display_name") or ""]
    ctx = (d.get("context_window") or {}).get("used_percentage")
    if isinstance(ctx, (int, float)):
        parts.append(f"ctx {ctx:.0f}%")
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = (d.get("rate_limits") or {}).get(key) or {}
        if isinstance(w.get("used_percentage"), (int, float)):
            s = f"{label} {w['used_percentage']:.0f}%"
            if key == "five_hour" and w.get("resets_at"):
                s += " until " + datetime.fromtimestamp(w["resets_at"]).strftime("%I:%M %p").lstrip("0").lower()
            parts.append(s)
    return " | ".join(p for p in parts if p)


def main():
    try:
        d = json.loads(sys.stdin.buffer.read().decode("utf-8"))   # Windows stdin defaults to cp1252
    except ValueError:
        return
    if not isinstance(d, dict):
        return
    if isinstance(d.get("rate_limits"), dict):
        try:
            save(d["rate_limits"])
        except OSError:
            pass
    sys.stdout.write(line(d) + "\n")


if __name__ == "__main__":
    main()
