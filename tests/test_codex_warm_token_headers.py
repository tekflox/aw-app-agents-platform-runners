"""Regression tests for card 3d25bf3b-9510-8143-ae96-f896ce6aef08
("Codex: schedule_wakeup 400 'could not identify this run'"): codex was
silently excluded from every per-run MCP caller-identity header because
`_build_container_kwargs` only ever wrote the MCP config payload (headers
included) when `spec["mcp_config_flag"]` was set — `None` for codex, which
has no `--mcp-config` flag at all. Its connection to aw-gateway came from a
static `config.toml` with no caller-identity header whatsoever, so
`_caller_run_id()` (mcp_server.py) had nothing to resolve and every codex
tool call that needs caller identity (schedule_wakeup chief among them)
400'd with "Could not identify this run".

The fix uses codex's own `env_http_headers` (config_types.rs
StreamableHttp transport — a header NAME mapped to an env-var NAME, not a
value) so `X-Aw-Warm-Token` — stable per (agent,session), safe to bake
once, unlike the per-turn `X-Aw-Caller-Run-Id` — reaches codex without ever
writing a session's actual token into the ONE $CODEX_HOME shared across
every codex run workspace-wide.

A first version of this fix keyed the patch on `job["mcp_servers"]`'s own
key names (e.g. "crispal", from that agent's agent-config) and shipped
green — every unit test here passed — but a LIVE dispatch against
crispal-codex still 400'd identically. Root cause: codex's
`mcp_config_flag: None` means it never receives a job's per-agent-config
server payload at all (unlike claude's regenerated `--mcp-config`), so
every codex agent shares the ONE static entry `aw-workspace-cli agent
sync` wrote (named "aw-gateway" here) regardless of which agent-config it
runs under — a name that has nothing to do with the job's own server
names. `test_patches_aw_gateway_even_when_job_uses_a_different_server_name`
below pins exactly this gap.

Run: python3 -m pytest tests/test_codex_warm_token_headers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

CONFIG_TOML_BASE = (
    '[projects."/opt/aw-workspace"]\n'
    'trust_level = "trusted"\n'
    "\n"
    "[mcp_servers.aw-gateway]\n"
    'url = "http://aw-app-mcp-gateway:9200/mcp"\n'
    "\n"
    "[mcp_servers.aw-gateway.http_headers]\n"
    'Authorization = "Bearer some-static-gateway-token"\n'
)


# ---------------------------------------------------------------------------
# _patch_codex_warm_token_headers
# ---------------------------------------------------------------------------

def test_adds_env_http_headers_for_a_configured_server(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TOML_BASE)

    changed = execute_mod._patch_codex_warm_token_headers(config)

    assert changed is True
    text = config.read_text()
    assert "[mcp_servers.aw-gateway.env_http_headers]" in text
    assert 'X-Aw-Warm-Token = "AW_MCP_WARM_TOKEN"' in text
    # The pre-existing static header must survive untouched.
    assert 'Authorization = "Bearer some-static-gateway-token"' in text


def test_is_idempotent(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TOML_BASE)

    execute_mod._patch_codex_warm_token_headers(config)
    once = config.read_text()
    changed_again = execute_mod._patch_codex_warm_token_headers(config)

    assert changed_again is False
    assert config.read_text() == once
    assert once.count("[mcp_servers.aw-gateway.env_http_headers]") == 1


def test_never_invents_a_server_this_config_toml_never_defined(tmp_path):
    """A config.toml with no real [mcp_servers.<name>] table at all (only
    unrelated sections) must come back unchanged — codex's own
    RawMcpServerConfig rejects a table with neither `command` nor `url` as
    "invalid transport", which would break EVERY codex run reading this
    file."""
    config = tmp_path / "config.toml"
    config.write_text('[projects."/opt/aw-workspace"]\ntrust_level = "trusted"\n')

    changed = execute_mod._patch_codex_warm_token_headers(config)

    assert changed is False
    assert "env_http_headers" not in config.read_text()


def test_inserts_under_an_existing_manual_env_http_headers_table(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        CONFIG_TOML_BASE
        + "\n[mcp_servers.aw-gateway.env_http_headers]\n"
        + 'X-Some-Other-Header = "SOME_OTHER_ENV_VAR"\n'
    )

    changed = execute_mod._patch_codex_warm_token_headers(config)

    assert changed is True
    text = config.read_text()
    assert text.count("[mcp_servers.aw-gateway.env_http_headers]") == 1
    assert 'X-Some-Other-Header = "SOME_OTHER_ENV_VAR"' in text
    assert 'X-Aw-Warm-Token = "AW_MCP_WARM_TOKEN"' in text


def test_patches_every_real_server_table_regardless_of_name(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        CONFIG_TOML_BASE
        + '\n[mcp_servers.a-second-server]\n'
        + 'url = "http://somewhere-else:9999/mcp"\n'
    )

    changed = execute_mod._patch_codex_warm_token_headers(config)

    assert changed is True
    text = config.read_text()
    assert "[mcp_servers.aw-gateway.env_http_headers]" in text
    assert "[mcp_servers.a-second-server.env_http_headers]" in text


def test_missing_config_toml_is_a_noop_not_an_error(tmp_path):
    config = tmp_path / "does-not-exist.toml"

    changed = execute_mod._patch_codex_warm_token_headers(config)

    assert changed is False
    assert not config.exists()


def test_unparseable_toml_is_a_noop_not_an_error(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("this is not [ valid toml")

    changed = execute_mod._patch_codex_warm_token_headers(config)

    assert changed is False


# ---------------------------------------------------------------------------
# End to end: a real codex spawn gets the env var AND the patched config.toml
# ---------------------------------------------------------------------------

def _mcp_servers_payload(warm_token: str = "warm-token-abc123", server_name: str = "aw-gateway") -> dict:
    return {
        server_name: {
            "type": "streamable-http",
            "url": "http://aw-app-mcp-gateway:9200/mcp/some-profile",
            "headers": {
                "Authorization": "Bearer some-static-gateway-token",
                "X-Aw-Caller-Run-Id": "run-xyz",
                "X-Aw-Warm-Token": warm_token,
            },
        }
    }


def test_build_container_kwargs_sets_the_warm_token_env_var(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text('{"last_refresh": "2026-09-05T00:00:00Z"}')
    (codex_dir / "config.toml").write_text(CONFIG_TOML_BASE)

    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))

    _, _, kwargs, _ = execute_mod._build_container_kwargs({
        "run_id": "r-warm-token", "cli": "codex", "prompt": "hi",
        "mcp_servers": _mcp_servers_payload("warm-token-abc123"),
    })

    assert kwargs["environment"][execute_mod.CODEX_WARM_TOKEN_ENV_VAR] == "warm-token-abc123"

    # The per-run staged copy (mounted read-only at /aw-creds) must carry the
    # header patch so a brand-new shared home inherits it on first copy.
    # direct_home_mount is true here, so the isolated scratch dir hangs off
    # REAL_HOME (see _build_container_kwargs's own docstring), not the
    # workspace tree.
    staged_config = home / ".codex" / "isolated" / "r-warm-token" / "creds" / "config.toml"
    assert 'X-Aw-Warm-Token = "AW_MCP_WARM_TOKEN"' in staged_config.read_text()


def test_patches_aw_gateway_even_when_job_uses_a_different_server_name(tmp_path, monkeypatch):
    """THE live-reproduced bug: crispal-codex's agent-config names its MCP
    server "crispal" (a scoped gateway profile URL), which never appears
    anywhere in codex's own static config.toml (only "aw-gateway" does,
    from `aw-workspace-cli agent sync`). The header patch must land on
    "aw-gateway" — the table codex ACTUALLY reads — not on "crispal", which
    would silently no-op exactly like the first, live-broken version of
    this fix did."""
    ws = tmp_path / "ws"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text('{"last_refresh": "2026-09-05T00:00:00Z"}')
    (codex_dir / "config.toml").write_text(CONFIG_TOML_BASE)

    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))

    _, _, kwargs, _ = execute_mod._build_container_kwargs({
        "run_id": "r-crispal-name-mismatch", "cli": "codex", "prompt": "hi",
        "mcp_servers": _mcp_servers_payload("warm-token-xyz", server_name="crispal"),
    })

    assert kwargs["environment"][execute_mod.CODEX_WARM_TOKEN_ENV_VAR] == "warm-token-xyz"
    staged_config = home / ".codex" / "isolated" / "r-crispal-name-mismatch" / "creds" / "config.toml"
    text = staged_config.read_text()
    assert "[mcp_servers.aw-gateway.env_http_headers]" in text
    assert "crispal" not in text  # never invented — see the module docstring


def test_build_container_kwargs_patches_an_already_populated_shared_home(tmp_path, monkeypatch):
    """The shared $CODEX_HOME is only ever populated from the staged copy
    ONCE (`[ -f config.toml ] || cp -a ...`) — an install that already ran
    codex before this fix shipped must still get patched, not just future
    fresh installs."""
    ws = tmp_path / "ws"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text('{"last_refresh": "2026-09-05T00:00:00Z"}')
    (codex_dir / "config.toml").write_text(CONFIG_TOML_BASE)

    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))

    shared_home = ws / ".aw-workspace" / "data" / "agents-platform-runners" / "codex-home"
    shared_home.mkdir(parents=True)
    # Pre-existing shared home, from before this fix — no warm-token header.
    (shared_home / "config.toml").write_text(CONFIG_TOML_BASE)
    (shared_home / "auth.json").write_text('{"last_refresh": "2026-08-01T00:00:00Z"}')

    execute_mod._build_container_kwargs({
        "run_id": "r-existing-home", "cli": "codex", "prompt": "hi",
        "mcp_servers": _mcp_servers_payload("warm-token-def456"),
    })

    assert 'X-Aw-Warm-Token = "AW_MCP_WARM_TOKEN"' in (shared_home / "config.toml").read_text()


def test_no_warm_token_in_job_leaves_env_var_unset(tmp_path, monkeypatch):
    """A dispatch with no mcp_servers at all (e.g. raw/no-MCP job) must not
    crash trying to extract a token that doesn't exist."""
    ws = tmp_path / "ws"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text("{}")
    (codex_dir / "config.toml").write_text(CONFIG_TOML_BASE)

    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))

    _, _, kwargs, _ = execute_mod._build_container_kwargs({
        "run_id": "r-no-mcp", "cli": "codex", "prompt": "hi",
    })

    assert execute_mod.CODEX_WARM_TOKEN_ENV_VAR not in kwargs["environment"]
