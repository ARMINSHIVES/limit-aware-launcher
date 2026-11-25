import os
from pathlib import Path

# Where rate_limits.json (from statusline.py), run logs and waiting.json live. statusline.py reads the same variable.
DATA = Path(os.environ.get("LIMIT_LAUNCHER_DATA") or Path.home() / ".limit-launcher")
# A Markdown to-do file that lists runs parked on a limit; set it empty to turn the log off.
TODO = os.environ.get("LIMIT_LAUNCHER_TODO", str(DATA / "TO_FINISH.md"))
