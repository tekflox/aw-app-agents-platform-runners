"""Unit tests for the decentralized skills-index sync client (ADR 2026-08-06).

Covers slug discovery, the shared hash algorithm, and the full/delta/unchanged
+ 409-reset behaviour of SkillsSyncClient against a stubbed HTTP layer — no
real network, no agents-platform-multitenant instance needed.
"""
from __future__ import annotations

import hashlib

import pytest

from agents_platform_runners_app import skills_sync as ss


# --- discovery + hashing ---------------------------------------------------

def _make_skill(root, name, *, with_md=True):
    d = root / name
    d.mkdir(parents=True)
    if with_md:
        (d / "SKILL.md").write_text("---\nname: x\n---\n")
    return d


def test_list_skill_slugs_only_dirs_with_skill_md(tmp_path):
    _make_skill(tmp_path, "aw-alpha")
    _make_skill(tmp_path, "aw-beta")
    _make_skill(tmp_path, "not-a-skill", with_md=False)  # no SKILL.md → ignored
    (tmp_path / "loose.txt").write_text("x")             # a file → ignored
    assert ss.list_skill_slugs(str(tmp_path)) == ["aw-alpha", "aw-beta"]


def test_list_skill_slugs_missing_dir_is_empty(tmp_path):
    assert ss.list_skill_slugs(str(tmp_path / "nope")) == []


def test_compute_state_hash_is_order_and_dup_invariant():
    a = ss.compute_state_hash(["b", "a", "a"])
    b = ss.compute_state_hash(["a", "b"])
    assert a == b
    # byte-identical to the server's algorithm
    expected = hashlib.sha256("a\nb".encode()).hexdigest()
    assert a == expected


def test_empty_hash_is_stable():
    assert ss.compute_state_hash([]) == hashlib.sha256(b"").hexdigest()


# --- client behaviour ------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    c = ss.SkillsSyncClient(
        base="http://ap-mt.test", token="tok", workspace="aw",
        data_dir=str(data_dir), skills_root=str(skills_root),
    )
    calls = []

    def _fake_post(payload):
        calls.append(payload)
        return _FakeResp(200, {"synced": True, "state_hash": payload.get("state_hash")})

    monkeypatch.setattr(c, "_post", _fake_post)
    c._calls = calls  # type: ignore[attr-defined]
    c._skills_root_path = skills_root  # type: ignore[attr-defined]
    return c


def test_first_run_is_full_sync(client):
    _make_skill(client._skills_root_path, "aw-one")
    result = client.sync_incremental()
    assert result["mode"] == "full"
    assert client._calls[-1]["mode"] == "full"
    assert client._calls[-1]["skills"] == ["aw-one"]


def test_unchanged_is_noop(client):
    _make_skill(client._skills_root_path, "aw-one")
    client.sync_incremental()  # full
    n = len(client._calls)
    result = client.sync_incremental()  # no change
    assert result["status"] == "unchanged"
    assert len(client._calls) == n  # no extra POST


def test_change_after_ack_is_delta(client):
    _make_skill(client._skills_root_path, "aw-one")
    client.sync_incremental()  # full, acks {aw-one}
    _make_skill(client._skills_root_path, "aw-two")
    result = client.sync_incremental()
    assert result["mode"] == "delta"
    assert result["added"] == ["aw-two"]
    assert result["removed"] == []
    assert client._calls[-1]["mode"] == "delta"
    assert client._calls[-1]["prev_hash"] == ss.compute_state_hash(["aw-one"])


def test_removal_is_delta_removed(client):
    _make_skill(client._skills_root_path, "aw-one")
    _make_skill(client._skills_root_path, "aw-two")
    client.sync_incremental()  # full
    (client._skills_root_path / "aw-two" / "SKILL.md").unlink()  # aw-two disappears
    result = client.sync_incremental()
    assert result["mode"] == "delta"
    assert result["removed"] == ["aw-two"]


def test_409_clears_ack_and_forces_full_next(client, monkeypatch):
    _make_skill(client._skills_root_path, "aw-one")
    client.sync_incremental()  # full, acked
    _make_skill(client._skills_root_path, "aw-two")

    def _post_409(payload):
        client._calls.append(payload)
        return _FakeResp(409, {"error": "hash_mismatch"})

    monkeypatch.setattr(client, "_post", _post_409)
    r1 = client.sync_incremental()  # delta → 409
    assert r1["status"] == "hash_mismatch"
    assert r1["forced_full"] is True

    # ack cleared → next cycle is a full sync again
    def _post_ok(payload):
        client._calls.append(payload)
        return _FakeResp(200, {"synced": True})

    monkeypatch.setattr(client, "_post", _post_ok)
    r2 = client.sync_incremental()
    assert r2["mode"] == "full"
    assert set(client._calls[-1]["skills"]) == {"aw-one", "aw-two"}


def test_sync_full_always_posts_full(client):
    _make_skill(client._skills_root_path, "aw-one")
    client.sync_incremental()  # full + ack
    n = len(client._calls)
    result = client.sync_full()  # reconcile — unconditional
    assert result["mode"] == "full"
    assert len(client._calls) == n + 1
    assert client._calls[-1]["mode"] == "full"
