

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


# --- per-CLI flag shapes (verified against the real binaries 2026-08-13) ----

def test_copilot_declares_its_own_tool_flags():
    """GitHub Copilot CLI 1.0.79 has --allow-tool/--deny-tool. They were not
    declared at all, so gating on the spec silently dropped every tool
    restriction for copilot."""
    from agents_platform_runners_app.execute import CLI_SPECS
    spec = CLI_SPECS["copilot"]
    assert spec["allowed_tools_flag"] == "--allow-tool"
    assert spec["disallowed_tools_flag"] == "--deny-tool"
    assert spec["tools_flag_style"] == "repeat"


def test_claude_joins_tools_but_copilot_repeats_the_flag():
    """Handing claude's comma-joined shape to copilot would give it ONE tool
    literally named "a,b" — an allow-list matching nothing."""
    from agents_platform_runners_app.execute import CLI_SPECS
    assert CLI_SPECS["claude"]["tools_flag_style"] == "csv"
    assert CLI_SPECS["copilot"]["tools_flag_style"] == "repeat"


def test_cursor_agent_has_a_skip_permissions_flag_and_add_dir():
    """--force ("Run Everything", alias --yolo) is cursor-agent's
    skip-permissions equivalent. Both this and --add-dir were None, so a
    dangerous_skip_permissions agent still waited on prompts no headless run
    can answer."""
    from agents_platform_runners_app.execute import CLI_SPECS
    spec = CLI_SPECS["cursor-agent"]
    assert spec["skip_perms_flag"] == "--force"
    assert spec["add_dir_flag"] == "--add-dir"


def test_every_cli_declares_each_gated_flag_explicitly():
    """A MISSING key and a None key both read as falsy at the call site, so an
    undeclared flag is indistinguishable from "this CLI has none" — which is
    how copilot lost its tool flags. Require the key to be present."""
    from agents_platform_runners_app.execute import CLI_SPECS
    for cli, spec in CLI_SPECS.items():
        for key in ("allowed_tools_flag", "disallowed_tools_flag",
                    "tools_flag_style", "append_system_prompt_flag"):
            assert key in spec, f"{cli} does not declare {key}"


def test_tmp_access_mount_source_lives_where_the_workspace_can_create_it(tmp_path, monkeypatch):
    """podman creates a missing bind source as root:root 0755, and it made the
    old parent (a bare data/) root-owned too — so the workspace could not
    mkdir, chmod OR rmdir it from its own uid. The source must therefore sit
    under AW_WORKSPACE_HOME, which the workspace owns and creates itself."""
    import os
    from agents_platform_runners_app import execute

    monkeypatch.setattr(execute, "WORKSPACE_CONTAINER_DIR", str(tmp_path))
    rel = execute._prepare_tmp_mount_source()

    assert rel.startswith(".aw-workspace/"), rel
    made = tmp_path / rel
    assert made.is_dir()
    assert oct(os.stat(made).st_mode & 0o777) == "0o777"


def test_tmp_access_mount_source_widens_an_existing_narrow_dir(tmp_path, monkeypatch):
    """The dir seen in the wild was one podman had ALREADY created 0755, so
    exist_ok alone would have left it unusable."""
    import os
    from agents_platform_runners_app import execute

    monkeypatch.setattr(execute, "WORKSPACE_CONTAINER_DIR", str(tmp_path))
    stale = tmp_path / ".aw-workspace" / "data" / "agents-platform-runners" / "sandbox-tmp"
    stale.mkdir(parents=True)
    stale.chmod(0o755)

    execute._prepare_tmp_mount_source()
    assert oct(os.stat(stale).st_mode & 0o777) == "0o777"
