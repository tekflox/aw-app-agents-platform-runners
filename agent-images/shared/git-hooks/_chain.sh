# Sourced by every hook script in this directory — not executable itself.
#
# chain_hook NAME "$@" runs whichever hook named NAME would have fired had
# core.hooksPath not been pointed at this read-only mounted directory: the
# repo's own .git/hooks/NAME (where a `pre-commit install`-managed hook
# lands — the §6 pre-commit standard, card 40f5bf3b-9510-811d-916c-
# dd2ad57d3ac4) and, if the repo configures one itself, a husky-style
# repo-local core.hooksPath.
#
# $GIT_DIR is read directly, never via `git rev-parse --git-path hooks` —
# that resolves relative to the CURRENT (overridden) core.hooksPath and
# would loop straight back into this very directory.
chain_hook() {
    _chain_name="$1"
    shift

    _chain_git_dir="${GIT_DIR:-}"
    if [ -z "$_chain_git_dir" ]; then
        _chain_git_dir=$(git rev-parse --git-dir 2>/dev/null) || return 0
    fi
    case "$_chain_git_dir" in
        /*) : ;;
        *) _chain_git_dir="$(pwd)/$_chain_git_dir" ;;
    esac

    # A repo-local core.hooksPath (husky etc.) is shadowed, from git's own
    # point of view, by the GIT_CONFIG_* env override that activates this
    # directory — env config outranks file config, so a plain
    # `git config --get core.hooksPath` would just hand back THIS
    # directory. Read the repo's config FILE directly instead, bypassing
    # the override, to find what it would have been without it.
    _chain_local_path=$(git config --file "$_chain_git_dir/config" --get core.hooksPath 2>/dev/null) \
        || _chain_local_path=""
    if [ -n "$_chain_local_path" ]; then
        case "$_chain_local_path" in
            /*) _chain_target="$_chain_local_path/$_chain_name" ;;
            *) _chain_target="$(dirname "$_chain_git_dir")/$_chain_local_path/$_chain_name" ;;
        esac
        # Loop guard: if the repo-local path resolves back to THIS directory
        # (e.g. someone wrote our own mount path into .git/config directly,
        # instead of only via the env override that normally activates it),
        # chaining into it would exec this very script again, forever.
        # SELF_DIR is set by the caller (each hook script) before sourcing
        # this file, and is visible here because this function runs in the
        # caller's own shell, not a subshell.
        if [ "$(dirname "$_chain_target")" != "${SELF_DIR:-}" ] && [ -x "$_chain_target" ]; then
            "$_chain_target" "$@"
            return $?
        fi
    fi

    _chain_target="$_chain_git_dir/hooks/$_chain_name"
    if [ -x "$_chain_target" ]; then
        "$_chain_target" "$@"
        return $?
    fi
    return 0
}
