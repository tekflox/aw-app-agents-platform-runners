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
        "default_extra": ["--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose"],
        "model_flag": "--model", "add_dir_flag": "--add-dir",
        "creds_dir": ".claude", "creds_file": ".claude.json",
    },
    "codex": {
        "bin": "codex", "subcmd": "exec", "prompt_flag": None,
        "default_extra": ["--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "--json"],
        "model_flag": "-c", "add_dir_flag": None,
        "creds_dir": ".codex", "creds_file": None,
    },
    "copilot": {
        "bin": "copilot", "subcmd": None, "prompt_flag": "-p",
        "default_extra": ["--allow-all-tools"],
        "model_flag": "--model", "add_dir_flag": "--add-dir",
        "creds_dir": ".copilot", "creds_file": None,
    },
    "cursor-agent": {
        "bin": "cursor-agent", "subcmd": None, "prompt_flag": None,
        "default_extra": ["--print"],
        "model_flag": "--model", "add_dir_flag": None,
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


def _build_container_kwargs(job: dict) -> tuple[str, list[str], dict]:
    """Return (image, command_argv, docker-SDK run kwargs) for this job.

    Mirrors agents-platform's ``build_docker_argv`` narrowed to what a single
    stateless job needs — no warm-pool, no WS/legacy modes (this app always
    speaks the Redis-publish path itself), no resume/session persistence yet
    (v1 — a real limitation, not an oversight: each ``/execute`` call is a
    fresh isolated run dir; multi-turn sessions against a Runner-backed model
    are a follow-up).
    """
    cli = job.get("cli") or "claude"
    if cli not in CLI_SPECS:
        cli = "claude"
    spec = CLI_SPECS[cli]
    image = f"{REGISTRY}/{IMAGE_PREFIX}-{cli}:{DEFAULT_TAG}"

    run_id = job["run_id"]
    # Isolated per-run project dir — created via THIS process's own view of
    # the same directory tree the podman host mounts from
    # (WORKSPACE_CONTAINER_DIR == WORKSPACE_HOST_DIR on disk).
    isolated_rel = f".claude/isolated/{run_id}"
    (Path(WORKSPACE_CONTAINER_DIR) / isolated_rel).mkdir(parents=True, exist_ok=True)
    isolated_container_path = f"/home/ubuntu/{isolated_rel}"

    volumes: dict[str, dict] = {}

    def _mount(host_rel: str, container_path: str, ro: bool = False) -> None:
        host_path = f"{WORKSPACE_HOST_DIR.rstrip('/')}/{host_rel.lstrip('/')}"
        volumes[host_path] = {"bind": container_path, "mode": "ro" if ro else "rw"}

    # CLI credentials — this workspace's own $HOME/.claude (etc.), installed
    # by the code-agent-clis app's login flow, NOT the legacy monolith's
    # data/home/ tree. This is the entire point of the fix: creds + cwd are
    # scoped to /opt/aw-workspace, never /opt/agentic-workspace.
    #
    # rw, not ro (found live 2026-08-02, same day as the /home/ubuntu DAC
    # traversal fix above): once auth actually succeeded, the CLI still
    # failed with a read-only-filesystem error creating a session
    # directory — claude writes session/shell-snapshot state under
    # ~/.claude/ at runtime (not just reads credentials from it), so
    # mounting the whole dir ro broke every real run past the login check.
    # rw here matches what a real interactive claude session on this host
    # already has.
    creds_dir = spec["creds_dir"]
    if (Path(WORKSPACE_CONTAINER_DIR) / creds_dir).is_dir():
        _mount(creds_dir, f"/home/ubuntu/{creds_dir}", ro=False)
    creds_file = spec.get("creds_file")
    if creds_file and (Path(WORKSPACE_CONTAINER_DIR) / creds_file).is_file():
        _mount(creds_file, f"/home/ubuntu/{creds_file}", ro=False)

    # Isolated run cwd (rw — the CLI writes its own session/project state here)
    _mount(isolated_rel, isolated_container_path, ro=False)

    # Visible proof-of-scoping mount — found live 2026-08-02: nothing above
    # actually mounts anything AT a container path named "aw-workspace" (creds
    # go to ~/.claude, skills to /opt/agentic-workspace/skills), so `ls /opt`
    # inside a spawned container never showed the real workspace root even
    # though every functional mount (creds, sessions) was already correctly
    # scoped there — Frederico's own sanity check ("ls /opt should show
    # aw-workspace") was a fair ask this didn't satisfy. Mount the workspace
    # root itself (ro) at the exact path its name promises, purely so this is
    # directly verifiable, not just true-but-invisible.
    if WORKSPACE_HOST_DIR:
        volumes[WORKSPACE_HOST_DIR.rstrip("/")] = {"bind": "/opt/aw-workspace", "mode": "ro"}

    # Legacy skills library (docs/knowledge_base/skills/*, ~1.5MB) — found live
    # 2026-08-02: the aw-agent-telegram bootstrap system prompt (and others)
    # hardcode `cat /opt/agentic-workspace/skills/<name>/SKILL.md`, a path
    # that only exists in the legacy monolith's tree, never mounted here on
    # purpose (the whole point of this app is NOT mounting /opt/agentic-
    # workspace). Claude correctly refused to proceed when that path 404'd,
    # reading it as a prompt-injection mismatch rather than executing blind.
    # Fix: a one-time copy of skills/ lives on this workspace's own host
    # tree (shared/agentic-workspace-skills/, kept in sync manually for
    # now — see the KB note from today) and gets bind-mounted at the exact
    # absolute path every existing skill-loading prompt already expects, ro
    # (this app never needs to write to it).
    _skills_host_rel = "shared/agentic-workspace-skills"
    if (Path(WORKSPACE_CONTAINER_DIR) / _skills_host_rel).is_dir():
        _mount(_skills_host_rel, "/opt/agentic-workspace/skills", ro=True)

    argv = [spec["bin"]]
    if spec.get("subcmd"):
        argv.append(spec["subcmd"])

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

    kwargs: dict[str, Any] = {
        "name": f"aw-runner-run-{run_id}",
        "command": argv,
        "working_dir": isolated_container_path,
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
    try:
        for raw in container.logs(stream=True, follow=True):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            for sub in line.splitlines():
                if sub.strip():
                    _publish_line(r, run_id, sub)
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
