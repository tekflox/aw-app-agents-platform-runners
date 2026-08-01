#!/usr/bin/env bash
# Installs a trivial `hello` command into the workspace's persistent bin dir
# (~/.aw-workspace/bin, on PATH, survives restarts). Idempotent — safe to
# re-run (on install, and on every reconcile pass after workspace
# recreation). The greeting word is configurable via AW_APP_HELLO_GREETING
# (set by template_app/plugin.py from config_schema.greeting).
#
# TEMPLATE: replace this with your app's real installer(s). Keep the shape —
# idempotent, no interactive prompts, writes only under AW_WORKSPACE_HOME —
# see aw-app-essentials/scripts/install_*.sh for real examples (apt package,
# single-binary download, corepack activation, git-clone install).
set -euo pipefail

GREETING="${AW_APP_HELLO_GREETING:-Hello}"
AW_BIN_DIR="${AW_WORKSPACE_HOME:-$HOME/.aw-workspace}/bin"
mkdir -p "$AW_BIN_DIR"

cat > "$AW_BIN_DIR/hello" <<SCRIPT
#!/usr/bin/env bash
echo "${GREETING}, \${1:-world}!"
SCRIPT
chmod +x "$AW_BIN_DIR/hello"

"$AW_BIN_DIR/hello" template
