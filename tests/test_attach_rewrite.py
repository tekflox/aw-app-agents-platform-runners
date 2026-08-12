"""Attachments across the runner/connector host boundary — both directions.

The agent CLI runs in a nested podman container on aw-remote-host; the
Telegram connector in agents-platform_multitenant runs under docker on the
outer bare-metal host. Neither can open a path the other produced.

* **Outbound** — an agent writes ``[[ATTACH: /opt/aw-workspace/.tmp/x.png]]``
  and ``_deliver_reply``'s ``os.path.exists()`` misses, dropping the block
  with nothing but a log line. Fixed on this side, where the bytes are still
  readable: upload to the run, rewrite to ``artefact://``.
* **Inbound** — a file the user attaches arrives as a gallery URL the agent
  has to download before it can look at it. AP now sends the bytes with the
  job and this side writes them to the agent's own disk, rewriting the URL in
  the prompt to that path.

Both live in ``agent-images/shared/aw_attach.py``. These tests cover the
wiring and the do-no-harm guarantees; the outbound upload itself is exercised
against a live AP separately (it needs a real run to attach to).

Run: .venv/aw/bin/python -m pytest tests/test_attach_rewrite.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

BASE_JOB = {
    "run_id": "r1",
    "cli": "claude",
    "agent_id": "agent-1",
    "session_id": "session-1",
    "prompt": "hi",
}


def _warm_volumes(tmp_path, monkeypatch) -> dict:
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    _image, kwargs = execute_mod._build_warm_kwargs(dict(BASE_JOB), "epoch1",
                                                    "redis://example.test:6379/0")
    return kwargs["volumes"]


def test_helper_is_mounted_beside_the_relay_that_imports_it(tmp_path, monkeypatch):
    # The relay puts its own dirname on sys.path, so the helper has to land in
    # that same directory — mounting it anywhere else silently disables the
    # rewrite (the relay logs and carries on).
    binds = {v["bind"] for v in _warm_volumes(tmp_path, monkeypatch).values()}
    assert "/usr/local/bin/aw_attach.py" in binds
    assert "/usr/local/bin/aw-warm-relay.py" in binds


def test_helper_is_mounted_read_only(tmp_path, monkeypatch):
    modes = {v["bind"]: v["mode"] for v in _warm_volumes(tmp_path, monkeypatch).values()}
    assert modes["/usr/local/bin/aw_attach.py"] == "ro"


def test_helper_file_actually_exists_where_the_mount_points(tmp_path, monkeypatch):
    # A mount source that doesn't exist would be created as an empty DIRECTORY
    # by the container engine, and the relay's import would fail at runtime
    # rather than here.
    assert execute_mod.ATTACH_HELPER_PATH.is_file()


def test_execute_loads_the_same_helper_the_relay_uses():
    mod = execute_mod._attach_helper()
    assert hasattr(mod, "rewrite_stream_line")
    assert Path(mod.__file__).resolve() == execute_mod.ATTACH_HELPER_PATH.resolve()


class TestRewriteGuards:
    """Everything that must pass through byte-identical. A rewrite that
    corrupts the stream is far worse than one that doesn't happen: the
    consumer json.loads() every line and falls back to treating a broken one
    as raw assistant text, which leaks stream-json into the user's chat."""

    def setup_method(self):
        self.mod = execute_mod._attach_helper()

    def test_non_json_line_untouched(self):
        line = "not json at all [[ATTACH: /tmp/x.png]]"
        assert self.mod.rewrite_stream_line(line, "run-1") == line

    def test_line_without_marker_untouched(self):
        line = json.dumps({"type": "result", "result": "no attachments here"})
        assert self.mod.rewrite_stream_line(line, "run-1") == line

    def test_unrelated_event_types_untouched(self):
        line = json.dumps({"type": "user", "message": {"content": "[[ATTACH: /tmp/x.png]]"}})
        assert self.mod.rewrite_stream_line(line, "run-1") == line

    def test_empty_line_untouched(self):
        assert self.mod.rewrite_stream_line("", "run-1") == ""

    def test_missing_run_id_is_never_guessed(self, tmp_path):
        f = tmp_path / "real.png"
        f.write_bytes(b"\x89PNG")
        text = f"[[ATTACH: {f}]]"
        assert self.mod.rewrite_text(text, "") == text

    def test_nonexistent_path_left_for_the_agent_to_see(self):
        text = "[[ATTACH: /definitely/not/here.png]]"
        assert self.mod.rewrite_text(text, "run-1") == text

    def test_relative_path_left_alone(self):
        text = "[[ATTACH: relative/chart.png]]"
        assert self.mod.rewrite_text(text, "run-1") == text

    def test_already_rewritten_marker_is_not_rewritten_again(self):
        text = "[[ATTACH: artefact://run-1/tgattach-abc-x.png]]"
        assert self.mod.rewrite_text(text, "run-1") == text

    def test_url_marker_left_alone(self):
        text = '[[ATTACH: https://example.test/x.png caption="c"]]'
        assert self.mod.rewrite_text(text, "run-1") == text

    def test_empty_file_is_not_uploaded(self, tmp_path):
        f = tmp_path / "empty.png"
        f.write_bytes(b"")
        text = f"[[ATTACH: {f}]]"
        assert self.mod.rewrite_text(text, "run-1") == text

    def test_oversized_file_is_not_uploaded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self.mod, "MAX_ATTACH_BYTES", 4)
        f = tmp_path / "big.png"
        f.write_bytes(b"12345")
        text = f"[[ATTACH: {f}]]"
        assert self.mod.rewrite_text(text, "run-1") == text

    def test_upload_failure_leaves_the_marker_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self.mod, "_upload", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr(self.mod, "_uploaded", {})
        f = tmp_path / "chart.png"
        f.write_bytes(b"\x89PNG data")
        text = f"[[ATTACH: {f}]]"
        assert self.mod.rewrite_text(text, "run-1") == text


class TestRewriteSuccess:
    def setup_method(self):
        self.mod = execute_mod._attach_helper()

    def _stub_upload(self, monkeypatch, calls):
        monkeypatch.setattr(self.mod, "_uploaded", {})

        def _fake(run_id, path, data):
            calls.append((run_id, path, len(data)))
            return self.mod._artefact_name(path, data)

        monkeypatch.setattr(self.mod, "_upload", _fake)

    def test_marker_is_rewritten_and_caption_preserved(self, tmp_path, monkeypatch):
        calls = []
        self._stub_upload(monkeypatch, calls)
        f = tmp_path / "chart.png"
        f.write_bytes(b"\x89PNG data")
        out = self.mod.rewrite_text(f'[[ATTACH: {f} caption="Q4 revenue"]]', "run-9")
        assert out.startswith("[[ATTACH: artefact://run-9/tgattach-")
        assert out.endswith('chart.png caption="Q4 revenue"]]')
        assert len(calls) == 1

    def test_extension_survives_so_the_photo_branch_still_fires(self, tmp_path, monkeypatch):
        self._stub_upload(monkeypatch, [])
        f = tmp_path / "shot.png"
        f.write_bytes(b"x")
        out = self.mod.rewrite_text(f"[[ATTACH: {f}]]", "run-9")
        assert out.rstrip("]").endswith(".png")

    def test_same_file_uploaded_once_per_run(self, tmp_path, monkeypatch):
        calls = []
        self._stub_upload(monkeypatch, calls)
        f = tmp_path / "chart.png"
        f.write_bytes(b"data")
        text = f"[[ATTACH: {f}]]"
        # Once in an assistant message, once in the terminal result event.
        self.mod.rewrite_stream_line(json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}), "run-9")
        self.mod.rewrite_stream_line(json.dumps({"type": "result", "result": text}), "run-9")
        assert len(calls) == 1

    def test_two_different_files_get_distinct_names(self, tmp_path, monkeypatch):
        self._stub_upload(monkeypatch, [])
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        a.write_bytes(b"aaa")
        b.write_bytes(b"bbb")
        out = self.mod.rewrite_text(f"[[ATTACH: {a}]] and [[ATTACH: {b}]]", "run-9")
        refs = [seg.split("]]")[0] for seg in out.split("artefact://")[1:]]
        assert len(set(refs)) == 2

    def test_result_event_keeps_its_other_fields(self, tmp_path, monkeypatch):
        self._stub_upload(monkeypatch, [])
        f = tmp_path / "chart.png"
        f.write_bytes(b"x")
        line = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                           "result": f"see this [[ATTACH: {f}]]"})
        evt = json.loads(self.mod.rewrite_stream_line(line, "run-9"))
        assert evt["subtype"] == "success" and evt["is_error"] is False
        assert "artefact://run-9/" in evt["result"]


class TestMaterialiseInbound:
    """The other direction: AP ships the user's attachment inline with the
    job, the runner writes it to the agent's disk and points the prompt at
    the real file instead of a URL the agent would have to download."""

    def setup_method(self):
        self.mod = execute_mod._attach_helper()

    def _att(self, name="photo.jpg", data=b"\xff\xd8\xffbytes",
             url="https://ap.test/api/gallery/direct/tok123"):
        import base64
        return {"placeholder": url, "name": name, "mime": "image/jpeg",
                "content": base64.b64encode(data).decode()}

    def test_file_lands_on_disk_and_prompt_points_at_it(self, tmp_path):
        att = self._att()
        prompt = f"[UPLOAD] Image available at: {att['placeholder']}\n\nwhat is this?"
        out = self.mod.materialise_inbound(prompt, [att], "run-7",
                                           workspace_dir=str(tmp_path),
                                           agent_bind=str(tmp_path))
        assert att["placeholder"] not in out
        path = out.split("at: ")[1].split("\n")[0]
        assert Path(path).is_file()
        assert Path(path).read_bytes() == b"\xff\xd8\xffbytes"
        assert "what is this?" in out

    def test_path_handed_over_is_the_agents_view_not_the_runners(self, tmp_path):
        # The runner writes through its own mount; the agent sees the same
        # tree at a different root. The prompt must carry the AGENT's path.
        att = self._att()
        out = self.mod.materialise_inbound(att["placeholder"], [att], "run-7",
                                           workspace_dir=str(tmp_path),
                                           agent_bind="/opt/aw-workspace")
        assert out.startswith("/opt/aw-workspace/.tmp/agent-inbound/run-7/")
        assert str(tmp_path) not in out
        # ...and the bytes really are on disk under the runner's own root.
        written = Path(str(tmp_path)) / ".tmp" / "agent-inbound" / "run-7" / "photo.jpg"
        assert written.is_file()

    def test_runs_are_isolated_from_each_other(self, tmp_path):
        a = self._att(name="x.png", data=b"aaa", url="https://ap.test/api/gallery/direct/t1")
        b = self._att(name="x.png", data=b"bbb", url="https://ap.test/api/gallery/direct/t2")
        pa = self.mod.materialise_inbound(a["placeholder"], [a], "run-A",
                                          workspace_dir=str(tmp_path), agent_bind=str(tmp_path))
        pb = self.mod.materialise_inbound(b["placeholder"], [b], "run-B",
                                          workspace_dir=str(tmp_path), agent_bind=str(tmp_path))
        assert pa != pb
        assert Path(pa).read_bytes() == b"aaa"
        assert Path(pb).read_bytes() == b"bbb"

    def test_multiple_attachments_each_get_their_own_placeholder_swapped(self, tmp_path):
        a = self._att(name="a.png", data=b"aaa", url="https://ap.test/api/gallery/direct/t1")
        b = self._att(name="b.png", data=b"bbb", url="https://ap.test/api/gallery/direct/t2")
        prompt = f"first {a['placeholder']} then {b['placeholder']}"
        out = self.mod.materialise_inbound(prompt, [a, b], "run-7",
                                           workspace_dir=str(tmp_path), agent_bind=str(tmp_path))
        assert a["placeholder"] not in out and b["placeholder"] not in out
        assert out.count(".tmp/agent-inbound/run-7/") == 2

    def test_filename_is_sanitised(self, tmp_path):
        att = self._att(name="../../etc/passwd")
        out = self.mod.materialise_inbound(att["placeholder"], [att], "run-7",
                                           workspace_dir=str(tmp_path), agent_bind=str(tmp_path))
        assert ".." not in out
        assert Path(out).parent.name == "run-7"

    def test_no_attachments_is_a_noop(self, tmp_path):
        prompt = "just text"
        assert self.mod.materialise_inbound(prompt, None, "run-7",
                                            workspace_dir=str(tmp_path)) == prompt
        assert self.mod.materialise_inbound(prompt, [], "run-7",
                                            workspace_dir=str(tmp_path)) == prompt

    def test_placeholder_absent_from_prompt_writes_nothing(self, tmp_path):
        att = self._att()
        out = self.mod.materialise_inbound("unrelated prompt", [att], "run-7",
                                           workspace_dir=str(tmp_path), agent_bind=str(tmp_path))
        assert out == "unrelated prompt"

    def test_corrupt_payload_leaves_the_url_in_place(self, tmp_path):
        att = self._att()
        att["content"] = "!!!not base64!!!"
        prompt = f"see {att['placeholder']}"
        out = self.mod.materialise_inbound(prompt, [att], "run-7",
                                           workspace_dir=str(tmp_path), agent_bind=str(tmp_path))
        assert out == prompt

    def test_old_run_dirs_are_pruned(self, tmp_path):
        import os
        import time
        root = tmp_path / ".tmp" / "agent-inbound"
        stale = root / "run-old"
        stale.mkdir(parents=True)
        (stale / "x.png").write_bytes(b"x")
        old = time.time() - self.mod.INBOUND_RETENTION_S - 60
        os.utime(stale, (old, old))
        att = self._att()
        self.mod.materialise_inbound(att["placeholder"], [att], "run-new",
                                     workspace_dir=str(tmp_path), agent_bind=str(tmp_path))
        assert not stale.exists()
        assert (root / "run-new").is_dir()


class TestInboundWiring:
    def test_execute_route_forwards_attachments(self, monkeypatch):
        from fastapi.testclient import TestClient

        from agents_platform_runners_app.routes import build_routes

        captured = {}
        monkeypatch.setattr(execute_mod, "start_job", lambda job, url: captured.update(job))
        monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "unix:///fake.sock")
        client = TestClient(build_routes({
            "execute_secret": "s3cr3t",
            "shared_redis_url": "redis://example.test:6379/0",
        }))
        atts = [{"placeholder": "https://ap.test/api/gallery/direct/t1",
                 "name": "a.png", "mime": "image/png", "content": "eA=="}]
        resp = client.post("/execute", headers={"x-runner-secret": "s3cr3t"},
                           json={"prompt": "hi", "attachments": atts})
        assert resp.status_code == 200
        assert captured.get("attachments") == atts

    def test_prompt_is_rewritten_before_either_spawn_path_reads_it(self, tmp_path, monkeypatch):
        """_run_job_blocking must rewrite job["prompt"] before the warm/cold
        branch — the cold path bakes the prompt into argv and the warm path
        feeds it through the FIFO, so a later rewrite would miss both."""
        import base64
        import sys
        import types

        # _run_job_blocking imports the docker SDK to spawn the container.
        # This test never gets that far — it stops the function right after
        # the rewrite — but the import itself would still fail wherever the
        # SDK isn't installed, e.g. this repo's release CI. Stub it so the
        # test measures the rewrite's position, not the environment.
        monkeypatch.setitem(sys.modules, "docker", types.ModuleType("docker"))
        monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path))
        monkeypatch.setattr(execute_mod, "_redis_client", lambda url: None)
        monkeypatch.setattr(execute_mod, "_publish_line", lambda *a, **k: None)
        monkeypatch.setattr(execute_mod, "_publish_done", lambda *a, **k: None)

        seen = {}

        def _boom(*a, **k):
            raise RuntimeError("stop after the rewrite")

        monkeypatch.setattr(execute_mod.warm_pool, "enabled", lambda: False)
        monkeypatch.setattr(execute_mod, "_build_container_kwargs", _boom)

        url = "https://ap.test/api/gallery/direct/t1"
        job = {"run_id": "run-5", "prompt": f"look at {url}", "cli": "claude",
               "attachments": [{"placeholder": url, "name": "p.png", "mime": "image/png",
                                "content": base64.b64encode(b"png").decode()}]}
        execute_mod._run_job_blocking(job, "redis://example.test:6379/0")
        seen["prompt"] = job["prompt"]
        assert url not in seen["prompt"]
        assert "/.tmp/agent-inbound/run-5/p.png" in seen["prompt"]
