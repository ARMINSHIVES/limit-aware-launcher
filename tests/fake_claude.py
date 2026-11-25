"""Stand-in for `claude -p ... --output-format stream-json` (tests; no tokens spent).

Reads the prompt on stdin and prints stream-json lines shaped like the real ones (recorded 2026-11).
FAKE_CLAUDE_MODE:
  normal   - finishes after FAKE_CLAUDE_SECONDS (default 0.3) with a success result
  slow     - keeps working until stopped; exits 0 on CTRL_BREAK / SIGINT, as claude -p does
  stubborn - ignores CTRL_BREAK, SIGINT and SIGTERM, so only a tree kill ends it
  limit    - hits the claude.ai usage limit, shaped like a real headless stop (fixtures/limit_stop.stream.jsonl, 2026-11-05):
             rate_limit_event "rejected", an assistant error line, a result with is_error and api_error, exit 0.
             FAKE_CLAUDE_RESETS_AT sets resetsAt (default: 60 s from now). A --resume launch runs normally.
FAKE_CLAUDE_LOG: a file that gets one line per launch (agent, budget, resume), so a test can count them.
"""
import argparse
import json
import os
import signal
import sys
import time


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def hit_limit(sid):
    reset = int(os.environ.get("FAKE_CLAUDE_RESETS_AT") or time.time() + 60)
    info = {"status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour", "overageStatus": "rejected",
            "isUsingOverage": False}
    text = "You've hit your 5-hour limit · resets 3pm (UTC)"
    emit({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {**info, "unifiedWindows": {
        "five_hour": {"utilization": 1, "resetsAt": reset, "status": "rejected"},
        "seven_day": {"utilization": 0.13, "resetsAt": reset + 86400 * 5, "status": "allowed"}}}})
    emit({"type": "assistant", "parent_tool_use_id": None, "session_id": sid, "error": "rate_limit",
          "api_error": "usage_limit_reached", "api_error_params": {"rate_limit_info": info},
          "message": {"id": "msg_limit", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0},
                      "content": [{"type": "text", "text": text}]}})
    emit({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
          "api_error": "usage_limit_reached", "terminal_reason": "api_error", "total_cost_usd": 0.004,
          "result": text, "session_id": sid})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", default="fake-session")
    ap.add_argument("--agent", default="")
    ap.add_argument("--max-budget-usd", default="1")
    ap.add_argument("--resume", default=None)
    args, _ = ap.parse_known_args()
    mode = os.environ.get("FAKE_CLAUDE_MODE", "normal")
    sigs = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    for s in sigs:
        signal.signal(s, signal.SIG_IGN if mode == "stubborn" else (lambda *_: sys.exit(0)))
    if log := os.environ.get("FAKE_CLAUDE_LOG"):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"agent": args.agent, "budget": args.max_budget_usd, "resume": args.resume}) + "\n")
    prompt = sys.stdin.read()
    sid = args.resume or args.session_id
    emit({"type": "system", "subtype": "init", "session_id": sid, "agent": args.agent, "cwd": os.getcwd()})
    if mode == "limit" and not args.resume:
        hit_limit(sid)
        return
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50}
    msg = {"id": "msg_fake1", "usage": usage, "content": [{"type": "tool_use", "id": "toolu_f1", "name": "Read"}]}
    emit({"type": "assistant", "message": msg, "parent_tool_use_id": None, "session_id": sid})
    # The same message again (one line per content block repeats the usage), then a bigger final usage.
    emit({"type": "assistant", "message": msg, "parent_tool_use_id": None, "session_id": sid})
    # A sub-agent's message: its tokens belong to the sub-agent's task, not the run.
    emit({"type": "assistant", "message": {"id": "msg_sub", "usage": usage, "content": []},
          "parent_tool_use_id": "toolu_f1", "session_id": sid})
    final = {**usage, "output_tokens": 40}
    emit({"type": "assistant", "message": {"id": "msg_fake2", "usage": final, "content": [{"type": "text", "text": "ok"}]},
          "parent_tool_use_id": None, "session_id": sid})
    end = time.time() + float(os.environ.get("FAKE_CLAUDE_SECONDS", "0.3"))
    while mode in ("slow", "stubborn") or time.time() < end:
        time.sleep(0.05)
    emit({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0123,
          "result": f"Done: {prompt[:40]}", "session_id": sid})


if __name__ == "__main__":
    main()
