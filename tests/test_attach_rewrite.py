"""``[[ATTACH]]`` rewriting — the runner half of cross-host attachments.

An agent writes ``[[ATTACH: /opt/aw-workspace/.tmp/chart.png]]``. The Telegram
connector in agents-platform_multitenant then does ``os.path.exists(path)``
against ITS OWN filesystem — a different machine — so the block was dropped
with nothing but a log line and the user saw neither file nor error.

``agent-images/shared/aw_attach.py`` fixes that on this side, where the bytes
are still readable: upload to the run, rewrite the marker to ``artefact://``.
These tests cover the wiring and the do-no-harm guarantees; the upload itself
is exercised against a live AP separately (it needs a real run to attach to).

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
