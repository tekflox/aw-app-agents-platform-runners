"""Regression tests for card 3d55bf3b-9510-81ea-9a4e-fcd0753602f4
("Crispal kb_index isolation leak", 2026-09-08).

crispal-codex wrote knowledge-base documents at the workspace ROOT
(`memory/...`, `docs/atendimento/...`) instead of under `crispal/`, and could
read every other tenant's — even though its agent-config
(`agent-config-crispal-admin`) names exactly one MCP server, the SCOPED
gateway profile `http://aw-app-mcp-gateway:9200/mcp/crispal-full`, whose
`kb_index="crispal"` the gateway enforces correctly.

The enforcement point was never the bug. Codex has no `--mcp-config` flag
(`CLI_SPECS["codex"]["mcp_config_flag"] is None`), so it never received that
agent-config at all: it read the ONE shared `$CODEX_HOME/config.toml` that
`aw-workspace-cli agent sync` writes, whose single `aw-gateway` entry points
at the gateway's UNSCOPED ROOT `/mcp`. The leaking run's own event trace shows
it: tool name `aw-gateway.aw__kb__update_knowledge_base` — the workspace-
prefixed AGGREGATED name only the root endpoint serves — and a result reading
`Updated: memory/...` with no `crispal/` prefix.

Bypassing `/mcp/<profile>` bypasses that profile's `upstreams` allow-list,
`tools_allow`, run policy and `presentation_namespace` too; the KB paths were
just the part that left evidence on disk.

The fix (`_render_codex_config_toml` + the per-run override mount in
`_build_container_kwargs`) gives codex the same per-agent MCP config claude
gets from `--mcp-config`: the shared config.toml with its `[mcp_servers]` tree
replaced wholesale by this job's own resolved servers, bind-mounted read-only
over `$CODEX_HOME/config.toml` — a single FILE, so `auth.json`, the rollouts
and `state_*.sqlite` in that same shared dir survive untouched.

Run: .venv/aw/bin/python -m pytest tests/test_codex_agent_scoped_mcp_config.py
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

#: Byte-for-byte the shape of the live shared $CODEX_HOME/config.toml on
#: 2026-09-08 (read from
#: .aw-workspace/data/agents-platform-runners/codex-home/config.toml), token
#: redacted. The `aw-gateway` entry here IS the leak.
SHARED_CONFIG_TOML = (
    '[projects."/opt/aw-workspace"]\n'
    'trust_level = "trusted"\n'
    "\n"
    '[projects."/tmp"]\n'
    'trust_level = "trusted"\n'
    "\n"
    "[mcp_servers.aw-gateway]\n"
    'url = "http://aw-app-mcp-gateway:9200/mcp"\n'
    "\n"
    "[mcp_servers.aw-gateway.http_headers]\n"
    'Authorization = "Bearer static-gateway-token"\n'
    "\n"
    "[mcp_servers.aw-gateway.env_http_headers]\n"
    'X-Aw-Warm-Token = "AW_MCP_WARM_TOKEN"\n'
)

CRISPAL_PROFILE_URL = "http://aw-app-mcp-gateway:9200/mcp/crispal-full"

#: What agents-platform-multitenant ships over the wire as `job["mcp_servers"]`
#: for crispal-codex — its agent-config's one server, gateway token injected,
#: plus the per-run/per-session identity headers executor.py adds to every
#: runner dispatch.
CRISPAL_SERVERS = {
    "crispal": {
        "type": "streamable-http",
        "url": CRISPAL_PROFILE_URL,
        "headers": {
            "Authorization": "Bearer live-gateway-token",
            "X-Aw-Caller-Run-Id": "run-0b0aac74",
            "X-Aw-Warm-Token": "warm-token-abc123",
        },
    }
}


def _codex_home(tmp_path: Path) -> Path:
    """A fake $HOME with a logged-in .codex, enough for direct_home_mount."""
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text('{"last_refresh": "2026-09-05T00:00:00Z"}')
    (codex / "config.toml").write_text(SHARED_CONFIG_TOML)
    return home


def _paths(monkeypatch, tmp_path: Path) -> Path:
    home = _codex_home(tmp_path)
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    return home


# ---------------------------------------------------------------------------
# _render_codex_config_toml
# ---------------------------------------------------------------------------

def test_scoped_profile_replaces_the_unscoped_root_entry():
    """THE leak, at the rendering layer: given an agent that names only the
    scoped profile, the rendered config must carry that profile's path and
    must NOT carry a bare `/mcp` gateway URL anywhere."""
    text = execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, CRISPAL_SERVERS)

    cfg = tomllib.loads(text)
    assert set(cfg["mcp_servers"]) == {"crispal"}
    assert cfg["mcp_servers"]["crispal"]["url"] == CRISPAL_PROFILE_URL
    # The unscoped root, in every form it could survive as.
    assert "aw-gateway" not in cfg["mcp_servers"]
    assert '"http://aw-app-mcp-gateway:9200/mcp"' not in text
    assert not any(u.rstrip("/").endswith(":9200/mcp")
                   for u in (s.get("url", "") for s in cfg["mcp_servers"].values()))


def test_everything_outside_mcp_servers_is_preserved():
    """codex refuses to run non-interactively in an untrusted project, so
    dropping the [projects.*] tables while replacing [mcp_servers] would
    trade an isolation bug for a total outage."""
    cfg = tomllib.loads(
        execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, CRISPAL_SERVERS))

    assert cfg["projects"]["/opt/aw-workspace"]["trust_level"] == "trusted"
    assert cfg["projects"]["/tmp"]["trust_level"] == "trusted"


def test_an_agent_naming_no_gateway_server_gets_no_gateway_server():
    """agent-config-crispal-dev / -image / -social name ONLY
    `http://aw-app-crispal:9410/mcp` — no gateway entry at all. A codex agent
    on any of those held the full unscoped gateway anyway, because the shared
    config.toml was merged into rather than replaced. Replacement is the
    contract: what the agent config does not name, the agent does not get."""
    servers = {"crispal": {"url": "http://aw-app-crispal:9410/mcp",
                           "headers": {"Authorization": "Bearer app-token"}}}

    cfg = tomllib.loads(execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, servers))

    assert set(cfg["mcp_servers"]) == {"crispal"}
    assert cfg["mcp_servers"]["crispal"]["url"] == "http://aw-app-crispal:9410/mcp"


def test_no_servers_at_all_leaves_no_mcp_servers_table():
    cfg = tomllib.loads(execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, {}))

    assert "mcp_servers" not in cfg
    assert cfg["projects"]["/opt/aw-workspace"]["trust_level"] == "trusted"


def test_static_headers_are_baked_but_per_turn_identity_headers_are_not():
    """X-Aw-Warm-Token keeps arriving through env_http_headers (its VALUE is
    per-spawn); X-Aw-Caller-Run-Id is per-TURN and a warm container holding
    one config.toml for its whole life would freeze a stale one — the exact
    bug the warm-token mechanism exists to avoid."""
    cfg = tomllib.loads(
        execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, CRISPAL_SERVERS))
    server = cfg["mcp_servers"]["crispal"]

    assert server["http_headers"]["Authorization"] == "Bearer live-gateway-token"
    assert "X-Aw-Caller-Run-Id" not in server["http_headers"]
    assert "X-Aw-Warm-Token" not in server["http_headers"]
    assert server["env_http_headers"]["X-Aw-Warm-Token"] == execute_mod.CODEX_WARM_TOKEN_ENV_VAR
    # And the token VALUE never lands in the file.
    assert "warm-token-abc123" not in execute_mod._render_codex_config_toml(
        SHARED_CONFIG_TOML, CRISPAL_SERVERS)


def test_server_names_that_are_not_bare_toml_keys_still_parse():
    """A server name is free text from an agent config; `aw-gateway/crispal`
    is not a valid bare TOML key and would make codex reject the whole file
    ("invalid unquoted key"), i.e. take out every MCP server, not just one."""
    servers = {"aw-gateway/crispal": {"url": CRISPAL_PROFILE_URL}}

    cfg = tomllib.loads(execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, servers))

    assert cfg["mcp_servers"]["aw-gateway/crispal"]["url"] == CRISPAL_PROFILE_URL


def test_rendering_is_stable_when_reapplied_to_its_own_output():
    """A warm container's config is rendered once and frozen, but the cold
    path re-renders every spawn from whatever the shared home holds — that
    must not accumulate duplicate tables (TOML rejects a redefined table)."""
    once = execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, CRISPAL_SERVERS)
    twice = execute_mod._render_codex_config_toml(once, CRISPAL_SERVERS)

    assert tomllib.loads(twice) == tomllib.loads(once)


# ---------------------------------------------------------------------------
# _build_container_kwargs — the wiring that actually reaches a container
# ---------------------------------------------------------------------------

def test_codex_spawn_mounts_the_scoped_config_over_the_shared_one(tmp_path, monkeypatch):
    """The test that would have caught the live leak: a codex dispatch whose
    agent config names only the scoped profile must end up with a container
    whose $CODEX_HOME/config.toml is the rendered per-agent file."""
    home = _paths(monkeypatch, tmp_path)

    _, _, kwargs, _ = execute_mod._build_container_kwargs({
        "run_id": "r-crispal", "cli": "codex", "prompt": "hi",
        "mcp_servers": CRISPAL_SERVERS,
    })

    vols = kwargs["volumes"]
    override = [src for src, m in vols.items()
                if m["bind"] == "/aw-codex-home/config.toml"]
    assert override, f"no per-agent config.toml override mounted: {vols}"
    assert vols[override[0]]["mode"] == "ro"
    assert override[0].startswith("/host/aw-workspace-home/"), override[0]

    # The shared home itself is still mounted rw and NOT replaced — auth.json,
    # rollouts and state_*.sqlite live there.
    assert any(m["bind"] == "/aw-codex-home" and m["mode"] == "rw"
               for m in vols.values()), vols

    rendered = home / ".codex" / "isolated" / "r-crispal" / "codex-config.toml"
    cfg = tomllib.loads(rendered.read_text())
    assert cfg["mcp_servers"]["crispal"]["url"] == CRISPAL_PROFILE_URL
    assert "aw-gateway" not in cfg["mcp_servers"]
    assert '"http://aw-app-mcp-gateway:9200/mcp"' not in rendered.read_text()


def test_codex_spawn_with_no_servers_still_strips_the_shared_gateway(tmp_path, monkeypatch):
    home = _paths(monkeypatch, tmp_path)

    execute_mod._build_container_kwargs({
        "run_id": "r-no-mcp", "cli": "codex", "prompt": "hi",
    })

    rendered = home / ".codex" / "isolated" / "r-no-mcp" / "codex-config.toml"
    assert "mcp_servers" not in tomllib.loads(rendered.read_text())


def test_the_shared_home_keeps_its_own_unscoped_config(tmp_path, monkeypatch):
    """The override is per-run and mounted, never written back — two agents
    with different scopes run concurrently against this one shared dir, so
    rewriting it in place would be a race with a cross-tenant payload."""
    _paths(monkeypatch, tmp_path)
    shared = (tmp_path / "ws" / ".aw-workspace" / "data"
              / "agents-platform-runners" / "codex-home")
    shared.mkdir(parents=True)
    shared.joinpath("config.toml").write_text(SHARED_CONFIG_TOML)
    shared.joinpath("auth.json").write_text('{"last_refresh": "2026-08-01T00:00:00Z"}')

    execute_mod._build_container_kwargs({
        "run_id": "r-shared", "cli": "codex", "prompt": "hi",
        "mcp_servers": CRISPAL_SERVERS,
    })

    assert "aw-gateway" in tomllib.loads(shared.joinpath("config.toml").read_text())["mcp_servers"]


def test_a_never_populated_shared_home_is_still_backfilled(tmp_path, monkeypatch):
    """The container entrypoint's `[ -f config.toml ] || cp -a /aw-creds/.`
    reads TRUE unconditionally once the override is mounted over that path,
    so first-populate has to happen host-side or a fresh install silently
    ends up with an empty $CODEX_HOME."""
    _paths(monkeypatch, tmp_path)
    shared = (tmp_path / "ws" / ".aw-workspace" / "data"
              / "agents-platform-runners" / "codex-home")

    execute_mod._build_container_kwargs({
        "run_id": "r-fresh", "cli": "codex", "prompt": "hi",
        "mcp_servers": CRISPAL_SERVERS,
    })

    assert (shared / "config.toml").is_file()
    assert (shared / "auth.json").is_file()


def test_claude_is_untouched(tmp_path, monkeypatch):
    """claude gets its per-agent config from --mcp-config and has no
    $CODEX_HOME — this fix must not add a mount to its spawns."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))

    _, _, kwargs, _ = execute_mod._build_container_kwargs({
        "run_id": "r-claude", "cli": "claude", "prompt": "hi",
        "mcp_servers": CRISPAL_SERVERS,
    })

    assert not any("config.toml" in m["bind"] for m in kwargs["volumes"].values())
