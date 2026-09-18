"""``agents-platform agents`` — list/add/delete/run, with emphasis on the
two Architect-flagged risks: ``run``'s nested body shape
(``{"input": {"input": TEXT}}``) and ``call_me_back`` hardcoded ``false``,
and polling against the REAL terminal status set
(``success``/``error``/``cancelled`` — not ``succeeded``/``failed``).
"""
from __future__ import annotations

import argparse
import json

from agents_platform_runners_app.cli.groups import agents as agents_grp


class _FakeClient:
    def __init__(self, run_statuses=None):
        self.calls = []
        self._run_statuses = list(run_statuses or [])

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path == "/api/agents":
            return [
                {"slug": "coder-sonnet", "name": "Coder", "model_slug": "claude-sonnet",
                 "group_slug": "dev-team", "description": "x" * 80},
            ]
        if path.startswith("/api/runs/"):
            status = self._run_statuses.pop(0) if self._run_statuses else "success"
            return {"id": path.rsplit("/", 1)[-1], "status": status,
                    "output": {"text": "hello"}, "error": None}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, json_body=None, params=None):
        self.calls.append(("POST", path, json_body))
        if path.endswith("/run"):
            return {"run_id": "run-1", "target_id": "target-1"}
        return {**(json_body or {}), "slug": (json_body or {}).get("slug") or "generated-slug"}

    def delete(self, path, params=None):
        self.calls.append(("DELETE", path, params))
        return {"deleted": path.rsplit("/", 1)[-1], "soft": not (params or {}).get("hard")}


def test_list_table_truncates_description(capsys):
    client = _FakeClient()
    ns = argparse.Namespace(as_json=False, deleted=False)
    rc = agents_grp._list(client, ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "coder-sonnet" in out
    assert "…" in out  # truncated description


def test_list_deleted_passes_deleted_only_param():
    client = _FakeClient()
    ns = argparse.Namespace(as_json=True, deleted=True)
    agents_grp._list(client, ns)
    assert ("GET", "/api/agents", {"deleted_only": "true"}) in client.calls


def test_add_from_json_merged_first_then_flags_override(tmp_path):
    client = _FakeClient()
    from_json_file = tmp_path / "agent.json"
    from_json_file.write_text(json.dumps({"name": "Old Name", "icon": "robot", "color": "#000"}))
    ns = argparse.Namespace(
        name="New Name", slug=None, description=None, system_prompt=None,
        system_prompt_file=None, model_slug=None, group_slug=None, agent_config_slug=None,
        skill_slugs=[], icon=None, color=None, from_json=str(from_json_file), as_json=False,
    )
    agents_grp._add(client, ns)
    body = next(c[2] for c in client.calls if c[0] == "POST")
    assert body["name"] == "New Name"  # explicit flag overrides from-json
    assert body["icon"] == "robot"     # from-json value survives when no flag given


def test_delete_soft_by_default(capsys):
    client = _FakeClient()
    ns = argparse.Namespace(slug="coder-sonnet", hard=False, yes=True, as_json=False)
    rc = agents_grp._delete(client, ns)
    assert rc == 0
    assert ("DELETE", "/api/agents/coder-sonnet", None) in client.calls
    assert "restorable" in capsys.readouterr().out


def test_delete_hard_passes_hard_true(capsys):
    client = _FakeClient()
    ns = argparse.Namespace(slug="coder-sonnet", hard=True, yes=True, as_json=False)
    agents_grp._delete(client, ns)
    assert ("DELETE", "/api/agents/coder-sonnet", {"hard": "true"}) in client.calls


def test_run_body_is_nested_and_call_me_back_hardcoded_false():
    client = _FakeClient()
    ns = argparse.Namespace(
        slug="coder-sonnet", input="reply with OK", target_slug=None, session_id=None,
        notion_task_id=None, wait=False, timeout=900.0, as_json=False,
    )
    agents_grp._run(client, ns)
    body = next(c[2] for c in client.calls if c[0] == "POST")
    assert body["input"] == {"input": "reply with OK"}
    assert body["call_me_back"] is False


def test_run_without_wait_prints_run_id_and_exits_0(capsys):
    client = _FakeClient()
    ns = argparse.Namespace(
        slug="coder-sonnet", input="hi", target_slug=None, session_id=None,
        notion_task_id=None, wait=False, timeout=900.0, as_json=False,
    )
    rc = agents_grp._run(client, ns)
    assert rc == 0
    assert "run-1" in capsys.readouterr().out
    assert not any(c[0] == "GET" and c[1].startswith("/api/runs/") for c in client.calls)


def test_run_with_wait_polls_until_success(monkeypatch, capsys):
    client = _FakeClient(run_statuses=["running", "running", "success"])
    ns = argparse.Namespace(
        slug="coder-sonnet", input="hi", target_slug=None, session_id=None,
        notion_task_id=None, wait=True, timeout=900.0, as_json=False,
    )
    monkeypatch.setattr(agents_grp.time, "sleep", lambda s: None)
    rc = agents_grp._run(client, ns)
    assert rc == 0
    assert "hello" in capsys.readouterr().out


def test_run_with_wait_exits_1_on_failed_run(capsys):
    client = _FakeClient(run_statuses=["error"])
    ns = argparse.Namespace(
        slug="coder-sonnet", input="hi", target_slug=None, session_id=None,
        notion_task_id=None, wait=True, timeout=900.0, as_json=False,
    )
    rc = agents_grp._run(client, ns)
    assert rc == 1


def test_run_with_wait_times_out_with_124(monkeypatch, capsys):
    """Run never leaves 'running' — a fake, deterministically-advancing clock
    ensures the deadline is crossed on the first check regardless of real
    wall-clock speed."""
    client = _FakeClient(run_statuses=["running"] * 100)
    ns = argparse.Namespace(
        slug="coder-sonnet", input="hi", target_slug=None, session_id=None,
        notion_task_id=None, wait=True, timeout=1.0, as_json=False,
    )
    monkeypatch.setattr(agents_grp.time, "sleep", lambda s: None)
    clock = iter([0.0, 2.0, 2.0, 2.0])  # first check inside the loop already past the deadline
    monkeypatch.setattr(agents_grp.time, "monotonic", lambda: next(clock))
    rc = agents_grp._run(client, ns)
    assert rc == agents_grp.EXIT_TIMEOUT
    err = capsys.readouterr().err
    assert "Timed out" in err
