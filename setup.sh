#!/bin/bash
# Create the Python virtualenv and install dependencies. Safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "python3 not found. Install it (xcode-select --install) and re-run." >&2
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtualenv at $VENV"
  "$PY" -m venv "$VENV"
fi

echo "Installing Python dependencies"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$HERE/runtime/requirements.txt"

if [ ! -f "$HERE/.env" ]; then
  cp "$HERE/.env.example" "$HERE/.env"
  chmod 600 "$HERE/.env"
  echo "Created .env - add your LLM_API_KEY to it."
fi

cat <<MSG

Done.
  Python: $VENV/bin/python

Next:
  1. Put your API key in $HERE/.env (any OpenAI-compatible provider)
  2. cargo tauri dev      (install the CLI first: cargo install tauri-cli)
MSG
