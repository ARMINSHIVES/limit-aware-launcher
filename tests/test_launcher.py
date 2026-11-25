"""Launching runs: finding claude, the command line, the stream reader, whole-run stop."""
import os
import queue
import sys
import time
from pathlib import Path

import pytest

from limit_launcher import launcher as L

FAKE = [sys.executable, str(Path(__file__).parent / "fake_claude.py")]


def run_until_exit(launcher, events, run_id, timeout=20, also=()):
    """Collects (kind, payload) for run_id until its "exit" and every kind in `also` have arrived.
    "exit" (reader thread, at stdout EOF) and "stopped" (stop thread, after its wait returns) come
    from different threads, so either can be first."""
    got, end = [], time.time() + timeout
    while time.time() < end:
        try:
            kind, rid, payload = events.get(timeout=0.5)
        except queue.Empty:
            continue
        if rid == run_id:
            got.append((kind, payload))
            if {"exit", *also} <= {k for k, _ in got}:
                return got
    raise AssertionError(f"run didn't exit: {got}")


@pytest.fixture
def fake(tmp_path):
    events = queue.Queue()
    lau = L.Launcher(lambda *a: events.put(a), claude=FAKE, cwd=tmp_path, log_dir=tmp_path / "runs", grace_s=2)
    return lau, events


def test_find_claude_prefers_newest_extension(tmp_path, monkeypatch):
    monkeypatch.delenv("LIMIT_LAUNCHER_CLAUDE", raising=False)
    monkeypatch.setattr(L.shutil, "which", lambda _: None)
    monkeypatch.setattr(L.Path, "home", lambda: tmp_path)
    exe = "claude.exe" if os.name == "nt" else "claude"
    for v in ("2.1.317", "2.1.318", "2.1.99"):
        d = tmp_path / ".vscode" / "extensions" / f"anthropic.claude-code-{v}-win32-x64" / "resources" / "native-binary"
        d.mkdir(parents=True)
        (d / exe).write_text("")
    assert "2.1.318" in L.find_claude()
    override = tmp_path / "my-claude.exe"
    override.write_text("")
    monkeypatch.setenv("LIMIT_LAUNCHER_CLAUDE", str(override))
    assert L.find_claude() == str(override)


def test_command_flags(monkeypatch, tmp_path):
    lau = L.Launcher(lambda *a: None, claude=["claude.exe"], log_dir=tmp_path)
    cmd = lau.command("agent-builder", 2.0, "sid-1")
    s = " ".join(cmd)
    assert cmd[:2] == ["claude.exe", "-p"]
    assert "--agent agent-builder" in s and "--output-format stream-json --verbose" in s
    assert "--max-budget-usd 2 " in s and "--session-id sid-1" in s
    assert "--permission-mode auto --permission-prompts none" in s
    assert "--disallowedTools" not in cmd and "--settings" not in cmd and "--bare" not in cmd
    monkeypatch.setattr(L, "DISALLOWED_TOOLS", "mcp__mail,mcp__drive")
    lau = L.Launcher(lambda *a: None, claude=["claude"], log_dir=tmp_path, settings=Path("hooks.json"))
    cmd = lau.command("writer", 1, "sid-2", resume=True)
    assert cmd[cmd.index("--disallowedTools") + 1] == "mcp__mail,mcp__drive"
    assert cmd[cmd.index("--settings") + 1] == "hooks.json" and cmd[cmd.index("--resume") + 1] == "sid-2"


def test_check_launch(monkeypatch):
    monkeypatch.setattr(L, "EXPENSIVE_AGENTS", {"researcher"})
    agents = {"writer", "researcher"}
    assert L.check_launch("writer", agents, 2, True, False) is None
    assert "Unknown" in L.check_launch("nobody", agents, 2, True, False)
    assert L.check_launch("writer", agents, 2, False, False) == "Not confirmed."
    assert "second confirmation" in L.check_launch("researcher", agents, 2, True, False)
    assert L.check_launch("researcher", agents, 2, True, True) is None
    for bad in (0, -1, 26, "2", True, None):
        assert "budget" in L.check_launch("writer", agents, bad, True, True)


def test_run_streams_events_and_logs_them(fake, tmp_path):
    lau, events = fake
    run_id, sid = lau.start("writer", "Draft a note", 0.5)
    got = run_until_exit(lau, events, run_id)
    evs = [p for k, p in got if k == "event"]
    assert evs[0]["subtype"] == "init" and evs[0]["session_id"] == sid      # --session-id is passed through
    assert evs[-1]["type"] == "result" and evs[-1]["result"] == "Done: Draft a note"   # the prompt went on stdin
    assert got[-1] == ("exit", 0)
    assert (tmp_path / "runs" / f"{run_id}.stream.jsonl").read_text(encoding="utf-8").count("\n") == len(evs)


def test_run_cap(fake, monkeypatch):
    lau, events = fake
    monkeypatch.setattr(L, "MAX_RUNNING", 1)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    run_id, _ = lau.start("writer", "x", 1)
    with pytest.raises(RuntimeError, match="already going"):
        lau.start("writer", "y", 1)
    lau.stop(run_id)
    run_until_exit(lau, events, run_id, also=("stopped",))


@pytest.mark.parametrize("mode,method", [("slow", "ctrl_break" if os.name == "nt" else "sigint"),
                                         ("stubborn", "terminate" if os.name == "nt" else "sigterm")])
def test_whole_run_stop(fake, monkeypatch, mode, method):
    """CTRL_BREAK ends a run that honours it; one that ignores it is ended by the tree kill."""
    if mode == "stubborn" and os.name != "nt":
        pytest.skip("SIGTERM is ignored by the stubborn fake; the POSIX path ends with SIGKILL")
    lau, events = fake
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    run_id, _ = lau.start("writer", "x", 1)
    deadline = time.time() + 15
    while time.time() < deadline:                 # wait until it is up and streaming
        kind, rid, payload = events.get(timeout=15)
        if kind == "event" and payload.get("type") == "assistant":
            break
    assert lau.stop(run_id) is True
    got = run_until_exit(lau, events, run_id, also=("stopped",))
    assert [p for k, p in got if k == "stopped"] == [method]
    assert lau.runs[run_id].proc.poll() is not None
    assert not any(k == "event" and p.get("type") == "result" for k, p in got)   # ended before finishing
    assert lau.stop(run_id) is False              # already gone
