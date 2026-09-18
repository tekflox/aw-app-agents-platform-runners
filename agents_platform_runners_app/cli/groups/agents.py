"""``agents-platform agents`` — ``/api/agents`` on agents-platform-multitenant
(routes: ``backend/app/api/agents.py:411-508,642``).

``add`` deliberately does not expose a flag per ``AgentIn`` field (19 of
them, growing) — flags for the ten a human actually types by hand, plus
``--from-json`` as the escape hatch that keeps every remaining/future field
reachable with no CLI change. See ``docs/architecture/cli.md``.

``run``'s request body is **nested** — ``{"input": {"input": TEXT}}``, read
by ``run_agent_ep`` as ``body.input.get("input")`` — and ``call_me_back`` is
hardcoded ``false``: a CLI invocation has no Agents Platform session to wake,
and the endpoint 400s a ``call_me_back=true`` with no resolvable caller.
"""
from __future__ import annotations

import json
import sys
import time

from ..client import PlatformClient, PlatformError
from ..output import emit_json, emit_table, fail, ok

GROUP = "agents"
DESCRIPTION = "Manage and run Agents Platform agents"

# The real terminal set on Run.status (see agents-platform-multitenant's
# models.py: "pending|queued|running|success|error|cancelled") — NOT
# "succeeded"/"failed", which is what a plausible-sounding guess would use.
_TERMINAL_STATUSES = {"success", "error", "cancelled"}
_POLL_INTERVAL_S = 3.0
_DEFAULT_WAIT_TIMEOUT_S = 900.0
EXIT_TIMEOUT = 124


def register(sub) -> None:
    p_list = sub.add_parser("list", help="list agents")
    p_list.add_argument("--deleted", action="store_true", help="only soft-deleted agents")
    p_list.set_defaults(func=_list)

    p_add = sub.add_parser("add", help="create an agent")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--slug", default=None)
    p_add.add_argument("--description", default=None)
    prompt_group = p_add.add_mutually_exclusive_group()
    prompt_group.add_argument("--system-prompt", default=None)
    prompt_group.add_argument("--system-prompt-file", default=None,
                               help="path to read, or '-' for stdin")
    p_add.add_argument("--model", dest="model_slug", default=None)
    p_add.add_argument("--group", dest="group_slug", default=None)
    p_add.add_argument("--agent-config", dest="agent_config_slug", default=None)
    p_add.add_argument("--skill", dest="skill_slugs", action="append", default=[])
    p_add.add_argument("--icon", default=None)
    p_add.add_argument("--color", default=None)
    p_add.add_argument("--from-json", default=None,
                        help="a full/partial AgentIn JSON file (or '-' for stdin), "
                             "merged first — explicit flags above override it")
    p_add.set_defaults(func=_add)

    p_del = sub.add_parser("delete", help="delete an agent")
    p_del.add_argument("slug")
    p_del.add_argument("--hard", action="store_true", help="permanent delete (irreversible)")
    p_del.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_del.set_defaults(func=_delete)

    p_run = sub.add_parser("run", help="run an agent")
    p_run.add_argument("slug")
    p_run.add_argument("--input", required=True, help="the run's input text, or '-' for stdin")
    p_run.add_argument("--target", dest="target_slug", default=None,
                        help="delivery Target slug; omit to use the auto-provisioned 'ad-hoc' Target")
    p_run.add_argument("--session", dest="session_id", default=None,
                        help="resume a prior CLI session")
    p_run.add_argument("--notion-task", dest="notion_task_id", default=None)
    p_run.add_argument("--wait", action="store_true", help="poll until the run finishes")
    p_run.add_argument("--timeout", type=float, default=_DEFAULT_WAIT_TIMEOUT_S,
                        help=f"seconds to wait with --wait (default {int(_DEFAULT_WAIT_TIMEOUT_S)})")
    p_run.set_defaults(func=_run)


def _truncate(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 1] + "…"


def _list(client: PlatformClient, ns) -> int:
    params = {"deleted_only": "true"} if ns.deleted else None
    agents = client.get("/api/agents", params=params) or []
    if ns.as_json:
        emit_json(agents)
    else:
        headers = ("SLUG", "NAME", "MODEL", "GROUP", "DESCRIPTION")
        rows = [
            (a.get("slug", ""), a.get("name", ""), a.get("model_slug") or "",
             a.get("group_slug") or "", _truncate(a.get("description") or "", 60))
            for a in agents
        ]
        emit_table(rows, headers)
    return 0


def _read_text_or_stdin(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def _read_file_or_stdin(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _add(client: PlatformClient, ns) -> int:
    body: dict = {}
    if ns.from_json:
        body = json.loads(_read_file_or_stdin(ns.from_json))

    body["name"] = ns.name
    if ns.slug is not None:
        body["slug"] = ns.slug
    if ns.description is not None:
        body["description"] = ns.description
    if ns.system_prompt is not None:
        body["system_prompt"] = ns.system_prompt
    elif ns.system_prompt_file is not None:
        body["system_prompt"] = _read_file_or_stdin(ns.system_prompt_file)
    if ns.model_slug is not None:
        body["model_slug"] = ns.model_slug
    if ns.group_slug is not None:
        body["group_slug"] = ns.group_slug
    if ns.agent_config_slug is not None:
        body["agent_config_slug"] = ns.agent_config_slug
    if ns.skill_slugs:
        body["skill_slugs"] = ns.skill_slugs
    if ns.icon is not None:
        body["icon"] = ns.icon
    if ns.color is not None:
        body["color"] = ns.color

    agent = client.post("/api/agents", json_body=body)
    if ns.as_json:
        emit_json(agent)
    else:
        ok(f"Created agent '{agent.get('slug')}'.")
    return 0


def _delete(client: PlatformClient, ns) -> int:
    if not ns.yes:
        if not sys.stdin.isatty():
            fail(f"Refusing to delete agent '{ns.slug}' without --yes on a non-interactive stdin.")
            return 1
        answer = input(f"Delete agent '{ns.slug}'? [y/N] ").strip().lower()
        if answer != "y":
            ok("Aborted.")
            return 0
    result = client.delete(f"/api/agents/{ns.slug}", params={"hard": "true"} if ns.hard else None)
    if ns.as_json:
        emit_json(result)
    elif ns.hard:
        ok(f"Permanently deleted agent '{ns.slug}'.")
    else:
        ok(f"Soft-deleted agent '{ns.slug}' (restorable).")
    return 0


def _run(client: PlatformClient, ns) -> int:
    text = _read_text_or_stdin(ns.input)
    body: dict = {
        "input": {"input": text},
        "call_me_back": False,
    }
    if ns.target_slug:
        body["target_slug"] = ns.target_slug
    if ns.session_id:
        body["session_id"] = ns.session_id
    if ns.notion_task_id:
        body["notion_task_id"] = ns.notion_task_id

    result = client.post(f"/api/agents/{ns.slug}/run", json_body=body)
    run_id = result.get("run_id")

    if not ns.wait:
        if ns.as_json:
            emit_json(result)
        else:
            ok(f"run_id: {run_id}")
        return 0

    deadline = time.monotonic() + ns.timeout
    run = None
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}")
        if run.get("status") in _TERMINAL_STATUSES:
            break
        time.sleep(_POLL_INTERVAL_S)
    else:
        run = client.get(f"/api/runs/{run_id}")

    status = (run or {}).get("status")
    if status not in _TERMINAL_STATUSES:
        if ns.as_json:
            emit_json(run)
        else:
            fail(f"Timed out after {ns.timeout:.0f}s waiting on run '{run_id}' "
                 f"(status={status}). It may still finish — check it with:\n"
                 f"  … agents-platform runs get {run_id}")
        return EXIT_TIMEOUT

    if ns.as_json:
        emit_json(run)
    else:
        output_text = ((run or {}).get("output") or {}).get("text", "")
        print(output_text)
        if status != "success":
            fail(f"Run '{run_id}' finished with status={status}: {(run or {}).get('error') or ''}")

    return 0 if status == "success" else 1
