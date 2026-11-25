"""Usage-limit awareness: the status-line file, holding launches, parking and resuming runs that hit the limit."""
import json
import queue
import subprocess
import sys
import time
from pathlib import Path

from limit_launcher import __main__ as cli
from limit_launcher import launcher as L
from limit_launcher import limits as lm
from test_launcher import FAKE, run_until_exit

HERE = Path(__file__).parent
STATUSLINE = HERE.parent / "limit_launcher" / "statusline.py"
REAL_STOP = HERE / "fixtures" / "limit_stop.stream.jsonl"   # lines from a real headless stop, 2026-11-05


def write_limits(path: Path, five=(10.0, 0.0), seven=(5.0, 0.0)):
    path.write_text(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": five[0], "resets_at": five[1]},
        "seven_day": {"used_percentage": seven[0], "resets_at": seven[1]}}}), encoding="utf-8")


def test_real_headless_stop_is_detected():
    lines = [json.loads(x) for x in REAL_STOP.read_text(encoding="utf-8").splitlines()]
    assert [lm.limit_stop(ev) for ev in lines] == [1793908800.0, 1793908800.0, 0.0]
    assert lm.limit_stop({"type": "result", "subtype": "success", "is_error": False}) is None
    assert lm.limit_stop({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}) is None


def test_hold_until_uses_threshold_and_ignores_reset_windows(tmp_path):
    now = 1_000_000.0
    f = tmp_path / "rate_limits.json"
    lim = lm.Limits(f, threshold=90, clock=lambda: now)
    assert lim.hold_until() is None                               # no file yet
    write_limits(f, five=(89.9, now + 600), seven=(50, now + 9000))
    assert lim.hold_until() is None
    write_limits(f, five=(90, now + 600), seven=(95, now + 9000))
    assert lim.hold_until() == now + 9000                         # both over: wait for the later reset
    write_limits(f, five=(100, now - 1), seven=(5, now + 9000))   # the 5-hour window already reset
    assert lim.hold_until() is None
    f.write_text("not json", encoding="utf-8")
    assert lim.hold_until() is None


def test_run_events_feed_the_limits(tmp_path):
    now = 1793900000.0
    lim = lm.Limits(tmp_path / "none.json", clock=lambda: now)
    lim.observe(json.loads(REAL_STOP.read_text(encoding="utf-8").splitlines()[0]))
    assert lim.windows()["seven_day"][0] == 13.0
    assert lim.hold_until() == 1793908800.0


def test_statusline_saves_rate_limits_and_prints_a_line(tmp_path):
    payload = {"model": {"display_name": "Opus 5.5"}, "context_window": {"used_percentage": 34.2},
               "rate_limits": {"five_hour": {"used_percentage": 42.0, "resets_at": 1793908800},
                               "seven_day": {"used_percentage": 13.0, "resets_at": 1794470400}}}
    env = {"LIMIT_LAUNCHER_DATA": str(tmp_path), "SYSTEMROOT": "C:\\Windows", "SYSTEMDRIVE": "C:"}
    out = subprocess.run([sys.executable, "-I", "-S", str(STATUSLINE)], input=json.dumps(payload).encode(),
                         capture_output=True, env=env, timeout=20)
    assert out.returncode == 0
    line = out.stdout.decode()
    assert line.startswith("Opus 5.5 | ctx 34% | 5h 42% until ") and line.rstrip().endswith("| 7d 13%")
    saved = json.loads((tmp_path / "rate_limits.json").read_text(encoding="utf-8"))
    assert saved["rate_limits"]["five_hour"] == {"used_percentage": 42.0, "resets_at": 1793908800}
    # No rate_limits (before the first response, or an API-key login): the file is left alone.
    subprocess.run([sys.executable, "-I", "-S", str(STATUSLINE)], input=b'{"model": {}}', env=env, timeout=20)
    assert json.loads((tmp_path / "rate_limits.json").read_text(encoding="utf-8")) == saved


def test_to_finish_add_and_done(tmp_path):
    f = tmp_path / "TO_FINISH.md"
    f.write_text("# To finish\n\n- [ ] something else\n", encoding="utf-8")
    run = {"run_id": "run-1", "agent": "coder", "session_id": "sid-1", "description": "Build it"}
    lm.to_finish_add(run, time.time() + 60, f)
    lm.to_finish_add({**run, "run_id": "run-2", "session_id": "sid-2"}, time.time() + 60, f)
    text = f.read_text(encoding="utf-8")
    assert lm.HEADING in text and "launched run run-1 (coder) | session sid-1 | paused; the launcher resumes it at" in text
    lm.to_finish_done("sid-1", f)
    text = f.read_text(encoding="utf-8")
    assert "sid-1" not in text and "sid-2" in text and "- [ ] something else" in text
    lm.to_finish_add(run, time.time(), None)      # the log turned off: nothing to do
    lm.to_finish_done("sid-2", None)


def test_launch_is_held_near_the_limit_then_starts_after_reset(tmp_path):
    now = [1_000_000.0]
    f = tmp_path / "rate_limits.json"
    write_limits(f, five=(93, now[0] + 300))
    events = queue.Queue()
    lim = lm.Limits(f, clock=lambda: now[0])
    lau = L.Launcher(lambda *a: events.put(a), claude=FAKE, cwd=tmp_path, log_dir=tmp_path / "runs", grace_s=2,
                     limits=lim, clock=lambda: now[0])
    run_id, sid = lau.start("writer", "Draft a note", 0.5, description="Draft")
    kind, rid, w = events.get(timeout=2)
    assert (kind, rid, w["until"], w["resume"]) == ("held", run_id, now[0] + 300, False)
    assert run_id not in lau.runs and lau.running() == 0
    assert json.loads((tmp_path / "runs" / "waiting.json").read_text(encoding="utf-8"))[run_id]["prompt"] == "Draft a note"
    lau.tick()
    assert events.empty()                                          # not before the reset
    # A new launcher (a restart) still knows about it.
    assert run_id in L.Launcher(lambda *a: None, claude=FAKE, log_dir=tmp_path / "runs").waiting
    now[0] += 301
    lau.tick()
    got = run_until_exit(lau, events, run_id)
    assert got[0][0] == "released" and ("exit", 0) in got
    assert lau.waiting == {}


def test_stop_drops_a_held_run(tmp_path):
    f = tmp_path / "rate_limits.json"
    write_limits(f, five=(99, time.time() + 300))
    events = queue.Queue()
    lau = L.Launcher(lambda *a: events.put(a), claude=FAKE, cwd=tmp_path, log_dir=tmp_path / "runs",
                     limits=lm.Limits(f))
    run_id, _ = lau.start("writer", "x", 1)
    assert lau.stop(run_id) is True
    assert [k for k, rid, _ in list(events.queue) if rid == run_id] == ["held", "exit"]
    assert lau.waiting == {}


def test_cli_parks_a_limit_stop_logs_it_and_resumes_the_session(tmp_path, monkeypatch):
    """End to end with the fake: the run hits the limit, is parked and logged, then resumes with --resume in the
    same session, and its to-do line goes once it is done."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    monkeypatch.setenv("FAKE_CLAUDE_RESETS_AT", str(int(time.time()) + 2))
    log = tmp_path / "launches.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    todo = tmp_path / "TO_FINISH.md"
    lines, seen_todo = [], []

    def out(s):
        lines.append(s)
        if s.startswith("Paused until"):
            seen_todo.append(todo.read_text(encoding="utf-8") if todo.exists() else "")

    code = cli.run("writer", "Draft the note", 1.0, cwd=tmp_path, claude=FAKE, log_dir=tmp_path / "runs",
                   limits=lm.Limits(tmp_path / "rate_limits.json"), todo=todo, tick_s=0.2, out=out)
    assert code == 0, lines
    launches = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    session = lines[0].split("session ")[1]
    assert [x["resume"] for x in launches] == [None, session]
    assert float(launches[1]["budget"]) == 1.0              # $1 minus the $0.004 spent, to the cent
    assert seen_todo and f"| session {session} |" in seen_todo[0]
    assert session not in todo.read_text(encoding="utf-8")
    assert "Resuming the session." in lines and "Done: " in " ".join(lines)
    assert lines[-1] == f"Cost ${0.004 + 0.0123:.4f}."


def test_cli_rejects_a_bad_budget(tmp_path):
    lines = []
    assert cli.run("writer", "x", 0, claude=FAKE, log_dir=tmp_path, out=lines.append) == 2
    assert "budget" in lines[0]
