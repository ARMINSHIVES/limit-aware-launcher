"""Usage-limit awareness for launched runs: hold new launches near the claude.ai limit, park runs that hit it.

Two sources say how close the limits are:
- <data>/rate_limits.json, written by statusline.py from Claude Code's status-line JSON:
  `rate_limits.five_hour|seven_day.used_percentage` (0-100) and `.resets_at` (Unix epoch seconds), as documented
  for the status line in 2026-11.
- `rate_limit_event` lines in a run's own stream-json: `rate_limit_info.unifiedWindows.<window>.utilization` (0-1)
  and `.resetsAt` (epoch seconds).

How a headless `claude -p` reports a limit stop (a real log, 2026-11-05; tests/fixtures/limit_stop.stream.jsonl):
a rate_limit_event with status "rejected" and resetsAt; an assistant line with error "rate_limit", api_error
"usage_limit_reached" and api_error_params.rate_limit_info.resetsAt; then a result with is_error true, subtype
"success" (!), api_error "usage_limit_reached" and the text "You've hit your 5-hour limit · resets 3pm (...)".
The process then exits 0.
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import config

THRESHOLD_PCT = 90.0
UNKNOWN_RESET_S = 1800     # a limit stop without a reset time: try again this much later
FILE = config.DATA / "rate_limits.json"
TO_FINISH = Path(config.TODO) if config.TODO else None
HEADING = "## Stopped by limits (auto-logged)"


def clock_text(ts: float, now: float | None = None) -> str:
    """8:40 pm today, "Mon 8:40 pm" on another day."""
    d = datetime.fromtimestamp(ts)
    t = d.strftime("%I:%M %p").lstrip("0").lower()
    return t if d.date() == datetime.fromtimestamp(now or time.time()).date() else f"{d:%a} {t}"


def limit_stop(ev: dict) -> float | None:
    """For a stream-json line that says the run hit a usage limit: the reset time (0.0 if not given). Else None."""
    kind = ev.get("type")
    if kind == "rate_limit_event":
        info = ev.get("rate_limit_info") or {}
        return float(info.get("resetsAt") or 0) if info.get("status") == "rejected" else None
    if ev.get("api_error") == "usage_limit_reached" or (kind == "assistant" and ev.get("error") == "rate_limit"):
        info = (ev.get("api_error_params") or {}).get("rate_limit_info") or {}
        return float(info.get("resetsAt") or 0)
    return None


class Limits:
    def __init__(self, path: Path = FILE, threshold: float = THRESHOLD_PCT, clock=time.time):
        self.path, self.threshold, self.clock = path, threshold, clock
        self.seen: dict[str, tuple[float, float]] = {}   # window -> (used %, resets_at) from runs' own events

    def observe(self, ev: dict):
        """A run's rate_limit_event."""
        info = ev.get("rate_limit_info") or {}
        for name, w in (info.get("unifiedWindows") or {}).items():
            if isinstance(w, dict) and w.get("resetsAt"):
                self.seen[name] = (float(w.get("utilization") or 0) * 100, float(w["resetsAt"]))
        if info.get("status") == "rejected" and info.get("resetsAt"):
            self.seen[info.get("rateLimitType") or "five_hour"] = (100.0, float(info["resetsAt"]))

    def _from_file(self) -> dict[str, tuple[float, float]]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        for name, w in (d.get("rate_limits") or {}).items() if isinstance(d, dict) else ():
            if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)) and w.get("resets_at"):
                out[name] = (float(w["used_percentage"]), float(w["resets_at"]))
        return out

    def windows(self) -> dict[str, tuple[float, float]]:
        """Windows that haven't reset yet. Where both sources know one, the higher use wins."""
        now, out = self.clock(), {}
        for src in (self._from_file(), self.seen):
            for name, (pct, reset) in src.items():
                if reset > now and pct >= out.get(name, (-1, 0))[0]:
                    out[name] = (pct, reset)
        return out

    def hold_until(self) -> float | None:
        """When launches may go again, if any window is at or over the threshold."""
        over = [reset for pct, reset in self.windows().values() if pct >= self.threshold]
        return max(over) if over else None


def to_finish_add(run: dict, until: float, path: Path = TO_FINISH, now: float | None = None):
    """Log a paused run in the to-do file, under HEADING. A Claude Code StopFailure hook can append its own lines in
    the same shape (`| session <id> |`), and to_finish_done clears both."""
    if path is None:
        return
    now = now or time.time()
    line = (f"- [ ] {datetime.fromtimestamp(now):%Y-%m-%d %H:%M} | rate_limit | launched run {run['run_id']} "
            f"({run['agent']}) | session {run['session_id']} | paused; the launcher resumes it at {clock_text(until, now)}: "
            f"{(run.get('description') or '')[:120]}\n")
    text = path.read_text(encoding="utf-8") if path.exists() else "# To finish\n"
    if HEADING not in text:
        text = text.rstrip("\n") + f"\n\n{HEADING}\n\nResume these sessions when credits are back (`claude --resume <session>`).\n"
    # The auto-logged section is kept last in the file, so appending lands inside it.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip("\n") + "\n" + line, encoding="utf-8")


def to_finish_done(session_id: str, path: Path = TO_FINISH):
    """A resumed run finished: drop its auto-logged lines (the launcher's and a StopFailure hook's, same session)."""
    if path is None:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    if HEADING not in text:
        return
    head, tail = text.split(HEADING, 1)
    pat = re.compile(rf"^- \[ \] .*\| session {re.escape(session_id)} \|.*\n?", re.M)
    new_tail = pat.sub("", tail)
    if new_tail != tail:
        path.write_text(head + HEADING + new_tail, encoding="utf-8")
