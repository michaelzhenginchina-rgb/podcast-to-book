#!/usr/bin/env bash
set -euo pipefail

RUNTIME="${PODCAST_TO_BOOK_RUNTIME:-$HOME/podcast-to-ebook-repo}"
PY="${PODCAST_TO_BOOK_PYTHON:-$RUNTIME/venv/bin/python}"

if [ ! -d "$RUNTIME" ]; then
  echo "runtime repo not found: $RUNTIME"
  echo "Set PODCAST_TO_BOOK_RUNTIME=/path/to/podcast-to-ebook-repo"
  exit 1
fi

cd "$RUNTIME"
echo "runtime: $RUNTIME"
echo "branch: $(git branch --show-current 2>/dev/null || true)"
echo "status:"
git status --short
echo

if [ ! -x "$PY" ]; then
  echo "runtime python not found or not executable: $PY"
  echo "Set PODCAST_TO_BOOK_PYTHON=/path/to/python"
  exit 1
fi

echo "python: $PY"
"$PY" - <<'PY'
import importlib

for name in ["openai", "dotenv", "pypdf", "ebooklib", "reportlab"]:
    try:
        importlib.import_module(name)
        print(f"{name}: ok")
    except Exception as exc:
        print(f"{name}: missing ({exc})")
PY
