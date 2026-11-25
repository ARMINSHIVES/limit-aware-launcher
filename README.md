# limit-aware-launcher

Runs headless Claude Code jobs that wait out usage limits and resume their own session after the reset.

Part of [Manez](https://github.com/ARMINSHIVES), a set of tools for running AI agents under rules.

## What and why

A headless `claude -p` run that hits the claude.ai usage limit simply fails, and an overnight batch dies at the first reset. This launcher holds a new run when a usage window is at 90%, and parks a run that the limit stops mid-task. It resumes the parked run in the same session (`--resume <session>`) after the reset, with whatever budget it has left. Parked runs survive a restart. The launcher uses only the Python standard library.

## Headline results

- 16 tests pass in about 13 seconds (Windows 11, Python 3.11.9). They use a fake `claude` process, so they spend no tokens.
- The limit-stop fixture is three lines from a real headless limit stop on 2026-11-05. I changed only its timezone text, to UTC.
- On Windows, `CTRL_C` does nothing to `claude -p`. A `CTRL_BREAK` sent through a helper attached to the run's console ends it in about 2.5 seconds and kills its running Bash commands. After a grace period the launcher falls back to `taskkill /T /F`. I measured this by hand on one machine, not in the test suite.

## Features

- Holds new launches while the 5-hour or 7-day window is at 90% or more, then starts them after the reset.
- Recognises a headless limit stop and parks the run with its session id, the reset time and the remaining budget in `waiting.json`.
- Resumes parked runs after the reset, in the same session.
- Caps concurrent runs at 3.
- Stops a whole run, sub-processes included, on Windows and POSIX. The POSIX path is untested.
- Optionally logs parked runs to a Markdown to-do file.
- A status-line command for Claude Code that records the usage windows for the launcher.

## Quick start

Python 3.11 or newer and a working Claude Code install. Only Windows was tested.

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
```

Run an agent headless. `AGENT` must be an agent Claude Code already knows (`claude -p --agent AGENT`):

```
python -m limit_launcher run writer "Draft release notes for v1.2" --budget 1.5
python -m limit_launcher limits
```

`--budget` is in dollars (default 2.0, at most 25). `run` takes the prompt from stdin if you leave it out, and also accepts `--cwd DIR` and `--settings FILE`. It exits 0 when the run finishes cleanly and 1 otherwise. `limits` prints the usage windows the launcher knows and whether launches are held.

To feed the launcher the usage windows, use the status-line script in `~/.claude/settings.json`:

```
{"statusLine": {"type": "command", "command": "python /path/to/limit_launcher/statusline.py"}}
```

It prints `model | ctx | 5h | 7d` and saves the `rate_limits` data. Without it, the launcher still learns the windows from each run's own `rate_limit_event` lines, only later.

## How it works

A headless limit stop has a recognisable shape: a `rate_limit_event` with status `rejected`, an assistant line carrying `usage_limit_reached`, then a result with `is_error: true`, subtype `success` and exit code 0. `limits.py` matches this shape, takes the reset time from it, and the launcher parks the run until then.

| Path | What it is |
|---|---|
| `limit_launcher/limits.py` | Reads the usage windows, `hold_until()` at 90%, `limit_stop()` for the stop shape, the to-do log |
| `limit_launcher/launcher.py` | `Launcher`: builds the command line, streams stream-json to a callback, holds, parks and resumes runs, stops a run whole |
| `limit_launcher/statusline.py` | The Claude Code status-line command |
| `limit_launcher/__main__.py` | The `run` and `limits` commands |
| `limit_launcher/config.py` | Data folder and to-do file settings |
| `tests/fake_claude.py` | A stand-in for `claude -p` with normal, slow, stubborn and limit modes |

A parked run with less than $0.05 of budget left is ended instead of resumed.

## Configuration

| Variable | Meaning |
|---|---|
| `LIMIT_LAUNCHER_DATA` | Where `rate_limits.json`, run logs and `waiting.json` live (default `~/.limit-launcher`). `statusline.py` reads the same variable |
| `LIMIT_LAUNCHER_TODO` | Markdown file listing parked runs (default `TO_FINISH.md` in the data folder). Set it empty to turn the log off |
| `LIMIT_LAUNCHER_CLAUDE` | Path to the `claude` executable |
| `LIMIT_LAUNCHER_DISALLOWED_TOOLS` | Comma-separated list passed as `--disallowedTools` |
| `LIMIT_LAUNCHER_CONFIRM_TWICE` | Agents that need a second confirmation through `check_launch` |

## Limits and known gaps

- It follows a stop pattern that Claude Code produces today (recorded 2026-11-05). If Claude Code changes its limit messages, `limit_stop()` will need an update.
- A limit stop without a reset time is retried after 30 minutes.
- Only Windows was tested. The POSIX stop path has no test.
- This is a command-line launcher. The task view and agent-creation form it came from are not included.
- It does not bypass or raise any limit. It waits.

## Licence

MIT, Armaan Francisco Shivpuri. No runtime dependencies. pytest (MIT) is needed for the tests. Claude Code is not bundled.

## Part of Manez

This is the run scheduler of [Manez](https://github.com/ARMINSHIVES). The other parts are [mcp-hub](https://github.com/ARMINSHIVES/mcp-hub), [grounded-kb](https://github.com/ARMINSHIVES/grounded-kb) and [social-publisher](https://github.com/ARMINSHIVES/social-publisher).
