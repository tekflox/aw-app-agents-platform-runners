"""Decentralized skills index — the aw-workspace side of the
``workspace -> [skill names]`` sync (Frederico decision 2026-08-06, ADR
memory/agents-platform-multitenant-skills-index-decentralized-20260806).

Skill CONTENT never leaves this workspace. This module only lists the NAMES
of the skills present in the workspace's ``skills/`` directory and ships that
list to agents-platform-multitenant's ``POST /api/runners/skills/sync`` so the
platform/UI can show what each workspace has. It runs as two in-process
watchdog tasks registered by ``plugin.py`` (see there):

* delta cadence (every ~3 min): only POSTs when the local set changed since the
  last ack; a first run with no ack does a full sync (== boot full sync).
* reconcile cadence (every ~6 min): an unconditional full sync so the index
  can't silently drift.

The acked state (hash + slug list) is persisted under the app's own data dir so
a restart resumes deltas instead of always full-syncing.

Kept deliberately free of any ``src.apps`` import so the app stays runnable in
standalone mode — the skills dir and data dir are resolved from the same env
vars aw-workspace's ``src/apps/paths.py`` uses, with the same defaults.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger("aw_apps.agents_platform_runners.skills_sync")

APP_ID = "agents-platform-runners"

# Mirrors src/apps/paths.py in aw-workspace.
DEFAULT_WORKSPACE_CONTAINER_DIR = "/opt/aw-workspace"


def skills_dir() -> str:
    """``<workspace root>/skills`` — same resolution as aw-workspace's
    ``paths.skills_dir()`` (env override + default), without importing it."""
    root = os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    )
    return os.path.join(root, "skills")


def _data_dir() -> str:
    """The app's own data dir — ``<AW_WORKSPACE_HOME>/data/<app-id>`` (matches
    what aw-workspace's runtime binds for ``fs:workspace-data``)."""
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(
        os.path.expanduser("~"), ".aw-workspace"
    )
    d = os.path.join(home, "data", APP_ID)
    os.makedirs(d, exist_ok=True)
    return d


def list_skill_slugs(root: str | None = None) -> list[str]:
    """Sorted, de-duplicated list of skill slugs in the workspace — every
    immediate subdirectory of ``skills/`` that contains a ``SKILL.md`` (the
    same shape Claude Code discovers). Missing dir → empty list."""
    base = Path(root or skills_dir())
    if not base.is_dir():
        return []
    slugs = {
        entry.name
        for entry in base.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    }
    return sorted(slugs)


def compute_state_hash(slugs) -> str:
    """sha256 of the sorted, de-duplicated slug list — MUST stay byte-identical
    to agents-platform-multitenant's ``api/runners.py::_state_hash``."""
    joined = "\n".join(sorted(set(slugs)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class SkillsSyncClient:
    """Stateful sync client — one per app activation. Holds the target platform
    URL + identity token, the workspace name, and persists the last acked
    state so deltas survive restarts."""

    def __init__(self, base: str, token: str, workspace: str,
                 *, data_dir: str | None = None, skills_root: str | None = None,
                 timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.workspace = workspace
        self.skills_root = skills_root
        self.timeout = timeout
        self._ack_path = os.path.join(data_dir or _data_dir(), "skills_sync_ack.json")

    # -- acked-state persistence --------------------------------------------
    def _load_ack(self) -> dict:
        try:
            with open(self._ack_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "hash" in data:
                data.setdefault("slugs", [])
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_ack(self, state_hash: str, slugs: list[str]) -> None:
        tmp = f"{self._ack_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"hash": state_hash, "slugs": sorted(set(slugs))}, f)
        os.replace(tmp, self._ack_path)  # atomic

    def _clear_ack(self) -> None:
        """Force the next cycle to full-sync (called on 409 / any failure)."""
        try:
            os.remove(self._ack_path)
        except FileNotFoundError:
            pass

    # -- HTTP ---------------------------------------------------------------
    def _post(self, payload: dict) -> httpx.Response:
        url = f"{self.base}/api/runners/skills/sync"
        with httpx.Client(timeout=self.timeout) as client:
            return client.post(url, json=payload,
                               headers={"Authorization": f"Bearer {self.token}"})

    def _full_sync(self, slugs: list[str], state_hash: str) -> dict:
        resp = self._post({
            "workspace": self.workspace,
            "mode": "full",
            "skills": slugs,
            "state_hash": state_hash,
        })
        resp.raise_for_status()
        self._save_ack(state_hash, slugs)
        return {"mode": "full", "skill_count": len(slugs), "state_hash": state_hash,
                "server": resp.json()}

    def _delta_sync(self, slugs: list[str], state_hash: str, ack: dict) -> dict:
        prev = set(ack.get("slugs", []))
        current = set(slugs)
        added = sorted(current - prev)
        removed = sorted(prev - current)
        resp = self._post({
            "workspace": self.workspace,
            "mode": "delta",
            "added": added,
            "removed": removed,
            "prev_hash": ack.get("hash"),
            "state_hash": state_hash,
        })
        if resp.status_code == 409:
            # Server drifted from our view — drop the ack so the next cycle
            # does a full reconcile instead of stacking deltas on a bad base.
            self._clear_ack()
            log.warning("skills_sync: delta rejected (409 hash_mismatch), "
                        "forcing full sync next cycle")
            return {"mode": "delta", "status": "hash_mismatch", "forced_full": True}
        resp.raise_for_status()
        self._save_ack(state_hash, slugs)
        return {"mode": "delta", "added": added, "removed": removed,
                "state_hash": state_hash, "server": resp.json()}

    # -- public entry points (called by the watchdog) -----------------------
    def sync_incremental(self) -> dict:
        """Delta cadence: no-op if unchanged since the ack; full sync if there
        is no ack yet (first run / after a reset)."""
        slugs = list_skill_slugs(self.skills_root)
        state_hash = compute_state_hash(slugs)
        ack = self._load_ack()
        if not ack:
            return self._full_sync(slugs, state_hash)
        if ack.get("hash") == state_hash:
            return {"mode": "delta", "status": "unchanged", "state_hash": state_hash}
        try:
            return self._delta_sync(slugs, state_hash, ack)
        except httpx.HTTPError:
            self._clear_ack()
            raise

    def sync_full(self) -> dict:
        """Reconcile cadence: always ship the complete list."""
        slugs = list_skill_slugs(self.skills_root)
        state_hash = compute_state_hash(slugs)
        try:
            return self._full_sync(slugs, state_hash)
        except httpx.HTTPError:
            self._clear_ack()
            raise
