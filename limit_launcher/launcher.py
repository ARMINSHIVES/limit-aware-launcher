"""Start and stop headless `claude -p --agent X` runs.

Each run streams `--output-format stream-json` lines to a reader thread, which hands them to the
event loop through `post`. Stopping a run whole on Windows (measured on Windows 11, 2026-11):
- CTRL_C does nothing to claude -p, so it isn't sent.
- CTRL_BREAK, delivered by a helper attached to the run's hidden console, ends it in about 2.5 s
  and kills its running Bash commands.
- If it is still alive after the grace period, `taskkill /T /F` ends the whole process tree.
Elsewhere: SIGINT to the process group, then SIGTERM, then SIGKILL.

Usage limits (limits.py): while a claude.ai window is at or over the threshold, a new launch is held (it gets
its ids and a task, but no process) until the window resets. A run that hits the limit exits; the app then parks it
with resume_later, and tick() restarts it with `--resume <session>` after the reset. Held and parked runs are saved
to <log_dir>/waiting.json so a restart keeps them.
"""
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import config
from .limits import Limits

DEFAULT_BUDGET_USD = 2.0
MAX_BUDGET_USD = 25.0
EXPENSIVE_AGENTS = {a for a in os.environ.get("LIMIT_LAUNCHER_CONFIRM_TWICE", "").split(",") if a}
MAX_RUNNING = 3
# Nobody can answer a permission prompt in a headless run, so it runs in auto mode with prompts off.
PERMISSION_ARGS = ["--permission-mode", "auto", "--permission-prompts", "none"]
# Tools no launched run may touch, comma-separated (for example connectors to your mail or drive).
DISALLOWED_TOOLS = os.environ.get("LIMIT_LAUNCHER_DISALLOWED_TOOLS", "")
RESUME_PROMPT = ("The claude.ai usage limit stopped this run, and it has now reset. Continue the task from where you "
                 "stopped. Don't redo finished steps. End with the short report the task asked for.")

# Sends CTRL_BREAK to every process on the run's console. The helper attaches to that console, so it
# gets the event too; its handler returns TRUE so it survives to report the result.
_BREAK_HELPER = r"""
import ctypes, sys
k = ctypes.windll.kernel32
keep = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(lambda _: 1)
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    sys.exit(2)
k.SetConsoleCtrlHandler(keep, True)
sys.exit(0 if k.GenerateConsoleCtrlEvent(1, 0) else 3)
"""


def _version(path: str) -> tuple:
    m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", path)
    return tuple(int(x) for x in m.groups()) if m else (0,)


def find_claude() -> str | None:
    """The claude CLI: $LIMIT_LAUNCHER_CLAUDE, PATH, the native installer's location, else the newest
    VS Code (or Cursor) extension's bundled binary. The extension path changes with every update."""
    if (p := os.environ.get("LIMIT_LAUNCHER_CLAUDE")) and Path(p).is_file():
        return p
    if p := shutil.which("claude"):
        return p
    home = Path.home()
    exe = "claude.exe" if os.name == "nt" else "claude"
    if (home / ".local" / "bin" / exe).is_file():
        return str(home / ".local" / "bin" / exe)
    found = []
    for editor in (".vscode", ".vscode-insiders", ".cursor"):
        found += glob.glob(str(home / editor / "extensions" / "anthropic.claude-code-*" / "resources"
                               / "native-binary" / exe))
    return max(found, key=_version) if found else None


def check_launch(agent: str, agents: set[str], budget, confirmed, confirmed_twice) -> str | None:
    """Validation shared by every launch path. Returns an error message, or None."""
    if agent not in agents:
        return f"Unknown agent: {agent}."
    if not confirmed:
        return "Not confirmed."
    if agent in EXPENSIVE_AGENTS and not confirmed_twice:
        return f"{agent} runs can use many tokens and need a second confirmation."
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not 0 < budget <= MAX_BUDGET_USD:
        return f"The budget must be between $0 and ${MAX_BUDGET_USD:g}."
    return None


class Run:
    def __init__(self, run_id: str, cmd: list[str], prompt: str, post, log_dir: Path, cwd: Path):
        self.id, self.cmd, self.prompt, self.post = run_id, cmd, prompt, post
        self.log_dir, self.cwd = log_dir, cwd
        self.proc: subprocess.Popen | None = None
        self.stop_method: str | None = None

    def start(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            # A hidden console of its own, which the CTRL_BREAK helper can attach to.
            kw = {"creationflags": subprocess.CREATE_NO_WINDOW}
        else:
            kw = {"start_new_session": True}
        with open(self.log_dir / f"{self.id}.stderr.log", "wb") as err:
            self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=err, **kw)
        threading.Thread(target=self._read, daemon=True, name=f"run-{self.id}").start()

    def _read(self):
        try:
            # The prompt goes on stdin: no command-line length limit or quoting to worry about.
            self.proc.stdin.write(self.prompt.encode("utf-8"))
            self.proc.stdin.close()
        except OSError:
            pass
        with open(self.log_dir / f"{self.id}.stream.jsonl", "ab") as log:
            for raw in self.proc.stdout:
                log.write(raw)
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    self.post("event", self.id, ev)
        self.proc.stdout.close()
        self.post("exit", self.id, self.proc.wait())

    def stop(self, grace_s: float) -> str:
        """End the whole run. Returns the step that ended it."""
        p = self.proc
        if p.poll() is not None:
            return "exited"
        steps = (("ctrl_break", self._ctrl_break), ("terminate", self._kill_tree)) if os.name == "nt" else \
                (("sigint", lambda: os.killpg(p.pid, signal.SIGINT)),
                 ("sigterm", lambda: os.killpg(p.pid, signal.SIGTERM)))
        for name, send in steps:
            try:
                send()
                p.wait(grace_s)
                return name
            except (OSError, subprocess.TimeoutExpired):
                continue
        p.kill()
        return "kill"

    def _ctrl_break(self):
        subprocess.run([sys.executable, "-I", "-c", _BREAK_HELPER, str(self.proc.pid)], timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _kill_tree(self):
        subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)


class Launcher:
    """post(kind, run_id, payload) is called from worker threads: ("event", id, stream-json dict),
    ("exit", id, return code) and ("stopped", id, method)."""

    def __init__(self, post, claude: list[str] | None = None, cwd: Path | None = None, extra_args=(),
                 grace_s: float = 8.0, log_dir: Path | None = None, settings: Path | None = None,
                 limits: Limits | None = None, clock=time.time):
        self.post = post
        if claude is None:
            found = find_claude()
            claude = [found] if found else []
        self.claude = claude     # the executable, plus any leading arguments (tests use a fake script)
        self.cwd = cwd or Path.cwd()      # the project whose CLAUDE.md and .claude/agents the run should see
        self.extra_args = list(extra_args)
        self.grace_s = grace_s
        self.log_dir = log_dir or config.DATA / "runs"
        self.settings = settings
        self.limits, self.clock = limits, clock
        self.runs: dict[str, Run] = {}
        # run_id -> {agent, prompt, budget, session_id, description, resume, until}: held or parked, no process.
        self.waiting: dict[str, dict] = self._load_waiting()

    def command(self, agent: str, budget: float, session_id: str, resume: bool = False) -> list[str]:
        cmd = [*self.claude, "-p", "--agent", agent, "--output-format", "stream-json", "--verbose",
               "--max-budget-usd", f"{budget:g}", "--resume" if resume else "--session-id", session_id,
               *PERMISSION_ARGS]
        if DISALLOWED_TOOLS:
            cmd += ["--disallowedTools", DISALLOWED_TOOLS]
        if self.settings:
            # Extra settings (hooks, say) for launched runs only; a handler also in ~/.claude/settings.json runs once.
            cmd += ["--settings", str(self.settings)]
        return cmd + self.extra_args

    def running(self) -> int:
        return sum(1 for r in self.runs.values() if r.proc and r.proc.poll() is None)

    def start(self, agent: str, prompt: str, budget: float, description: str = "") -> tuple[str, str]:
        """Returns (run_id, session_id). Raises RuntimeError when it can't start. Near the usage limit the run is
        held instead: it posts ("held", run_id, entry) and tick() starts it after the reset."""
        if not self.claude:
            raise RuntimeError("The claude CLI wasn't found. Set LIMIT_LAUNCHER_CLAUDE to its path.")
        until = self.limits.hold_until() if self.limits else None
        if until is None and self.running() >= MAX_RUNNING:
            raise RuntimeError(f"{MAX_RUNNING} launched runs are already going; wait for one to finish.")
        run_id = "run-" + uuid.uuid4().hex[:10]
        session_id = str(uuid.uuid4())
        if until is not None:
            self._wait(run_id, {"agent": agent, "prompt": prompt, "budget": budget, "session_id": session_id,
                                "description": description, "resume": False, "until": until})
        else:
            self._spawn(run_id, agent, prompt, budget, session_id, resume=False)
        return run_id, session_id

    def _spawn(self, run_id, agent, prompt, budget, session_id, resume):
        run = Run(run_id, self.command(agent, budget, session_id, resume), prompt, self.post, self.log_dir, self.cwd)
        run.agent, run.budget, run.session_id = agent, budget, session_id
        run.start()
        self.runs[run_id] = run

    # ---- held and parked runs ------------------------------------------------------------------------

    def _load_waiting(self) -> dict:
        try:
            d = json.loads((self.log_dir / "waiting.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _save_waiting(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "waiting.json").write_text(json.dumps(self.waiting, indent=1), encoding="utf-8")

    def _wait(self, run_id: str, entry: dict):
        self.waiting[run_id] = entry
        self._save_waiting()
        self.post("held", run_id, entry)

    def resume_later(self, run_id: str, until: float, budget_left: float, description: str = "") -> bool:
        """Park a run that stopped on the usage limit; tick() resumes its session after `until`."""
        run = self.runs.get(run_id)
        if run is None or not getattr(run, "session_id", None):
            return False
        self._wait(run_id, {"agent": run.agent, "prompt": RESUME_PROMPT, "budget": budget_left,
                            "session_id": run.session_id, "description": description, "resume": True, "until": until})
        return True

    def tick(self):
        """Start held and parked runs whose reset time has passed, if the limits and the run cap allow."""
        now = self.clock()
        for run_id, w in list(self.waiting.items()):
            if w["until"] > now:
                continue
            until = self.limits.hold_until() if self.limits else None
            if until is not None:         # a window is still over the threshold (the 7-day one, say)
                w["until"] = until
                self._save_waiting()
                self.post("held", run_id, w)
                continue
            if self.running() >= MAX_RUNNING:
                return
            del self.waiting[run_id]
            self._save_waiting()
            try:
                self._spawn(run_id, w["agent"], w["prompt"], w["budget"], w["session_id"], w["resume"])
            except OSError as e:
                print(f"Run {run_id} couldn't start after the limit reset: {e}", flush=True)
                self.post("exit", run_id, None)
                continue
            self.post("released", run_id, w)

    def stop(self, run_id: str) -> bool:
        """Stop a run whole, in the background. False if it isn't running. A held or parked run is dropped and
        reported as exited."""
        if self.waiting.pop(run_id, None) is not None:
            self._save_waiting()
            self.post("exit", run_id, None)
            return True
        run = self.runs.get(run_id)
        if not run or not run.proc or run.proc.poll() is not None:
            return False

        def work():
            run.stop_method = run.stop(self.grace_s)
            self.post("stopped", run_id, run.stop_method)

        threading.Thread(target=work, daemon=True, name=f"stop-{run_id}").start()
        return True
