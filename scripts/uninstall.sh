#!/usr/bin/env bash
# Reverses install_hello.sh. Called on app uninstall (journal replay per the
# ADR's Decision 7 — this script IS the revert action for the
# commands:install journal entry).
#
# TEMPLATE: reverse whatever your real install_*.sh scripts did — apt purge,
# rm the downloaded binary/symlink, rm -rf a cloned dir, etc. See
# aw-app-essentials/scripts/uninstall.sh for a real multi-CLI example.
set -euo pipefail

AW_BIN_DIR="${AW_WORKSPACE_HOME:-$HOME/.aw-workspace}/bin"
rm -f "$AW_BIN_DIR/hello"
