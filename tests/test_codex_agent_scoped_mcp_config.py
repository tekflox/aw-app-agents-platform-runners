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

Extended for card 4085bf3b-9510-81ba-adc3-fee01f6cee37 (2026-10-30): the
override above was only ever wired into the `creds_staged` branch, so when
staging itself failed (codex 0.147 leaving Unix sockets in the live
`~/.codex`, Errno 6 on `shutil.copytree`) the run fell through to either the
live-mount fallback or the legacy workspace-copy fallback, both of which
mount codex's SHARED, UNSCOPED config.toml with no override at all — the
regression this file's new tests below cover (socket-tolerant staging via
`_creds_copy_ignore`, the override mounted in all three branches, and
fail-CLOSED instead of fail-open when the render itself cannot be produced).

Run: .venv/aw/bin/python -m pytest tests/test_codex_agent_scoped_mcp_config.py
"""
from __future__ import annotations

import socket
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402


def _make_socket(path: Path) -> None:
    """A real AF_UNIX socket at *path* — exactly what codex 0.147's
    app-server daemon leaves behind in a live ``~/.codex``
    (``app-server-control/app-server-control.sock``,
    ``app-server-daemon/daemon-updater.sock``) and what made
    ``shutil.copytree`` raise ``shutil.Error`` (Errno 6) staging it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()

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
    per-spawn, and its own static-header copy is redundant with the env one).

    X-Aw-Caller-Run-Id IS baked in as a static header now (see
    execute.py's _STALE_PER_TURN_HEADER_NAMES docstring, corrected
    2026-09-19): a warm container holding one config.toml for its whole life
    does freeze it stale, but aw-mcp-gateway's own caller_context.py already
    overrides a frozen header with the live Redis-resolved run id whenever
    resolution succeeds, and falls back to the frozen value when it can't
    (no warm_redis_url configured, Redis down, token unmapped) — dropping the
    header here would remove that fallback and leave the container with NO
    identity at all in exactly that case.

    Authorization rides in via bearer_token_env_var (not a static baked
    value) for the same staleness reason X-Aw-Warm-Token does — see
    _render_codex_config_toml's own docstring and CODEX_GATEWAY_TOKEN_ENV_VAR."""
    cfg = tomllib.loads(
        execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, CRISPAL_SERVERS))
    server = cfg["mcp_servers"]["crispal"]

    assert server["bearer_token_env_var"] == execute_mod.CODEX_GATEWAY_TOKEN_ENV_VAR
    assert server["http_headers"]["X-Aw-Caller-Run-Id"] == "run-0b0aac74"
    assert "X-Aw-Warm-Token" not in server.get("http_headers", {})
    assert server["env_http_headers"]["X-Aw-Warm-Token"] == execute_mod.CODEX_WARM_TOKEN_ENV_VAR
    # The warm token's VALUE never lands in the file (it rides the env var
    # instead); the gateway bearer token likewise never lands as a static value.
    rendered = execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, CRISPAL_SERVERS)
    assert "warm-token-abc123" not in rendered
    assert "live-gateway-token" not in rendered


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


# ---------------------------------------------------------------------------
# features.plugins / features.remote_plugin — card 4085bf3b-9510-81ba (item 4)
# ---------------------------------------------------------------------------

def test_remote_plugins_are_force_disabled_in_the_rendered_config():
    """codex 0.147 dials the ChatGPT account's cached remote connectors
    (Tripadvisor, GitHub, Notion, ...) at startup even with `--disable apps`
    on the argv — a different switch (CLI_SPECS["codex"]'s own comment).
    `features.plugins = false` is the one confirmed-working global kill
    switch (per-plugin `enabled = false` is unreliable upstream); a headless
    runner job has no legitimate use for any codex plugin."""
    cfg = tomllib.loads(
        execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, CRISPAL_SERVERS))

    assert cfg["features"]["plugins"] is False
    assert cfg["features"]["remote_plugin"] is False
    # Still scoped correctly — this switch must not interfere with the
    # mcp_servers replacement the rest of this file covers.
    assert cfg["mcp_servers"]["crispal"]["url"] == CRISPAL_PROFILE_URL


def test_features_disable_survives_a_toml_table_appearing_first():
    """The documented codex quirk: a `features.*` dotted key placed AFTER a
    `[table]` section is silently folded into THAT table instead of the
    root (reproduces on 0.147.0) — so this must be written before
    `[projects.*]`, not appended after it like the mcp_servers tables are."""
    text = execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, CRISPAL_SERVERS)
    lines = [l for l in text.splitlines() if l.strip()]
    first_table_idx = next(i for i, l in enumerate(lines) if l.startswith("["))

    assert any(l.startswith("features.plugins") for l in lines[:first_table_idx])
    assert any(l.startswith("features.remote_plugin") for l in lines[:first_table_idx])


def test_features_disable_is_idempotent_across_rerender():
    """Re-applying the render to its own output (the cold path's every-spawn
    behaviour) must not try to redefine `features.plugins`/`remote_plugin`
    as a second root key — TOML rejects that outright."""
    once = execute_mod._render_codex_config_toml(SHARED_CONFIG_TOML, CRISPAL_SERVERS)
    twice = execute_mod._render_codex_config_toml(once, CRISPAL_SERVERS)

    cfg = tomllib.loads(twice)  # raises on a duplicate key — the real assertion
    assert cfg["features"]["plugins"] is False
    assert cfg["features"]["remote_plugin"] is False
    assert twice.count("features.plugins") == 1
    assert twice.count("features.remote_plugin") == 1


def test_features_disable_overrides_a_manually_edited_features_table():
    """Belt-and-suspenders: even if some future base config carries a
    `[features]` table of its own (not just the dotted form this function
    writes), the render must still force plugins off rather than leave a
    stale `enabled = true` sitting in the kept text."""
    base = SHARED_CONFIG_TOML + '\n[features]\nplugins = true\n'
    cfg = tomllib.loads(execute_mod._render_codex_config_toml(base, {}))

    assert cfg["features"]["plugins"] is False


# ---------------------------------------------------------------------------
# _creds_copy_ignore — card 4085bf3b-9510-81ba (item 1)
# ---------------------------------------------------------------------------

def test_creds_copy_ignore_skips_sockets_by_type(tmp_path):
    """The actual Errno 6 fix: a socket is excluded by its file TYPE, not
    because its name happens to be on a list — the next thing codex's
    daemon invents does not need a new named entry here."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "auth.json").write_text("{}")
    _make_socket(src / "some-new-daemon" / "future.sock")

    ignored = execute_mod._creds_copy_ignore(str(src), ["auth.json", "some-new-daemon"])
    assert "some-new-daemon" not in ignored  # the DIR itself isn't a socket
    nested = execute_mod._creds_copy_ignore(
        str(src / "some-new-daemon"), ["future.sock"])
    assert "future.sock" in nested


def test_creds_copy_ignore_skips_named_app_server_and_connector_dirs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("app-server-control", "app-server-daemon", "openai-curated-remote",
                 "auth.json"):
        (src / name).mkdir() if name != "auth.json" else (src / name).write_text("{}")

    ignored = execute_mod._creds_copy_ignore(str(src), [
        "app-server-control", "app-server-daemon", "openai-curated-remote", "auth.json"])

    assert ignored == {"app-server-control", "app-server-daemon", "openai-curated-remote"}


def test_staged_copy_tolerates_a_socket_and_excludes_connector_cache(monkeypatch):
    """Integration-level 'socket in source': a real socket (plus a populated
    plugins/cache/openai-curated-remote) in the live ~/.codex must no longer
    make staging raise — creds_staged stays True and the agent still gets
    its own scoped override, exactly as if neither existed.

    Uses a hand-rolled, SHORT tmp dir rather than pytest's own `tmp_path`:
    an AF_UNIX socket path is capped at ~108 bytes, and `tmp_path`'s own
    nesting (`/tmp/pytest-of-.../test-name-N/...`) alone is already close to
    that budget before this test's own subdirectories are added.
    """
    import shutil as _shutil
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="rc"))
    try:
        home = root / "h"
        home.mkdir()
        monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
        monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
        monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
        monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(root / "ws"))
        codex_home = home / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text('{"last_refresh": "2026-09-05T00:00:00Z"}')
        (codex_home / "config.toml").write_text(SHARED_CONFIG_TOML)
        _make_socket(codex_home / "asc" / "a.sock")
        _make_socket(codex_home / "asd" / "d.sock")
        (codex_home / "plugins" / "cache" / "openai-curated-remote" / "github").mkdir(parents=True)
        (codex_home / "plugins" / "cache" / "openai-curated-remote" / "github"
         / "manifest.json").write_text("{}")

        _, _, kwargs, _ = execute_mod._build_container_kwargs({
            "run_id": "r-socket", "cli": "codex", "prompt": "hi",
            "mcp_servers": CRISPAL_SERVERS,
        })

        vols = kwargs["volumes"]
        override = [src for src, m in vols.items() if m["bind"] == "/aw-codex-home/config.toml"]
        assert override, f"staging must have stayed True (socket tolerated): {vols}"

        real_copy = home / ".codex" / "isolated" / "r-socket" / "creds" / "plugins" / "cache"
        assert not real_copy.exists(), "the connector cache must not be staged"
    finally:
        _shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fail CLOSED on scope, in every path — card 4085bf3b-9510-81ba (items 2+3)
# ---------------------------------------------------------------------------

def test_staging_failure_still_mounts_a_scoped_override_on_the_live_fallback(tmp_path, monkeypatch):
    """THE regression this card exists to fix: when creds staging itself
    throws (any reason, not just the socket one above), the run must NOT
    fall through to the shared, unscoped config.toml — the live-mount
    fallback branch must mount this run's own scoped override too."""
    home = _paths(monkeypatch, tmp_path)

    def _raise(*_a, **_kw):
        raise OSError("simulated staging failure")
    monkeypatch.setattr(execute_mod.shutil, "copytree", _raise)

    _, _, kwargs, _ = execute_mod._build_container_kwargs({
        "run_id": "r-fallback", "cli": "codex", "prompt": "hi",
        "mcp_servers": CRISPAL_SERVERS,
    })

    vols = kwargs["volumes"]
    # Staging must indeed have failed open into the live-mount branch, not
    # the staged one — otherwise this test is not exercising the fallback.
    assert not any(m["bind"] == "/aw-codex-home/config.toml" for m in vols.values()), vols
    assert any(m["bind"] == f"/home/ubuntu/.codex" and m["mode"] == "rw" for m in vols.values()), vols

    override = [src for src, m in vols.items()
                if m["bind"] == "/home/ubuntu/.codex/config.toml"]
    assert override, f"no scoped override mounted on the live fallback: {vols}"
    assert vols[override[0]]["mode"] == "ro"

    rendered = home / ".codex" / "isolated" / "r-fallback" / "codex-config.toml"
    cfg = tomllib.loads(rendered.read_text())
    assert cfg["mcp_servers"]["crispal"]["url"] == CRISPAL_PROFILE_URL
    assert "aw-gateway" not in cfg["mcp_servers"]
    assert cfg["features"]["plugins"] is False


def test_legacy_copy_fallback_also_mounts_a_scoped_override(tmp_path, monkeypatch):
    """The third codex path (no separate $HOME bind-mount at all) must get
    the same per-run override as the other two."""
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text('{"last_refresh": "2026-09-05T00:00:00Z"}')
    (codex / "config.toml").write_text(SHARED_CONFIG_TOML)
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")  # no direct_home_mount
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))

    _, _, kwargs, _ = execute_mod._build_container_kwargs({
        "run_id": "r-legacy", "cli": "codex", "prompt": "hi",
        "mcp_servers": CRISPAL_SERVERS,
    })

    vols = kwargs["volumes"]
    override = [src for src, m in vols.items()
                if m["bind"] == "/home/ubuntu/.codex/config.toml"]
    assert override, f"no scoped override mounted on the legacy fallback: {vols}"
    assert vols[override[0]]["mode"] == "ro"
    assert override[0].startswith("/host/aw-workspace/"), override[0]

    rendered = tmp_path / "ws" / ".codex" / "isolated" / "r-legacy" / "codex-config.toml"
    cfg = tomllib.loads(rendered.read_text())
    assert cfg["mcp_servers"]["crispal"]["url"] == CRISPAL_PROFILE_URL
    assert "aw-gateway" not in cfg["mcp_servers"]


@pytest.mark.parametrize("failing_branch", ["staged", "live_fallback", "legacy"])
def test_render_failure_fails_closed_not_open(tmp_path, monkeypatch, failing_branch):
    """If the render/write itself cannot be produced — in ANY of the three
    codex paths — the spawn must abort loudly (CodexScopeRenderError) rather
    than silently proceed on the shared, unscoped config.toml. This is the
    opposite of the pre-fix behaviour, which caught and logged."""
    if failing_branch == "legacy":
        home = tmp_path / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "auth.json").write_text("{}")
        (home / ".codex" / "config.toml").write_text(SHARED_CONFIG_TOML)
        monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
        monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")
        monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
        monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    else:
        _paths(monkeypatch, tmp_path)
        if failing_branch == "live_fallback":
            monkeypatch.setattr(execute_mod.shutil, "copytree",
                                 lambda *_a, **_kw: (_ for _ in ()).throw(OSError("boom")))

    monkeypatch.setattr(execute_mod, "_render_codex_config_toml",
                         lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("rendering exploded")))

    with pytest.raises(execute_mod.CodexScopeRenderError):
        execute_mod._build_container_kwargs({
            "run_id": "r-fail-closed", "cli": "codex", "prompt": "hi",
            "mcp_servers": CRISPAL_SERVERS,
        })
