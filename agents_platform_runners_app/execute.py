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
        "creds_dir": ".claude", "creds_file": ".claude.json",
    },
    "codex": {
        "bin": "codex", "subcmd": "exec", "prompt_flag": None,
        "default_extra": ["--skip-git-repo-check", "--json"],
        "skip_perms_flag": "--dangerously-bypass-approvals-and-sandbox",
        "model_flag": "-c", "add_dir_flag": None,
        "mcp_config_flag": None,  # codex has no --mcp-config flag — see write-up below
        "creds_dir": ".codex", "creds_file": None,
    },
    "copilot": {
        "bin": "copilot", "subcmd": None, "prompt_flag": "-p",
        "default_extra": ["--allow-all-tools"],
        "skip_perms_flag": None,
        "model_flag": "--model", "add_dir_flag": "--add-dir",
        "mcp_config_flag": None,
        "creds_dir": ".copilot", "creds_file": None,
    },
    "cursor-agent": {
        "bin": "cursor-agent", "subcmd": None, "prompt_flag": None,
        "default_extra": ["--print"],
        "skip_perms_flag": None,
        "model_flag": "--model", "add_dir_flag": None,
        "mcp_config_flag": None,
        "creds_dir": ".cursor", "creds_file": None,
    },
}

STREAM_MAXLEN = 50_000
STREAM_TTL_S = 86400


def _stream_key(run_id: str) -> str:
    return f"run:{run_id}:events"


def _redis_client(redis_url: str):
    import redis  # sync client — this runs inside its own worker thread
    return redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=30)


def _publish_line(r, run_id: str, line: str) -> None:
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


def _build_container_kwargs(job: dict) -> tuple[str, list[str], dict]:
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
    # direct-home mode it lives UNDER the real .claude that's already mounted
    # whole (docker_agent.py does the same — no separate mount); otherwise it
    # sits in the host-shared workspace tree and is mounted explicitly below.
    isolated_rel = f".claude/isolated/{run_id}"
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

    def _mount(host_rel: str, container_path: str, ro: bool = False) -> None:
        host_path = f"{WORKSPACE_HOST_DIR.rstrip('/')}/{host_rel.lstrip('/')}"
        volumes[host_path] = {"bind": container_path, "mode": "ro" if ro else "rw"}

    def _mount_abs(host_path: str, container_path: str, ro: bool = False) -> None:
        volumes[host_path] = {"bind": container_path, "mode": "ro" if ro else "rw"}

    creds_dir = spec["creds_dir"]
    creds_file = spec.get("creds_file")

    if direct_home_mount:
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

    # Workspace root mount — found live 2026-08-02: nothing above actually
    # mounted anything AT a container path named "aw-workspace" (creds go to
    # ~/.claude, skills to /opt/agentic-workspace/skills), so `ls /opt`
    # inside a spawned container never showed the real workspace root even
    # though every functional mount (creds, sessions) was already correctly
    # scoped there. rw (Frederico, 2026-08-02): this IS the agent's real
    # working tree now, not just a visibility check — the CLI needs to
    # write here, not only read.
    if WORKSPACE_HOST_DIR:
        volumes[WORKSPACE_HOST_DIR.rstrip("/")] = {"bind": "/opt/aw-workspace", "mode": "rw"}

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
    if spec.get("prompt_flag"):
        argv += [spec["prompt_flag"], prompt]
    else:
        argv.append(prompt)

    model = job.get("model")
    if model and spec.get("model_flag"):
        if spec["model_flag"] == "-c":
            argv += ["-c", f'model="{model}"']
        else:
            argv += [spec["model_flag"], model]

    allowed = job.get("allowed_tools") or []
    if allowed:
        argv += ["--allowed-tools", ",".join(allowed)]
    disallowed = job.get("disallowed_tools") or []
    if disallowed:
        argv += ["--disallowed-tools", ",".join(disallowed)]
    if job.get("append_system_prompt"):
        argv += ["--append-system-prompt", job["append_system_prompt"]]
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
    # Running as a numeric uid that has no /etc/passwd entry inside the
    # image (see the "user" kwarg below) means the container never gets an
    # implicit $HOME — the CLI would otherwise look in / for its config.
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
    return image, argv, kwargs


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

    import docker as docker_sdk
    try:
        image, argv, kwargs = _build_container_kwargs(job)
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
