#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -c /dev/uinput ]]; then
    echo "Loading uinput for this session..."
    if command -v pkexec >/dev/null 2>&1; then
        pkexec /usr/bin/modprobe uinput
    else
        echo "Run this first: sudo modprobe uinput" >&2
        exit 1
    fi
fi

exec /usr/bin/python "$APP_DIR/main.py" "$@"
