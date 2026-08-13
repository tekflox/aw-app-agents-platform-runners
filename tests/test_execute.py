

# --- per-CLI flag gating (codex rejects claude's flags) ----------------------

def test_claude_gets_its_tool_and_system_prompt_flags():
    from agents_platform_runners_app.execute import CLI_SPECS
    spec = CLI_SPECS["claude"]
    assert spec["append_system_prompt_flag"] == "--append-system-prompt"
    assert spec["allowed_tools_flag"] == "--allowed-tools"


def test_codex_declares_none_of_them():
    """codex exits on an unknown flag, having published only thread.started —
    so the run lands as success with empty output and zero tokens. Every
    codex run through this Runner was silently a no-op because of it."""
    from agents_platform_runners_app.execute import CLI_SPECS
    spec = CLI_SPECS["codex"]
    assert spec["append_system_prompt_flag"] is None
    assert spec["allowed_tools_flag"] is None
    assert spec["disallowed_tools_flag"] is None


def test_isolated_cwd_hangs_off_this_clis_creds_dir():
    """In direct-home mode only the selected CLI's creds dir is mounted, so a
    working_dir under a hardcoded .claude does not exist in a codex container
    and podman refuses to start it at all ("workdir ... does not exist")."""
    from agents_platform_runners_app.execute import CLI_SPECS
    for cli, spec in CLI_SPECS.items():
        rel = f"{spec['creds_dir']}/isolated/run123"
        assert rel.startswith(spec["creds_dir"] + "/"), cli
    assert CLI_SPECS["codex"]["creds_dir"] == ".codex"


def test_only_claude_authenticates_via_an_injected_env_token():
    """The spawned image runs as uid 1000 while this workspace runs as 1001
    and a CLI login writes creds 0600 — so a CLI that must READ its creds off
    the mounted dir cannot use the live-mount path. claude is exempt only
    because its auth arrives as CLAUDE_CODE_OAUTH_TOKEN."""
    from agents_platform_runners_app.execute import CLI_SPECS
    assert CLI_SPECS["claude"]["env_token_auth"] is True
    assert CLI_SPECS["codex"]["env_token_auth"] is False


def test_codex_relocates_its_home_off_the_unwritable_container_home():
    """The container runs as the workspace uid while the image bakes
    /home/ubuntu 0750 ubuntu:ubuntu — the run user never gets write there, so
    creds must be staged somewhere else and the CLI pointed at it."""
    from agents_platform_runners_app.execute import CLI_SPECS
    assert CLI_SPECS["codex"]["home_env"] == "CODEX_HOME"


def test_codex_auth_mode_reads_chatgpt(tmp_path):
    from agents_platform_runners_app.execute import _codex_auth_mode
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    assert _codex_auth_mode(tmp_path) == "chatgpt"


def test_codex_auth_mode_is_blank_when_unreadable(tmp_path):
    """Must never raise — an unreadable auth.json just means no override is
    suppressed, not a failed run."""
    from agents_platform_runners_app.execute import _codex_auth_mode
    assert _codex_auth_mode(tmp_path) == ""
