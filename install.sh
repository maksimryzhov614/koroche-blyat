#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PYTHON=${PYTHON:-python3}
exec "$PYTHON" -E -B "$ROOT/scripts/install.py" "$@"
