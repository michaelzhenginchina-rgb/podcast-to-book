"""Locate the Python runtime that does the actual conversion.

The runtime ships inside this repository under ``runtime/``, so the common case
needs no configuration at all. ``PODCAST_TO_BOOK_RUNTIME`` exists only for the
uncommon case of pointing the app at a checkout somewhere else.

`src-tauri/src/main.rs` implements the same order, so the app and a directly
invoked script always agree on which runtime they are using.
"""

import os
import sys
from pathlib import Path

ENV_VAR = "PODCAST_TO_BOOK_RUNTIME"
REPO = Path(__file__).resolve().parent.parent


def _candidates():
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        yield Path(override).expanduser()
    yield REPO / "runtime"


def runtime_root() -> Path:
    """Directory containing the runtime's main.py, or raise with instructions."""
    for candidate in _candidates():
        if (candidate / "main.py").is_file():
            return candidate
    raise RuntimeError(
        f"Cannot find the Python runtime. Expected it at {REPO / 'runtime'}.\n"
        "If your checkout is incomplete, re-clone the repository; if the runtime\n"
        f"lives elsewhere, set {ENV_VAR}=/path/to/runtime."
    )


def ensure_importable() -> Path:
    """Put the runtime on sys.path so `from main import ...` works."""
    root = runtime_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def env_file() -> Path | None:
    """The .env holding OPENAI_API_KEY, or None if it has not been created."""
    candidate = REPO / ".env"
    return candidate if candidate.is_file() else None
