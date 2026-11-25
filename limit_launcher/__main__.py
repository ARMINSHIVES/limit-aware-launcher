"""python -m limit_launcher <command>

  run AGENT [PROMPT] [--budget USD] [--cwd DIR] [--settings FILE]
        Run `claude -p --agent AGENT` headless. PROMPT defaults to stdin. Near the usage limit the launch waits for
        the reset; a run stopped by the limit is parked and resumed in the same session after the reset. Exits 0 when
        the run finishes cleanly, 1 otherwise.
  limits
        The usage windows the launcher knows about (from statusline.py's file) and whether launches are held.
"""
import argparse
import queue
import sys
import time

from . import limits as lm
from .launcher import DEFAULT_BUDGET_USD, Launcher, check_launch

MIN_BUDGET_LEFT = 0.05   # a parked run with less than this left is ended instead of resumed


def run(agent: str, prompt: str, budget: float, cwd=None, settings=None, claude=None, log_dir=None,
        limits: lm.Limits | None = None, todo=lm.TO_FINISH, tick_s: float = 5.0, out=print) -> int:
    if err := check_launch(agent, {agent}, budget, True, True):
        out(err)
        return 2
    events = queue.Queue()
    limits = limits or lm.Limits()
    lau = Launcher(lambda *a: events.put(a), claude=claude, cwd=cwd, log_dir=log_dir, settings=settings,
                   limits=limits)
    run_id, session = lau.start(agent, prompt, budget, description=prompt[:120])
    out(f"{run_id}: session {session}")
    reset, cost, base, failed = None, 0.0, 0.0, False
    try:
        while True:
            try:
                kind, _, payload = events.get(timeout=tick_s)
            except queue.Empty:
                lau.tick()
                continue
            if kind == "event":
                ev = payload
                if ev.get("type") == "rate_limit_event":
                    limits.observe(ev)
                if (r := lm.limit_stop(ev)) is not None:
                    reset = max(r, reset or 0)
                if ev.get("type") == "result":
                    if isinstance(ev.get("total_cost_usd"), (int, float)):
                        cost = base + ev["total_cost_usd"]   # a resumed process reports its own cost only
                    failed = bool(ev.get("is_error")) or ev.get("subtype") != "success"
                    if reset is None and isinstance(ev.get("result"), str):
                        out(ev["result"])
            elif kind == "held":
                why = "resumes after the reset" if payload["resume"] else "starts after the reset"
                out(f"Paused until {lm.clock_text(payload['until'])}: the usage limit is reached, so it {why}.")
            elif kind == "released":
                base = cost
                out("Resuming the session." if payload["resume"] else "Starting.")
            elif kind == "exit":
                if reset is not None and payload is not None:
                    until = reset or limits.hold_until() or time.time() + lm.UNKNOWN_RESET_S
                    left = budget - cost
                    if left < MIN_BUDGET_LEFT:
                        out("Stopped on the usage limit with its budget used up.")
                        return 1
                    lau.resume_later(run_id, until, round(left, 2), prompt[:120])
                    lm.to_finish_add({"run_id": run_id, "agent": agent, "session_id": session,
                                      "description": prompt[:120]}, until, todo)
                    reset = None
                    continue
                lm.to_finish_done(session, todo)
                out(f"Cost ${cost:.4f}.")
                return 0 if payload == 0 and not failed else 1
    except KeyboardInterrupt:
        out("Stopping the run.")
        lau.stop(run_id)
        while events.get()[0] != "exit":
            pass
        return 1


def show_limits(limits: lm.Limits | None = None, out=print):
    limits = limits or lm.Limits()
    windows = limits.windows()
    if not windows:
        out(f"No usage data yet ({limits.path}). Install statusline.py as the Claude Code status line.")
    for name, (pct, reset) in sorted(windows.items()):
        out(f"{name:10} {pct:5.1f}%  resets {lm.clock_text(reset)}")
    until = limits.hold_until()
    out(f"Launches held until {lm.clock_text(until)}." if until else "Launches go now.")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m limit_launcher", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("agent")
    r.add_argument("prompt", nargs="?")
    r.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    r.add_argument("--cwd")
    r.add_argument("--settings")
    sub.add_parser("limits")
    a = ap.parse_args(argv)
    if a.cmd == "limits":
        show_limits()
        return 0
    prompt = a.prompt if a.prompt is not None else sys.stdin.read()
    return run(a.agent, prompt, a.budget, cwd=a.cwd, settings=a.settings)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
