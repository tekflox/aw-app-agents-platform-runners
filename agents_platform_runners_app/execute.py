"""``POST /execute`` — spawn an agent CLI container INSIDE this workspace's
own container engine (podman, via ``AW_CONTAINER_SOCKET``) and stream its
output back to the caller (agents-platform_multitenant's ``RunnerLLM``) over
the SAME Redis Stream mechanism agents-platform_multitenant already uses for
its own local docker-CLI runs (``backend/app/core/redis_streams.py`` there —
key scheme ``run:{run_id}:events``, XADD field ``line``/``done``).

Why this exists (2026-08-02 architecture change): agents-platform_multitenant
used to spawn every CLI-agent container as a SIBLING on its OWN host's Docker
daemon (``core/tools/docker_agent.py``), which meant every run mounted the
LEGACY monolith's ``/opt/agentic-workspace`` regardless of which workspace the
run's agent config belonged to. A "Runner" is this app: the execution service
that runs INSIDE a workspace (here, ``aw-workspace``) and mounts THAT
workspace's own paths. agents-platform_multitenant's ``RunnerLLM`` calls
``POST {base_url}/execute`` instead of touching docker.sock itself.

Design decisions, verified live (not assumed) on 2026-08-02:

* **Reachability**: this app's routes are NOT reachable from
  agents-platform_multitenant on the plain docker bridge network — the
  workspace is a nested podman container inside ``aw-remote-host`` and only
  binds ``127.0.0.1:9030`` in ITS OWN netns. The only proven path is the
  public BYOD tunnel edge: ``https://api.<slug>.workspace.<domain>/api/apps/
  agents-platform-runners/...`` (aw-backend's ``workspace_tunnel_proxy``
  bridges this over the workspace's live ``/link`` tunnel). That edge
  middleware normally also enforces `aw_id_jwt` + DB workspace-membership —
  bypassed here because this app's ``AppInstall.config.public`` was set to
  ``true`` (the documented "public-app carve-out" in
  ``workspace_tunnel_proxy.py``), which makes **this app's own auth surface
  the sole gate** for its traffic — hence the shared-secret check below.
* **Auth**: TWO layers, both required:
  1. aw-workspace's own per-app ``IdentityGuard`` still verifies the
     ``Authorization: Bearer <token>`` carries a JWT with a VALID SIGNATURE
     from aw-backend's identity key (no DB lookup — signature-only). Any
     caller must possess an aw-backend-issued identity JWT.
  2. This route itself additionally requires ``X-Runner-Secret: <secret>`` to
     match ``config["execute_secret"]`` — defense in depth now that the edge
     membership check is bypassed for this public app. Both together mean a
     caller needs BOTH a validly-signed identity JWT AND the specific shared
     secret configured on this app's Settings — neither alone is enough.
* **Streaming**: Redis Stream reuse, NOT a new protocol. Verified live:
  this workspace's own container CAN reach the exact same shared Redis
  instance agents-platform_multitenant uses for ``AP_REDIS_URL``
  (``redis://<pw>@aw-sandbox:6379/1`` from the sandbox's own network — reached
  here via the docker bridge gateway IP ``172.18.0.1:6379``, since
  ``aw-sandbox`` the DNS name doesn't resolve from inside the nested podman
  netns). Configured via this app's ``shared_redis_url`` secret.
* **Container spawn**: this app was granted ``containers:manage`` (Tier-2
  capability) purely to unlock ``AW_CONTAINER_SOCKET`` — spawn goes through
  the raw ``docker`` SDK talking to that socket directly (NOT through
  ``ctx.containers``/``ContainerSupervisor``, which is a one-sidecar-per-app
  registry, not a per-job ephemeral-container spawner). Mount sources are
  paths on the PODMAN HOST's filesystem (``aw-remote-host``'s own fs, since
  ``AW_CONTAINER_SOCKET`` talks to ITS nested podman, spawning SIBLING
  containers, not children of this app's own container) — that's
  ``AW_WORKSPACE_HOST_DIR`` (``/home/aw-remote-host/aw-workspace``), which is
  the SAME underlying directory tree as this process's own ``AW_WORKSPACE_
  CONTAINER_DIR`` (``/opt/aw-workspace``, == ``$HOME`` here) — a file created
  at one path is instantly visible at the other, so this app can ``mkdir()``
  isolated run dirs locally and reference them via the host-side path when
  building the spawned container's volume mounts.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import warm_pool

log = logging.getLogger("aw_apps.agents_platform_runners.execute")

# Path as seen by the PODMAN DAEMON (aw-remote-host's own filesystem) — used
# for volume mount SOURCES on the docker/podman SDK calls below.
WORKSPACE_HOST_DIR = os.environ.get("AW_WORKSPACE_HOST_DIR", "")
# The SAME directory tree, as seen by THIS process (== $HOME here) — used to
# mkdir isolated run dirs locally before referencing them via the host path.
WORKSPACE_CONTAINER_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
# Host path of the workspace's OWN $HOME (/home/ubuntu). The launcher
# (aw-remote-host ops) bind-mounts $HOME to this host dir, so the LIVE creds
# under $HOME/.claude are a valid mount SOURCE for the sibling CLI containers
# this app spawns — no copy needed. Empty on setups that don't separately
# bind-mount $HOME (then the legacy copy-into-workspace fallback kicks in).
WORKSPACE_HOME_HOST_DIR = os.environ.get("AW_WORKSPACE_HOME_HOST_DIR", "")
# This process's real $HOME as IT sees it (container-side path) — where the
# live credentials the workspace's own login writes actually resolve.
REAL_HOME = os.environ.get("HOME") or "/home/ubuntu"
CONTAINER_SOCKET = os.environ.get("AW_CONTAINER_SOCKET")
# Host path of the docker socket handed to an agent that has the Agent
# Config's "docker" permission. Distinct from CONTAINER_SOCKET above, which
# is THIS app's own podman socket used to spawn the agent container in the
# first place — the permission is about what the spawned agent may reach,
# not how it gets spawned.
DOCKER_SOCKET_PATH = os.environ.get("AW_DOCKER_SOCKET_PATH", "/var/run/docker.sock")
# Persistent path where the workspace's long-lived Claude OAuth token
# (`claude setup-token`, valid ~1 year) is stored. Lives under
# `.aw-workspace/` — on the persistent /opt/aw-workspace bind-mount, preserved
# across workspace update/restart. When present it is injected into spawned
# claude containers as CLAUDE_CODE_OAUTH_TOKEN: env-token auth doesn't rotate
# every 8h and never writes/blanks .credentials.json, so it survives updates
# and is immune to the shared-account refresh-token rotation that a copied
# .credentials.json suffers (see
# aw-workspace-runner-claude-oauth-token-not-refreshed-relogin-20260807).
CLAUDE_OAUTH_TOKEN_FILE = os.environ.get(
    "AW_CLAUDE_OAUTH_TOKEN_FILE",
    os.path.join(os.environ.get("AW_WORKSPACE_HOME") or f"{WORKSPACE_CONTAINER_DIR}/.aw-workspace",
                 "secrets", "claude_code_oauth_token"),
)
# aw-app-git's own data dir (fs:workspace-data — see that app's gh_auth.py
# _sync_creds_to_data_dir()), relative to WORKSPACE_CONTAINER_DIR so it can
# be mounted via the SAME WORKSPACE_HOST_DIR-relative helper (_mount) as
# every other WORKSPACE_CONTAINER_DIR-rooted path in this file — matches
# AW_WORKSPACE_HOME's own default (".aw-workspace" under the container dir,
# see CLAUDE_OAUTH_TOKEN_FILE above), not a separate/new convention.
GIT_CREDS_REL = ".aw-workspace/data/git"


def _claude_oauth_token() -> str:
    """Long-lived Claude OAuth token for spawned claude containers, if set —
    env var first, then the persistent secret file. Empty string when neither
    exists (falls back to the mounted .credentials.json auth)."""
    t = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if t:
        return t
    try:
        p = Path(CLAUDE_OAUTH_TOKEN_FILE)
        if p.is_file():
            return p.read_text().strip()
    except Exception:
        log.warning("execute: could not read %s", CLAUDE_OAUTH_TOKEN_FILE, exc_info=True)
    return ""
CONTAINER_NETWORK = os.environ.get("AW_CONTAINER_NETWORK")

# Same registry/prefix convention as agents-platform's own
# core/tools/docker_agent.py — these images are the single shared source of
# CLI agent images across every runner, not something this app builds itself.
REGISTRY = os.environ.get("AW_AGENT_REGISTRY", "ghcr.io")
IMAGE_PREFIX = os.environ.get("AW_AGENT_IMAGE_PREFIX", "fredericowu/aw-sandbox-agent-cli")
DEFAULT_TAG = os.environ.get("AW_AGENT_TAG", "latest")

# Trimmed mirror of agents-platform's docker_agent.CLI_SPECS — only the
# fields /execute actually needs. Kept in sync by hand (small, stable table);
# see that file's module docstring for the source of truth this was copied
# from (agent-images/<cli>/Dockerfile is what actually defines each CLI's
# entrypoint contract).
CLI_SPECS: dict[str, dict] = {
    "claude": {
        "bin": "claude", "subcmd": None, "prompt_flag": "-p",
        "default_extra": ["--output-format", "stream-json", "--verbose"],
        "skip_perms_flag": "--dangerously-skip-permissions",
        "model_flag": "--model", "add_dir_flag": "--add-dir",
        "mcp_config_flag": "--mcp-config",
        "strict_mcp_flag": "--strict-mcp-config",
        "allowed_tools_flag": "--allowed-tools",
        "disallowed_tools_flag": "--disallowed-tools",
        "tools_flag_style": "csv",          # --allowed-tools a,b,c
        "append_system_prompt_flag": "--append-system-prompt",
        # Auth reaches the container as CLAUDE_CODE_OAUTH_TOKEN, so the CLI
        # never has to READ the mounted creds — which is the only reason the
        # uid mismatch below doesn't bite claude.
        "env_token_auth": True,
        "creds_dir": ".claude", "creds_file": ".claude.json",
    },
    "codex": {
        "bin": "codex", "subcmd": "exec", "prompt_flag": None,
        "default_extra": ["--skip-git-repo-check", "--json"],
        "skip_perms_flag": "--dangerously-bypass-approvals-and-sandbox",
        "model_flag": "-c", "add_dir_flag": None,
        "mcp_config_flag": None,  # codex has no --mcp-config flag — see write-up below
        # codex genuinely has no per-tool allow/deny flags and no
        # system-prompt flag (checked against codex-cli 0.147.0 --help); the
        # system prompt is prepended to the prompt text instead (see below).
        "allowed_tools_flag": None,
        "disallowed_tools_flag": None,
        "tools_flag_style": None,
        "append_system_prompt_flag": None,
        # auth_mode "chatgpt" has no API key to inject — codex MUST read
        # auth.json off disk, and write its session state back.
        "env_token_auth": False,
        # Lets the creds land somewhere the RUN USER can actually write —
        # see the staging note in _build_spawn().
        "home_env": "CODEX_HOME",
        "creds_dir": ".codex", "creds_file": None,
    },
    "copilot": {
        "bin": "copilot", "subcmd": None, "prompt_flag": "-p",
        "default_extra": ["--allow-all-tools"],
        "skip_perms_flag": None,
        "model_flag": "--model", "add_dir_flag": "--add-dir",
        "mcp_config_flag": None,
        # GitHub Copilot CLI 1.0.79: --allow-tool / --deny-tool, and they are
        # REPEATED once per tool, not comma-joined like claude's. Its own help
        # example is the spec:
        #   copilot --allow-tool='shell(git:*)' --deny-tool='shell(git push)'
        "allowed_tools_flag": "--allow-tool",
        "disallowed_tools_flag": "--deny-tool",
        "tools_flag_style": "repeat",
        # No --append-system-prompt equivalent — prompt-prepended instead.
        "append_system_prompt_flag": None,
        "creds_dir": ".copilot", "creds_file": None,
    },
    "cursor-agent": {
        "bin": "cursor-agent", "subcmd": None, "prompt_flag": None,
        "default_extra": ["--print"],
        # cursor-agent 2026.08.11: --force ("Run Everything", --yolo is its
        # alias) IS the skip-permissions equivalent, and --add-dir exists.
        # Both were None here, so a dangerous_skip_permissions=true agent ran
        # WITH prompts (i.e. hung waiting for a human that a headless run
        # does not have) and extra dirs were dropped.
        "skip_perms_flag": "--force",
        "model_flag": "--model", "add_dir_flag": "--add-dir",
        "mcp_config_flag": None,
        # No per-tool allow/deny and no system-prompt flag in its --help.
        "allowed_tools_flag": None,
        "disallowed_tools_flag": None,
        "tools_flag_style": None,
        "append_system_prompt_flag": None,
        "creds_dir": ".cursor", "creds_file": None,
    },
}

STREAM_MAXLEN = 50_000
STREAM_TTL_S = 86400





def _cli_home_rel(creds_dir: str) -> str:
    """Workspace-relative dir bind-mounted as this CLI's home (CODEX_HOME).

    ONE shared home, deliberately — not one per session. Keying it by
    ``session_id or run_id`` looked right and was not: the FIRST turn of a
    conversation has no session_id yet, so its rollout landed under the run
    id, and the follow-up — which finally HAS a session id — looked in a
    different, empty directory and failed to resume exactly as before::

        thread/resume failed: no rollout found for thread id <id>

    codex already partitions conversations by thread id inside one home,
    exactly like a normal ~/.codex install, so there is nothing to split.
    """
    return os.path.join(".aw-workspace", "data", "agents-platform-runners",
                        f"{creds_dir.lstrip('.')}-home")


def _prepare_tmp_mount_source() -> str:
    """Create the dir that gets bound over the container's /tmp, 0777, and
    return it RELATIVE to the workspace tree.

    The bind replaces the image's own /tmp (1777). Two things went wrong with
    the old ``data/sandbox-tmp``:

    * that path is not one the workspace creates, so podman created it — and
      podman creates a missing bind source as **root:root 0755**. The
      container runs as the workspace uid, not root, so claude could not make
      its scratch dir::

          EACCES: permission denied, mkdir '/tmp/claude-1001'

      It dies there, before the turn, and the run lands green with that line
      as its whole output.
    * the workspace could not repair it either: podman had made the PARENT
      ``data/`` root-owned too, so mkdir, chmod and even rmdir all failed
      from uid 1001. Self-healing was impossible by construction.

    So the source now lives under AW_WORKSPACE_HOME, which the workspace owns
    and creates itself — podman never invents it — and which is also where
    CLAUDE.md says durable per-app state belongs, rather than a bare ``data/``
    at the repo root.

    Only the COLD path ever showed this: a warm container keeps the /tmp it
    already made, so an agent holding tmp_access looks healthy right up until
    it starts a fresh session.
    """
    rel = os.path.join(".aw-workspace", "data", "agents-platform-runners", "sandbox-tmp")
    path = Path(WORKSPACE_CONTAINER_DIR) / rel
    try:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o777)   # unconditional — a pre-existing narrow dir must be widened
    except Exception:
        log.exception("execute: could not prepare %s for the /tmp mount", path)
    return rel


def _codex_auth_mode(home: Path) -> str:
    """``auth_mode`` out of codex's auth.json ("chatgpt" | "apikey"), or "" if
    it can't be read. Best-effort on purpose: an unreadable file must not stop
    a run, it just means no override is suppressed."""
    try:
        return json.loads((home / ".codex" / "auth.json").read_text()).get("auth_mode") or ""
    except Exception:
        return ""


def _stream_key(run_id: str) -> str:
    return f"run:{run_id}:events"


def _redis_client(redis_url: str):
    import redis  # sync client — this runs inside its own worker thread
    return redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=30)


_attach_module = None


def _attach_helper():
    """Load ``agent-images/shared/aw_attach.py`` — the SAME file the warm
    relay imports inside the spawned container, so both output paths rewrite
    identically and there is only one copy to keep correct. It lives outside
    this package (it has to be bind-mountable into an image that has no
    access to this app's python env), hence the explicit path load."""
    global _attach_module
    if _attach_module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("aw_attach", str(ATTACH_HELPER_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _attach_module = mod
    return _attach_module


def _publish_line(r, run_id: str, line: str) -> None:
    # Cold/ephemeral path's counterpart to the rewrite aw-warm-relay.py does
    # for warm containers — an [[ATTACH]] pointing at a file only reachable on
    # this side of the wall is swapped for an artefact reference before the
    # line reaches agents-platform. See aw_attach.py.
    try:
        line = _attach_helper().rewrite_stream_line(line, run_id)
    except Exception:
        log.exception("execute: attach rewrite failed run=%s (line published unchanged)", run_id)
    try:
        r.xadd(_stream_key(run_id), {"line": line}, maxlen=STREAM_MAXLEN, approximate=True)
    except Exception:
        log.exception("execute: publish_line failed run=%s", run_id)


def _publish_done(r, run_id: str, returncode: int) -> None:
    try:
        r.xadd(_stream_key(run_id), {"done": "1", "returncode": str(returncode)},
               maxlen=STREAM_MAXLEN, approximate=True)
        r.expire(_stream_key(run_id), STREAM_TTL_S)
    except Exception:
        log.exception("execute: publish_done failed run=%s", run_id)


def _sync_home_creds_into_workspace(real_home: Path, spec: dict) -> None:
    """Best-effort ``cp -a``-equivalent of this CLI's creds_dir/creds_file
    from the process's real ``$HOME`` into ``WORKSPACE_CONTAINER_DIR`` — see
    the call site's comment for why this exists. Never raises: a stale or
    missing source just means the NEXT spawn falls back to whatever was
    already synced (or mounts nothing, same as before this existed)."""
    for rel in filter(None, [spec.get("creds_dir"), spec.get("creds_file")]):
        src = real_home / rel
        dst = Path(WORKSPACE_CONTAINER_DIR) / rel
        try:
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except Exception:
            log.warning("execute: creds resync %s -> %s failed", src, dst, exc_info=True)


def _build_container_kwargs(job: dict) -> tuple[str, list[str], dict, str | None]:
    """Return (image, command_argv, docker-SDK run kwargs) for this job.

    Mirrors agents-platform's ``build_docker_argv`` narrowed to what a single
    stateless job needs — no warm-pool, no WS/legacy modes (this app always
    speaks the Redis-publish path itself).

    Session persistence (fixed 2026-08-03 — root cause of "every Runner turn
    starts a fresh conversation"): the ACTUAL container cwd for the spawned
    CLI is the stable ``working_dir="/opt/aw-workspace"`` kwarg below (set by
    the 2026-08-02 ``a8db328`` change) — NOT this isolated dir, which is only
    mounted, never cd'd into (confirmed live: ``.claude/projects/-opt-aw-
    workspace/`` is the one actually being written to turn over turn). So
    claude's cwd-derived project-file lookup was ALREADY stable; keying this
    isolated scratch dir by run_id (matching agents-platform's own
    ``docker_agent.py`` line ~317-326 exactly — "Each run gets its own
    project dir... so the claude CLI never auto-loads sessions from a
    sibling run") is correct and deliberately NOT session_id-keyed, to avoid
    two queued turns of the same session racing on one directory. The real
    (and only) defect was that ``job.get("session_id")`` was captured into
    ``AW_SESSION_ID`` env but never turned into a ``--resume`` argv flag —
    fixed below, mirroring docker_agent.py's own ``--resume <session_id>`` /
    ``resume <session_id>`` (codex) convention, which resolves by session ID
    globally across ``~/.claude/projects/**`` regardless of cwd.
    """
    cli = job.get("cli") or "claude"
    if cli not in CLI_SPECS:
        cli = "claude"
    spec = CLI_SPECS[cli]
    image = f"{REGISTRY}/{IMAGE_PREFIX}-{cli}:{DEFAULT_TAG}"

    run_id = job["run_id"]
    session_id = job.get("session_id")

    # Preferred credential strategy (replicates agentic-workspace's
    # docker_agent.py, per Frederico 2026-08-07): when we know the host path of
    # the workspace's own $HOME, mount the LIVE $HOME/.claude rw DIRECTLY as the
    # sibling container's ~/.claude — no copy, no read-only shadow — so any
    # OAuth token refresh the CLI does persists straight back to the source of
    # truth. Falls back to the legacy copy-into-workspace path only when $HOME
    # has no separate host bind-mount. (Root cause fixed:
    # aw-workspace-runner-claude-oauth-token-not-refreshed-relogin-20260807.)
    _real_home = Path(REAL_HOME)
    direct_home_mount = bool(WORKSPACE_HOME_HOST_DIR) and (_real_home / spec["creds_dir"]).is_dir()

    # Isolated per-RUN (not per-session) scratch dir — matches legacy exactly
    # (docker_agent.py always keys by agent_id/run_id, never session_id). In
    # direct-home mode it lives UNDER the creds dir that's already mounted
    # whole (docker_agent.py does the same — no separate mount); otherwise it
    # sits in the host-shared workspace tree and is mounted explicitly below.
    #
    # It MUST hang off THIS cli's creds_dir, not a hardcoded ".claude": in
    # direct-home mode only the selected CLI's own creds dir is mounted, so a
    # codex run got a working_dir under an unmounted .claude and podman
    # refused to start the container outright —
    #   Error: workdir "/home/ubuntu/.claude/isolated/<run_id>" does not exist
    # Confirmed by hand 2026-08-13; the same command with the cwd under
    # .codex/ starts fine. Only bites once a CLI has creds on disk (that is
    # what turns direct_home_mount on), so codex "worked" — as a 401 — right
    # up until someone logged it in.
    isolated_rel = f"{spec['creds_dir']}/isolated/{run_id}"
    isolated_base = _real_home if direct_home_mount else Path(WORKSPACE_CONTAINER_DIR)
    isolated_host_dir = isolated_base / isolated_rel
    isolated_host_dir.mkdir(parents=True, exist_ok=True)
    isolated_container_path = f"/home/ubuntu/{isolated_rel}"

    # MCP config: agents-platform_multitenant's executor.py resolves the
    # agent's configured MCP servers (incl. gateway-token injection and the
    # X-Aw-Caller-Run-Id header) and ships the FINAL dict over the wire as
    # job["mcp_servers"] — this app has no filesystem access to that host's
    # own mcp-config directory, so the config itself crosses the wire
    # instead of a path to it. Written straight into the isolated run dir
    # (already mounted rw below), no separate mount needed.
    mcp_config_container_path: str | None = None
    mcp_servers = job.get("mcp_servers") or {}
    if mcp_servers and spec.get("mcp_config_flag"):
        claude_mcp = {
            "mcpServers": {
                name: {"type": cfg.get("type", "streamable-http"), "url": cfg["url"],
                       **({"headers": cfg["headers"]} if cfg.get("headers") else {})}
                for name, cfg in mcp_servers.items() if cfg.get("url")
            }
        }
        (isolated_host_dir / "mcp.json").write_text(json.dumps(claude_mcp, indent=2))
        mcp_config_container_path = f"{isolated_container_path}/mcp.json"

    volumes: dict[str, dict] = {}

    # The Agent Config's permission dict, forwarded verbatim by
    # agents-platform's executor.py (`if provider == "runner":
    # params["permissions"] = permissions`). Each key means whatever THIS
    # side decides it means — the platform can't resolve a host path it has
    # no access to — so the mapping below is the runner's half of the
    # contract, and it must agree with executor.py's `_perm_volumes` or an
    # Agent Config behaves differently depending on which executor happens
    # to pick the run up.
    _perms: dict = job.get("permissions") or {}

    def _workspace_access() -> bool:
        """Whether this run gets the workspace tree mounted.

        Fail-CLOSED, byte-for-byte what agents-platform's executor.py does
        (``bool(permissions.get("workspace_access", False))`` driving
        CliLLM's ``mount_cwd``). An Agent Config must mean the same thing
        whichever executor happens to pick a run up; a permission that
        depends on the execution path is not a permission, it is a
        coincidence.

        This briefly shipped fail-OPEN (2026-08-13) because this path had
        ignored the permission altogether and it was unknown whether any
        config actually set the key. Checked on the live tenant the same
        day: all six agent configs carry it explicitly, and the only four
        agents with no config at all (echo-coder, fake-tool-tester,
        monitor-shell, self-openai-agent) never reach this function — none
        of them spawns a CLI container. So the compatibility default
        protected nothing and silently disagreed with the other executor,
        which is precisely the shape of bug this workspace keeps producing.

        A grant is now explicit or it does not exist.
        """
        return bool(_perms.get("workspace_access", False))

    def _mount(host_rel: str, container_path: str, ro: bool = False) -> None:
        host_path = f"{WORKSPACE_HOST_DIR.rstrip('/')}/{host_rel.lstrip('/')}"
        volumes[host_path] = {"bind": container_path, "mode": "ro" if ro else "rw"}

    def _mount_abs(host_path: str, container_path: str, ro: bool = False) -> None:
        volumes[host_path] = {"bind": container_path, "mode": "ro" if ro else "rw"}

    creds_dir = spec["creds_dir"]
    creds_file = spec.get("creds_file")

    # The spawned image runs as uid 1000; this workspace runs as uid 1001, and
    # a CLI login writes its creds 0600. Mounting $HOME/<creds_dir> straight
    # in therefore hands the container files it can neither read nor write:
    #
    #   $ podman run -v .../.codex:/home/ubuntu/.codex <img> sh -c 'id; cat ...'
    #   uid=1000(ubuntu) ...
    #   -rw------- 1 1001 1001 auth.json
    #   cat: /home/ubuntu/.codex/config.toml: Permission denied
    #
    # claude never noticed because its auth arrives as an env token and it
    # never reads those files. codex (auth_mode "chatgpt", no API key to
    # inject) must read auth.json AND write session state — so it started a
    # thread, failed to load config/auth, and exited without ever running a
    # turn, which surfaced as a green run with empty output.
    #
    # For a CLI without env-token auth, hand the container its OWN per-run
    # COPY of the creds, mode 0777/0666, instead of the live dir. Cost: a
    # token refresh inside the container does not persist (same limitation
    # the fallback branch below already documents) — the workspace's own
    # `codex login` remains the source of truth.
    creds_staged = False
    if direct_home_mount and not spec.get("env_token_auth"):
        creds_copy = isolated_host_dir / "creds"
        try:
            if creds_copy.exists():
                shutil.rmtree(creds_copy, ignore_errors=True)
            shutil.copytree(_real_home / creds_dir, creds_copy,
                            ignore=shutil.ignore_patterns("isolated", "sessions", "cache"))
            for path in [creds_copy, *creds_copy.rglob("*")]:
                path.chmod(0o777 if path.is_dir() else 0o666)
            _host_creds = str(isolated_host_dir).replace(
                str(_real_home), WORKSPACE_HOME_HOST_DIR.rstrip("/"), 1)
            # Hand the creds over at a NEUTRAL path and let the entrypoint
            # copy them into the container's own $HOME below. Two distinct
            # failures make the obvious "bind ~/.codex straight in" wrong:
            #
            #  * a whole-DIR bind leaves ~/.codex on the nested bind-mounted
            #    host tree, where codex's in-process app-server cannot create
            #    its socket / PATH aliases — EPERM at startup, run dies.
            #  * per-FILE binds fix that, but podman then auto-creates the
            #    parent /home/ubuntu/.codex as ROOT, and the container user
            #    (uid 1000) cannot mkdir thread-writer-locks/ inside it —
            #    "failed to initialize thread persistence: Permission denied".
            #
            # Copying into $HOME at startup sidesteps both: the dir ends up on
            # the container's own writable layer, owned by the run user.
            _mount_abs(f"{_host_creds}/creds", "/aw-creds", ro=True)
            # The run cwd cannot stay under <creds_dir>/isolated/ — that path
            # is now shadowed by the creds mount above and would not exist in
            # the container (podman refuses to start on a missing workdir).
            # Give it its own neutral, world-writable mount instead.
            isolated_container_path = f"/home/ubuntu/run-{run_id}"
            isolated_host_dir.chmod(0o777)
            _mount_abs(_host_creds, isolated_container_path, ro=False)
            creds_staged = True  # creds handled — skip BOTH branches below
        except Exception:
            log.exception("execute: could not stage %s creds for run=%s — "
                          "falling back to the live mount", creds_dir, run_id)

    if creds_staged:
        # Already mounted above. Falling through to either branch would mount
        # a SECOND bind on the same destination and podman rejects the whole
        # container: "fill out specgen: /home/ubuntu/.codex: duplicate mount
        # destination".
        pass
    elif direct_home_mount:
        # Mount the LIVE $HOME/.claude rw straight in, exactly like
        # agentic-workspace's docker_agent.py mounts data/home/.claude rw.
        # Same underlying file the workspace's own `claude` login writes, so a
        # token refresh the spawned CLI performs at runtime lands on the source
        # of truth and the NEXT run sees the fresh token — auth self-heals, no
        # more ~8h forced relogin. Whole dir is rw because the CLI writes both
        # refreshed tokens (.credentials.json) and session/shell-snapshot state
        # here. (The old read-only .credentials.json shadow from 3dd2c55 was
        # what blocked refresh persistence; the blank-token race it guarded
        # against only fires under concurrent refreshes — acceptable for the
        # serial run model today, revisit with a flock if agents ever run in
        # parallel.)
        _home = WORKSPACE_HOME_HOST_DIR.rstrip("/")
        _mount_abs(f"{_home}/{creds_dir}", f"/home/ubuntu/{creds_dir}", ro=False)
        if creds_file and (_real_home / creds_file).is_file():
            _mount_abs(f"{_home}/{creds_file}", f"/home/ubuntu/{creds_file}", ro=False)
        # Isolated cwd already lives inside the whole-.claude mount above — no
        # separate mount (matches docker_agent.py).
    else:
        # Fallback — $HOME has no separate host bind-mount, so its host path is
        # unknown to the podman daemon and can't be a sibling-mount source.
        # Copy creds into the host-shared workspace tree and mount that copy,
        # with .credentials.json read-only to stop a blanking write racing
        # across concurrent runs (3dd2c55). This path CANNOT persist refreshes
        # (the copy is overwritten from $HOME before each spawn) — kept only so
        # these setups still authenticate for the lifetime of a fresh token.
        if _real_home.resolve() != Path(WORKSPACE_CONTAINER_DIR).resolve():
            _sync_home_creds_into_workspace(_real_home, spec)
        if (Path(WORKSPACE_CONTAINER_DIR) / creds_dir).is_dir():
            _mount(creds_dir, f"/home/ubuntu/{creds_dir}", ro=False)
            _cred_file = f"{creds_dir}/.credentials.json"
            if (Path(WORKSPACE_CONTAINER_DIR) / _cred_file).is_file():
                _mount(_cred_file, f"/home/ubuntu/{_cred_file}", ro=True)
        if creds_file and (Path(WORKSPACE_CONTAINER_DIR) / creds_file).is_file():
            _mount(creds_file, f"/home/ubuntu/{creds_file}", ro=True)
        # Isolated run cwd (rw — the CLI writes its own session/project state here)
        _mount(isolated_rel, isolated_container_path, ro=False)

    # Agent Config's "GitHub / Git" permission (agents-platform-multitenant's
    # executor.py forwards permissions.get("github") through RunnerLLM's
    # dispatch payload — see that repo's runner.py) — mount gh's own
    # credential store + .gitconfig (mirrored into aw-app-git's data dir on
    # login, see that app's gh_auth.py) read-only, same shape the
    # pre-decoupling agents-platform host-path mount used
    # (data/home/.gitconfig + .config/gh, executor.py's
    # _perm_volumes["github"]) — just sourced from the app's own data dir
    # instead of a hand-populated shared one. Read-only: a spawned agent
    # container has no legitimate reason to write back into the credential
    # aw-app-git's own login flow owns.
    if _perms.get("github"):
        _git_config_gh = Path(WORKSPACE_CONTAINER_DIR) / GIT_CREDS_REL / "config-gh"
        _git_gitconfig = Path(WORKSPACE_CONTAINER_DIR) / GIT_CREDS_REL / "gitconfig"
        if _git_config_gh.is_dir():
            _mount(f"{GIT_CREDS_REL}/config-gh", "/home/ubuntu/.config/gh", ro=True)
        if _git_gitconfig.is_file():
            _mount(f"{GIT_CREDS_REL}/gitconfig", "/home/ubuntu/.gitconfig", ro=True)

    # Workspace root mount — found live 2026-08-02: nothing above actually
    # mounted anything AT a container path named "aw-workspace" (creds go to
    # ~/.claude, skills to /opt/agentic-workspace/skills), so `ls /opt`
    # inside a spawned container never showed the real workspace root even
    # though every functional mount (creds, sessions) was already correctly
    # scoped there. rw (Frederico, 2026-08-02): this IS the agent's real
    # working tree now, not just a visibility check — the CLI needs to
    # write here, not only read.
    #
    # Gated on the Agent Config's "AW Workspace Folder Access" permission
    # since 2026-08-13. It was unconditional before, which quietly inverted
    # the meaning of that checkbox on this execution path: an agent whose
    # config said workspace_access=false still received the entire workspace
    # tree, read-write. That was not theoretical — the crispal-* agents are
    # configured that way and their own system prompts tell them they have no
    # workspace filesystem, so they were being lied to in the safe direction
    # by the prompt and the unsafe direction by the mount. Mirrors
    # agents-platform's executor.py, where the same permission drives
    # CliLLM's mount_cwd.
    if WORKSPACE_HOST_DIR and _workspace_access():
        volumes[WORKSPACE_HOST_DIR.rstrip("/")] = {"bind": "/opt/aw-workspace", "mode": "rw"}

    # "docker" and "tmp_access" mirror executor.py's _perm_volumes entries of
    # the same name. They were dropped on this path entirely — a config could
    # tick either box and nothing happened, with no log to say so.
    if _perms.get("docker") and Path(DOCKER_SOCKET_PATH).exists():
        volumes[DOCKER_SOCKET_PATH] = {"bind": "/var/run/docker.sock", "mode": "rw"}
    if _perms.get("tmp_access") and WORKSPACE_HOST_DIR:
        # Create it OURSELVES, 0777. This bind replaces the image's own /tmp
        # (1777) with a host dir; when that dir does not exist podman creates
        # it root:root 0755, and the container — which runs as the workspace
        # uid, not root — then cannot write its own scratch:
        #   EACCES: permission denied, mkdir '/tmp/claude-1001'
        # claude dies on that before the turn starts, which lands as a green
        # run with the error as its only output. Only ever seen on the COLD
        # path: a warm container keeps the /tmp it successfully made earlier,
        # so an agent with tmp_access looks fine right up until it starts a
        # fresh session. 0777 matches the /tmp semantics being replaced.
        _mount(_prepare_tmp_mount_source(), "/tmp", ro=False)

    # Bare `aw-workspace-cli` on PATH from any cwd (Telegram request,
    # 2026-08-11): the script is already visible at /opt/aw-workspace/
    # aw-workspace-cli via the rw mount just above, but /opt/aw-workspace
    # itself isn't on this image's PATH, so it only ran as `./aw-workspace-cli`
    # from that exact cwd. Rather than overriding the container's PATH env var
    # (which would require replicating the WHOLE image-baked PATH — e.g.
    # /opt/agent-npm/bin, wherever this image's own npm-installed CLI bin
    # lives — risking silently dropping an entry another agent image needs),
    # bind the same script a second time straight into /usr/local/bin, which
    # every agent image's baked-in PATH already includes. Mirrors the
    # aw-warm-wrapper mount pattern below (WARM_WRAPPER_PATH). The script's
    # own `#!/usr/bin/env python3` shebang resolves to THIS container's native
    # python3 either way, with PYTHONPATH (set below) supplying the pure-
    # Python deps — same cross-image-safety reasoning as that PYTHONPATH
    # comment.
    # Gated with the workspace mount above, not separately: the CLI drives
    # this workspace's own API (apps, folders, remote-hosts), so handing it
    # to an agent that was denied the workspace tree would give back through
    # a command exactly what the permission just withheld.
    if WORKSPACE_HOST_DIR and _workspace_access():
        _mount("aw-workspace-cli", "/usr/local/bin/aw-workspace-cli", ro=True)

    # Legacy skills mount intentionally REMOVED 2026-08-02 (Frederico): the
    # agent's real cwd is /opt/aw-workspace now — skills for this runner
    # belong under that tree, to be built out natively later, not borrowed
    # from a one-time copy of the legacy monolith's skills/. Left
    # /opt/agentic-workspace/skills unmounted (empty) on purpose; a
    # skill-loading prompt that hardcodes that legacy path will 404 until
    # the aw-workspace-native skills location exists and this gets pointed
    # at it instead.

    argv = [spec["bin"]]
    if spec.get("subcmd"):
        argv.append(spec["subcmd"])

    # Resume flag must come before the prompt flag (claude) / as a subcommand
    # (codex) — same convention as agents-platform's docker_agent.py. Only
    # emitted when the caller actually has a prior session_id for this
    # bot/chat; a first turn has none and starts a fresh conversation.
    if session_id and cli == "claude":
        argv.extend(["--resume", session_id])
    elif session_id and cli == "codex":
        argv.extend(["resume", session_id])

    prompt = job.get("prompt") or ""
    # A CLI with no --append-system-prompt equivalent (codex) would otherwise
    # DROP the agent's persona entirely — the run still "succeeds", just as a
    # different agent than the one that was asked for. Prepend it to the
    # prompt so the instructions still arrive.
    sys_prompt = job.get("append_system_prompt")
    if sys_prompt and not spec.get("append_system_prompt_flag"):
        prompt = f"{sys_prompt}\n\n---\n\n{prompt}" if prompt else sys_prompt
    if spec.get("prompt_flag"):
        argv += [spec["prompt_flag"], prompt]
    else:
        argv.append(prompt)

    model = job.get("model")
    # A codex CLI logged in with a ChatGPT account (auth_mode "chatgpt")
    # accepts ONLY that account's default model. Any -c model= override is
    # rejected by the API and the whole turn fails:
    #   400 invalid_request_error: The 'gpt-5' model is not supported when
    #   using Codex with a ChatGPT account.
    # Verified 2026-08-13 for both "gpt-5-codex" (what the codex-runner-gpt-5
    # model row asks for) and "gpt-5"; the identical run with no override
    # answers normally. Drop the override rather than fail every run — the
    # model row is a platform-wide default that no agent author can see is
    # incompatible with how this workspace's codex happens to be logged in.
    if model and cli == "codex" and _codex_auth_mode(_real_home) == "chatgpt":
        log.info("execute: codex is logged in with a ChatGPT account — ignoring "
                 "model override %r (only the account default is supported)", model)
        model = None
    if model and spec.get("model_flag"):
        if spec["model_flag"] == "-c":
            argv += ["-c", f'model="{model}"']
        else:
            argv += [spec["model_flag"], model]

    # --allowed-tools / --disallowed-tools / --append-system-prompt are
    # CLAUDE flags. They were emitted for every CLI, and codex rejects an
    # unknown flag outright ("error: unexpected argument
    # '--append-system-prompt' found") — the process then exits having
    # published only thread.started, so the run lands as success with empty
    # output and zero tokens instead of an error. Confirmed live 2026-08-13:
    # every codex run through this Runner was silently a no-op, because an
    # agent with a system_prompt always sends append_system_prompt.
    #
    # Gate each flag on the spec declaring it, so adding a CLI is a table
    # entry rather than another `if cli == ...` here.
    def _tool_flags(values: list[str], flag: str | None) -> list[str]:
        """Render a tool list the way THIS cli wants it.

        claude takes one flag with a comma-joined list; copilot repeats the
        flag once per tool (its own --help example:
        ``--allow-tool='shell(git:*)' --deny-tool='shell(git push)'``).
        Passing claude's shape to copilot would hand it a single tool literally
        named "a,b,c" — an allow-list that silently matches nothing, which is
        the quietest possible way to get the permissions wrong.
        """
        if not values or not flag:
            return []
        if spec.get("tools_flag_style") == "repeat":
            return [arg for v in values for arg in (flag, v)]
        return [flag, ",".join(values)]

    argv += _tool_flags(job.get("allowed_tools") or [], spec.get("allowed_tools_flag"))
    argv += _tool_flags(job.get("disallowed_tools") or [], spec.get("disallowed_tools_flag"))
    if job.get("append_system_prompt") and spec.get("append_system_prompt_flag"):
        argv += [spec["append_system_prompt_flag"], job["append_system_prompt"]]
    if mcp_config_container_path:
        argv += [spec["mcp_config_flag"], mcp_config_container_path]
        if spec.get("strict_mcp_flag"):
            argv.append(spec["strict_mcp_flag"])

    # dangerous_skip_permissions: defaults to True (historic always-on
    # behavior) but must be suppressible — Agent Config "secure mode"
    # (backend/app/core/security.py on the multitenant side) sets it False
    # and adds "Bash" to disallowed_tools; the Bash half already worked
    # (forwarded via disallowed_tools above) but this flag used to be
    # hardcoded into `default_extra` regardless, silently ignoring the
    # override for every runner-provider agent.
    if job.get("dangerous_skip_permissions", True) and spec.get("skip_perms_flag"):
        argv.append(spec["skip_perms_flag"])

    argv += spec["default_extra"]
    argv += list(job.get("extra_args") or [])

    env = {}
    if job.get("notion_task_id"):
        env["NOTION_TASK_ID"] = job["notion_task_id"]
    if job.get("source_device"):
        env["AW_SOURCE_DEVICE"] = job["source_device"]
    env["AW_SESSION_ID"] = job.get("session_id") or ""
    env["AW_RUN_ID"] = run_id
    # Durable auth for claude: a long-lived OAuth token (claude setup-token)
    # injected via env. Preferred over the mounted .credentials.json because
    # env-token auth doesn't rotate every 8h and never writes/blanks the creds
    # file — so it survives update/restart and can't be invalidated by another
    # environment sharing the same account's rotating refresh token.
    if cli == "claude":
        _oat = _claude_oauth_token()
        if _oat:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = _oat
    # Must match the credential mount targets below (/home/ubuntu/...) so the
    # spawned CLI's own $HOME-relative lookups (~/.claude, ~/.config/gh, etc.)
    # resolve to them. podman DOES synthesize a passwd entry for the "user"
    # kwarg's numeric uid (home = the image's WORKDIR), but that's a
    # different, unrelated path (/opt/aw-workspace) — irrelevant here since
    # this explicit env var always wins. /home/ubuntu itself is chmod 0770 in
    # the image (agent-images/*/Dockerfile) specifically so this uid, which
    # is only a member of it via --group-add below, can still write new
    # paths under $HOME that aren't one of the pre-mounted credential dirs.
    env["HOME"] = "/home/ubuntu"

    # Share aw-workspace's own venv (Dockerfile) with this sibling container
    # via PYTHONPATH only — NEVER prepend its bin/ to PATH here. This CLI
    # image is a different base/distro than aw-workspace's own
    # (confirmed live 2026-08-05: Ubuntu 24.04 / /usr/bin/python3.12 here vs
    # python:3.12-slim / /usr/local/bin/python3.12 there), so the venv's own
    # interpreter and any compiled C-extension wheel (psycopg[binary],
    # cryptography) are not safely executable here. Pure-Python packages
    # (httpx and friends — everything aw-workspace-cli actually needs to
    # start) import fine via PYTHONPATH regardless, using THIS container's
    # own native python3. Globbed (not hardcoded) so a future Python minor
    # version bump on either side doesn't silently break this.
    _venv_site_packages = sorted(
        Path(WORKSPACE_CONTAINER_DIR, ".aw-workspace", "venv", "lib").glob(
            "python3.*/site-packages"
        )
    )
    if _venv_site_packages:
        env["PYTHONPATH"] = str(_venv_site_packages[-1])

    # See the /aw-creds staging note above: for a CLI that must read its creds
    # off disk, copy them into the container's OWN $HOME before exec'ing it.
    if creds_staged:
        # PERSISTENT per-session home, bind-mounted from the workspace tree.
        #
        # The earlier version staged into /var/tmp INSIDE the container. That
        # ran, but the container is ephemeral, so codex's rollout files died
        # with it and EVERY follow-up turn failed:
        #
        #   Error: thread/resume: thread/resume failed: no rollout found for
        #   thread id <id> (code -32600)
        #
        # i.e. codex could not resume a session it had itself just created.
        # A chat bot that cannot continue a conversation is not working, even
        # though each individual turn looked fine in isolation.
        #
        # It went to /var/tmp because a bind over ~/.codex had failed with
        # EPERM and I read that as "the nested bind filesystem cannot host the
        # app-server's socket". That was WRONG. The real cause was the same
        # one behind the tmp_access bug: podman creates a missing bind source
        # as root:root, and the run user is not root. A source this process
        # creates itself, 0777, binds and works — verified end to end,
        # including `codex exec resume` recalling the previous turn.
        #
        # ONE shared home, not one per session. Keying it by
        # `session_id or run_id` looked right and was not: the FIRST turn of a
        # conversation has no session_id yet, so its rollout landed under the
        # run id, and the follow-up — which finally HAS a session id — looked
        # in a different, empty directory and failed to resume all the same.
        # codex already separates conversations by thread id inside one home,
        # exactly like a normal ~/.codex install, so there is nothing to
        # partition here.
        import shlex
        _home_rel = _cli_home_rel(creds_dir)
        _home_abs = Path(WORKSPACE_CONTAINER_DIR) / _home_rel
        try:
            _home_abs.mkdir(parents=True, exist_ok=True)
            for _p in [_home_abs, *_home_abs.parents][:3]:
                try:
                    _p.chmod(0o777)
                except Exception:
                    pass
        except Exception:
            log.exception("execute: could not prepare codex home %s", _home_abs)
        staged_home = f"/aw-{creds_dir.lstrip('.')}-home"
        _mount(_home_rel, staged_home, ro=False)
        if spec.get("home_env"):
            env[spec["home_env"]] = staged_home
        _inner = " ".join(shlex.quote(a) for a in argv)
        # Creds are copied in only when absent, so a refreshed token written
        # by a previous turn is not clobbered by the staged copy.
        argv = ["sh", "-lc",
                f'[ -f {staged_home}/auth.json ] || cp -a /aw-creds/. {staged_home}/ 2>/dev/null; '
                f'chmod -R u+rwX {staged_home} 2>/dev/null; exec {_inner}']

    kwargs: dict[str, Any] = {
        "name": f"aw-runner-run-{run_id}",
        "command": argv,
        # Frederico, 2026-08-02: /opt/aw-workspace (rw, mounted above) is the
        # agent's real working tree now — not the per-run isolated dir under
        # ~/.claude/isolated/. That dir is still mounted (rw) so the CLI's
        # own session/project state under ~/.claude keeps working, but the
        # process's actual cwd defaults to the workspace root itself.
        "working_dir": "/opt/aw-workspace",
        "environment": env,
        "volumes": volumes,
        "detach": True,
        "remove": True,
        "privileged": False,
        "stdin_open": False,
        "tty": False,
        # The agent-CLI images run as a baked-in "ubuntu" user (uid 1000),
        # but the CLI credential files mounted above are owned by THIS
        # workspace process's own uid (os.getuid(), 1001 in the verified
        # live deploy — not guaranteed 1000 everywhere depending on how the
        # workspace container itself was built). --user here overrides the
        # image's default so the mounted (often 0600) credential files are
        # actually readable — found live 2026-08-02: without this, claude
        # exits with "Not logged in" despite creds being correctly mounted,
        # because uid 1000 (image default) can't read a 0600 file owned by
        # uid 1001 (this workspace's real uid).
        #
        # SECOND bug found 2026-08-02 (same symptom, deeper cause): even
        # with --user set to the correct uid, reads still failed with a bare
        # "Permission denied" on *every* file under the mount, regardless of
        # that file's own mode bits (644, 600, tried both) — because the
        # image bakes `/home/ubuntu` itself as `drwxr-x--- ubuntu:ubuntu`
        # (0750). A process whose uid/gid don't match "ubuntu" (1000) has NO
        # bits at all on that directory ("other" is `---`), so it can't even
        # *traverse into* /home/ubuntu to reach .claude/.credentials.json —
        # this fails before the kernel ever looks at the target file's own
        # permissions. Confirmed via `stat` returning "Permission denied" on
        # `/home/ubuntu` itself (not just the file) when run as the
        # workspace's real uid. Root (uid 0) "worked" only because root
        # bypasses DAC checks entirely, which is why every earlier
        # --privileged / --security-opt / --userns=host experiment showed no
        # effect — none of those touch a plain DAC directory-traversal
        # check. Fix: keep the real uid (so the credential FILE's own
        # owner-match still applies) but add gid 1000 ("ubuntu", the group
        # that owns /home/ubuntu) as a supplementary group — the directory's
        # group bits (`r-x`) then grant traversal. Verified live: `stat`/
        # `cat` on the mounted credentials file both succeed with
        # `--user <uid>:<gid> --group-add 1000`, and fail identically
        # without it even on a fresh tmpfs-backed (/dev/shm) source file —
        # ruling out any storage-driver/overlay explanation.
        "user": f"{os.getuid()}:{os.getgid()}",
        "group_add": [1000],
    }
    if CONTAINER_NETWORK:
        kwargs["network"] = CONTAINER_NETWORK
    # Returned separately (not embedded in `argv`/`kwargs`) so warm mode's
    # own command-building (see _build_warm_kwargs) can wire the SAME
    # resolved MCP config into its differently-shaped claude_argv, instead
    # of losing every MCP tool silently.
    return image, argv, kwargs, mcp_config_container_path


_SHARED_DIR = Path(__file__).resolve().parent.parent / "agent-images" / "shared"
WARM_WRAPPER_PATH = _SHARED_DIR / "aw-warm-wrapper"
WARM_RELAY_PATH = _SHARED_DIR / "aw-warm-relay.py"
ATTACH_HELPER_PATH = _SHARED_DIR / "aw_attach.py"


def _host_path_for(container_side_path: Path) -> str:
    """Map a path inside THIS process's own container to the corresponding
    host path the podman daemon can mount from — the same
    WORKSPACE_CONTAINER_DIR -> WORKSPACE_HOST_DIR prefix swap every other
    mount source in this module already relies on (see _mount() above)."""
    rel = os.path.relpath(str(container_side_path), WORKSPACE_CONTAINER_DIR)
    return f"{WORKSPACE_HOST_DIR.rstrip('/')}/{rel}"


def _build_warm_kwargs(job: dict, epoch_hash: str, redis_url: str) -> tuple[str, dict]:
    """(image, docker-SDK run kwargs) for a FRESH warm container — the
    ``build_kwargs`` callback ``warm_pool.get_or_create()`` calls only on a
    cold/stale session. Reuses ``_build_container_kwargs()``'s mount/env/
    credential resolution verbatim (already correct for this host's
    quirks — uid/gid, direct-home-mount, venv PYTHONPATH, etc; see that
    function's own docstring) and only overrides what warm mode needs to
    differ: long-lived (get_or_create sets detach/remove), entrypoint = the
    wrapper script instead of the CLI directly, no prompt baked into the
    command (fed later via FIFO through warm_pool.dispatch_turn), and the
    stable-name labels get_or_create() matches epochs against.

    claude_argv mirrors agents-platform's docker_agent.py
    ``build_docker_argv(warm_mode=True)`` branch — same shape (--input-format
    stream-json, --resume <session_id>, model, mcp-config, extra_args), PLUS
    allowed_tools/disallowed_tools/append_system_prompt/dangerous_skip_
    permissions — the cold path's `_build_container_kwargs` (see its own
    comment above the skip-permissions block) wires all of these into its
    `argv`, but this function built `claude_argv` from scratch and never
    picked them up. Turn 1 of a session is always cold (no session_id yet
    to key a warm container on — see `_run_job_blocking`), so the flags
    silently vanishing only showed up starting turn 2, e.g.
    --dangerously-skip-permissions missing meant Claude Code's interactive
    permission gate kicked in for real on a supposedly-unattended runner
    ("This command requires approval") — found live 2026-08-11.
    """
    agent_id = job["agent_id"]
    session_id = job["session_id"]
    spec = CLI_SPECS["claude"]

    image, _argv, kwargs, mcp_config_container_path = _build_container_kwargs(job)
    kwargs = dict(kwargs)

    volumes = dict(kwargs.get("volumes") or {})
    volumes[_host_path_for(WARM_WRAPPER_PATH)] = {"bind": "/usr/local/bin/aw-warm-wrapper", "mode": "ro"}
    volumes[_host_path_for(WARM_RELAY_PATH)] = {"bind": "/usr/local/bin/aw-warm-relay.py", "mode": "ro"}
    # Sibling module the relay imports (it adds its own dirname to sys.path),
    # so it has to land in the SAME directory as the relay, not just anywhere
    # importable. Absent → the relay logs and runs without attach rewriting.
    volumes[_host_path_for(ATTACH_HELPER_PATH)] = {"bind": "/usr/local/bin/aw_attach.py", "mode": "ro"}
    kwargs["volumes"] = volumes

    env = dict(kwargs.get("environment") or {})
    env["AW_REDIS_URL"] = redis_url
    kwargs["environment"] = env

    kwargs["entrypoint"] = ["/usr/local/bin/aw-warm-wrapper"]
    claude_argv = [spec["bin"], "--input-format", "stream-json", *spec["default_extra"]]
    claude_argv += ["--resume", session_id]
    model = job.get("model")
    if model and spec.get("model_flag"):
        claude_argv += [spec["model_flag"], model]
    if mcp_config_container_path:
        claude_argv += [spec["mcp_config_flag"], mcp_config_container_path]
        if spec.get("strict_mcp_flag"):
            claude_argv.append(spec["strict_mcp_flag"])

    allowed = job.get("allowed_tools") or []
    if allowed:
        claude_argv += ["--allowed-tools", ",".join(allowed)]
    disallowed = job.get("disallowed_tools") or []
    if disallowed:
        claude_argv += ["--disallowed-tools", ",".join(disallowed)]
    if job.get("append_system_prompt"):
        claude_argv += ["--append-system-prompt", job["append_system_prompt"]]
    # Same default-True + per-job-override contract as the cold path (see
    # _build_container_kwargs's comment on this same check).
    if job.get("dangerous_skip_permissions", True) and spec.get("skip_perms_flag"):
        claude_argv.append(spec["skip_perms_flag"])

    claude_argv += list(job.get("extra_args") or [])
    kwargs["command"] = claude_argv

    kwargs["labels"] = {
        warm_pool.WARM_LABEL: "1",
        warm_pool.AGENT_ID_LABEL: agent_id,
        warm_pool.SESSION_ID_LABEL: session_id,
        warm_pool.EPOCH_LABEL: epoch_hash,
    }
    return image, kwargs


def _dispatch_warm_turn(client, job: dict, redis_url: str) -> None:
    """RUNNER_WARM_CONTAINER=1 path: get-or-create this session's persistent
    container (spawning + pulling only on a cold/stale session) and feed the
    turn into its FIFO. Does NOT stream or wait for output — aw-warm-relay.py
    (running inside the container) publishes stdout AND the turn's "done"
    sentinel directly to ``run:{run_id}:events``, the SAME stream
    execute.py's ephemeral path publishes to, so agents-platform's existing
    consumer picks it up with zero changes and nothing else needs to happen
    in this process once the turn is fed."""
    import docker as docker_sdk

    agent_id = job["agent_id"]
    session_id = job["session_id"]
    run_id = job["run_id"]
    epoch = warm_pool.get_generation(redis_url)

    image = f"{REGISTRY}/{IMAGE_PREFIX}-claude:{DEFAULT_TAG}"
    try:
        log.info("execute: pulling %s for warm run=%s", image, run_id)
        client.images.pull(image)
    except docker_sdk.errors.APIError:
        log.warning("execute: warm image pull failed for %s (run=%s), falling back to local cache",
                   image, run_id, exc_info=True)
        client.images.get(image)

    name = warm_pool.get_or_create(
        client=client, agent_id=agent_id, session_id=session_id, epoch_hash=epoch,
        build_kwargs=lambda _name, _epoch: _build_warm_kwargs(job, _epoch, redis_url),
    )
    log.info("execute: dispatching run=%s to warm container %s", run_id, name)
    warm_pool.dispatch_turn(
        client=client, name=name, run_id=run_id, prompt=job.get("prompt") or "",
        notion_task_id=job.get("notion_task_id"), source_device=job.get("source_device"),
    )


def _run_job_blocking(job: dict, redis_url: str) -> None:
    """Blocking worker body — spawn the container, stream its stdout lines
    into the shared Redis Stream, publish the terminal 'done' sentinel.
    Runs in its own thread (started fire-and-forget by the /execute route)
    so the HTTP response returns immediately after the container is
    launched, matching RunnerLLM's "trigger then attach" contract."""
    run_id = job["run_id"]
    r = None
    try:
        r = _redis_client(redis_url)
    except Exception:
        log.exception("execute: could not build redis client for run=%s — job aborted", run_id)
        return

    # Inbound attachments ride along in the dispatch payload — write them to
    # the agent's disk and point the prompt at the real files BEFORE either
    # path below consumes job["prompt"] (cold bakes it into argv, warm feeds
    # it through the FIFO). See aw_attach.materialise_inbound.
    try:
        job["prompt"] = _attach_helper().materialise_inbound(
            job.get("prompt") or "", job.get("attachments"), run_id,
            workspace_dir=WORKSPACE_CONTAINER_DIR,
        )
    except Exception:
        log.exception("execute: inbound attachment materialise failed run=%s "
                      "(prompt left with its URLs)", run_id)

    import docker as docker_sdk

    # Warm path (ON by default since 0.32.0, switched off with the
    # warm_container config field — see warm_pool.enabled()/configure()):
    # only for claude, and only once the caller sends agent_id (the other
    # half of a warm container's stable name alongside session_id — see
    # RunnerLLM._dispatch in agents-platform-multitenant). Any job missing
    # either falls straight through to the unchanged ephemeral path below.
    if (warm_pool.enabled() and (job.get("cli") or "claude") == "claude"
            and job.get("agent_id") and job.get("session_id")):
        try:
            client = docker_sdk.DockerClient(base_url="unix://" + CONTAINER_SOCKET)
            _dispatch_warm_turn(client, job, redis_url)
        except Exception as e:
            log.exception("execute: warm dispatch failed run=%s", run_id)
            _publish_line(r, run_id, json.dumps({
                "type": "result", "subtype": "spawn_error", "is_error": True,
                "result": f"runner failed to dispatch warm turn: {e}",
            }))
            _publish_done(r, run_id, 1)
        finally:
            try:
                r.close()
            except Exception:
                pass
        return

    try:
        image, argv, kwargs, _mcp_config_container_path = _build_container_kwargs(job)
        client = docker_sdk.DockerClient(base_url="unix://" + CONTAINER_SOCKET)
        # Always attempt a pull, not just when the tag is missing locally —
        # a `:latest`-pinned image that already exists locally otherwise
        # never gets refreshed after an upstream rebuild, silently running a
        # stale agent-CLI version indefinitely (bit us once already,
        # 2026-08-02). Best-effort: fall back to whatever is cached locally
        # if the registry is unreachable rather than failing the run outright.
        try:
            log.info("execute: pulling %s for run=%s", image, run_id)
            client.images.pull(image)
        except docker_sdk.errors.APIError:
            log.warning("execute: pull failed for %s (run=%s), falling back to local cache", image, run_id, exc_info=True)
            client.images.get(image)  # raises ImageNotFound if truly absent — surfaces as spawn_error below

        log.info("execute: spawning run=%s image=%s argv=%s", run_id, image, argv)
        container = client.containers.run(image, **kwargs)
    except Exception as e:
        log.exception("execute: container spawn failed run=%s", run_id)
        _publish_line(r, run_id, json.dumps({
            "type": "result", "subtype": "spawn_error", "is_error": True,
            "result": f"runner failed to spawn container: {e}",
        }))
        _publish_done(r, run_id, 1)
        return

    returncode = 1
    # `container.logs(stream=True)` yields raw byte chunks exactly as
    # delivered by the daemon's log API — these do NOT align with the
    # underlying process's newline-terminated lines (unlike the OLD local
    # path's `asyncio.StreamReader.readline()` in agents-platform_multitenant's
    # cli.py, which buffers until a full line is available). A single
    # `claude --output-format stream-json` JSON line (e.g. a big tool_result)
    # can arrive split across two or more chunks. Publishing each raw chunk
    # as if it were a complete line ships PARTIAL JSON fragments into the
    # Redis Stream; the consumer's `json.loads` then fails on each half and
    # falls back to treating the fragment as plain assistant text — leaking
    # raw stream-json (e.g. `{"type":"user","message":{...,"tool_result"...}`)
    # straight into the delivered Telegram message. Fix: buffer chunks and
    # only publish on complete '\n'-terminated lines, carrying any trailing
    # partial segment over to the next chunk (flushed at stream end).
    buf = ""
    try:
        for raw in container.logs(stream=True, follow=True):
            buf += raw.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.strip():
                    _publish_line(r, run_id, line)
        if buf.strip():
            _publish_line(r, run_id, buf)
        try:
            status = container.wait()
            returncode = int(status.get("StatusCode", 1)) if isinstance(status, dict) else int(status)
        except Exception:
            returncode = 0
    except Exception:
        log.exception("execute: log-stream failed run=%s", run_id)
    finally:
        _publish_done(r, run_id, returncode)
        try:
            r.close()
        except Exception:
            pass


def start_job(job: dict, redis_url: str) -> None:
    """Fire-and-forget: launch `_run_job_blocking` in a background thread."""
    t = threading.Thread(target=_run_job_blocking, args=(job, redis_url),
                         name=f"runner-exec-{job.get('run_id', uuid.uuid4().hex[:8])}",
                         daemon=True)
    t.start()
