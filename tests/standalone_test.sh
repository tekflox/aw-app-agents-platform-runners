#!/usr/bin/env bash
# Standalone test — no framework runtime required. Run this INSIDE the
# aw-workspace container to prove install_hello.sh actually installs `hello`
# and that it resolves + prints the configured greeting after.
#
# TEMPLATE: this is the pattern — install, check `which`, check output,
# re-run once to prove idempotency. See aw-app-essentials/tests/
# standalone_test.sh for a bigger example (16 CLIs, several install
# mechanisms).
#
# Usage (from inside the container, with this repo copied in):
#   bash tests/standalone_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export AW_APP_HELLO_GREETING="${AW_APP_HELLO_GREETING:-Hello}"
AW_BIN_DIR="${AW_WORKSPACE_HOME:-$HOME/.aw-workspace}/bin"

echo "== install_hello.sh (greeting=$AW_APP_HELLO_GREETING) =="
bash scripts/install_hello.sh

echo "== resolution check (bin dir: $AW_BIN_DIR) =="
export PATH="$AW_BIN_DIR:$PATH"
which hello

echo "== output =="
hello template

echo "== idempotency re-run =="
bash scripts/install_hello.sh

echo "OK: hello installed and resolves"
