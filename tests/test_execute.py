

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
