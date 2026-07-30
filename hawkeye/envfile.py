"""Auto-load .env.local / .env at CLI startup.

Without this, FINNHUB_API_KEY and friends only exist if the shell that
launched the CLI happened to `export` them — dropping a key into
.env.local silently did nothing (see .env.local.example). Called once
from hawkeye.cli.main() before any os.environ lookups happen.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

_CANDIDATES = (".env.local", ".env")


def load_local_env(start_dir: Path | None = None) -> None:
    """Load .env.local then .env from `start_dir` (default: cwd).

    Values already present in the environment always win — python-dotenv's
    `override=False` never overwrites an existing key — so exports from CI
    or the shell still take precedence over either file. .env.local is
    loaded first, so it wins over .env for any key both files set.
    """
    base = start_dir or Path.cwd()
    for name in _CANDIDATES:
        path = base / name
        if path.is_file():
            load_dotenv(path, override=False)
