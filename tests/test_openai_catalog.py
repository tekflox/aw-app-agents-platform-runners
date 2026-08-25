"""The contributed OpenAI catalogue, and the key it runs on.

Both halves of this used to be somewhere else, and neither worked:

  * The models were four slugs hardcoded in agents-platform's ``seed.py``
    (``openai-gpt-4o``/``-gpt-4-1``/``-o1``/``-o3-mini``). A hardcoded seed
    can't track a catalogue that ships a new frontier model every few
    months, and because ``_insert_if_missing`` re-runs on every boot,
    deleting a stale one only made it come back. They live here now, where
    an app release refreshes them without a platform deploy.

  * The key came from ``OPENAI_API_KEY`` in the platform's process env and
    nowhere else, while the settings row a workspace owner actually fills in
    (``Settings.openai_api_key``) drove only TTS/STT. So every ``openai``
    model could be seeded, listed, and picked in the UI, and still refuse at
    dispatch with "OPENAI_API_KEY not set".

The rules pinned below are the ones a well-meaning catalogue refresh breaks
silently: a model that 404s or is v1/responses-only seeds *fine* and fails at
the first dispatch, and a ``temperature`` on a reasoning model is a 400 on
every call. Both are invisible until someone runs an agent.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agents_platform_runners_app import platform_settings

APP_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((APP_DIR / "aw-app.json").read_text())


@pytest.fixture(scope="module")
def openai_models(manifest) -> dict:
    return {m["slug"]: m for m in manifest["contributes"]["agents"]["models"]
            if m["provider"] == "openai"}


# ---------------------------------------------------------------- catalogue

def test_ships_the_current_frontier(openai_models):
    """GPT-5.6 is the current generation; a catalogue without it is stale by
    definition, which is the failure mode the platform seed had."""
    assert {"openai-gpt-5-6-sol", "openai-gpt-5-6-luna",
            "openai-gpt-5-6-terra"} <= set(openai_models)


def test_reclaims_the_slugs_the_platform_used_to_seed(openai_models):
    """Same four slugs, so an agent still pointing at one keeps resolving —
    the migration off the platform seed must not orphan ``model_slug``."""
    assert {"openai-gpt-4o", "openai-gpt-4-1",
            "openai-o1", "openai-o3-mini"} <= set(openai_models)


def test_no_responses_only_or_deprecated_models(openai_models):
    """agents-platform's openai provider is LangChain ``ChatOpenAI``, i.e.
    POST /v1/chat/completions. These families are listed by /v1/models and
    rejected on use, so seeding one produces a model that looks available in
    the UI and dies at dispatch.

    Verified against the live API 2026-08-25: every ``-pro`` variant and
    ``gpt-5.3-codex`` answer "not supported in the v1/chat/completions
    endpoint"; the ``*-chat-latest`` snapshots and the whole retired
    ``*-codex`` line answer "has been deprecated".
    """
    for slug, model in openai_models.items():
        model_id = model["model_id"]
        assert not model_id.endswith("-pro"), f"{slug}: v1/responses only"
        assert not model_id.endswith("-codex"), f"{slug}: deprecated or responses-only"
        assert not model_id.endswith("-chat-latest"), f"{slug}: deprecated snapshot"


def test_reasoning_models_carry_no_temperature(openai_models):
    """GPT-5.x and the o-series accept ``temperature=1`` and nothing else —
    any other value is a 400 on every single call. Only the gpt-4.x family
    takes one, so the split is by model id, not by taste."""
    for slug, model in openai_models.items():
        reasoning = not model["model_id"].startswith("gpt-4")
        if reasoning:
            assert "temperature" not in model["params"], \
                f"{slug} is a reasoning model and cannot take a temperature"


def test_every_model_id_is_undated(openai_models):
    """Dated snapshots (``gpt-5.4-2026-03-05``) pin a build that OpenAI
    retires on its own schedule; the alias tracks the replacement."""
    for slug, model in openai_models.items():
        assert not any(part.isdigit() and len(part) == 4
                       for part in model["model_id"].split("-")), \
            f"{slug} pins a dated snapshot"


# ------------------------------------------------------------- key push

def _client_transport(recorder: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})
    return httpx.MockTransport(handler)


def test_declares_the_key_as_a_secret_field(manifest):
    """Rendered masked, and excluded from anything that logs the config."""
    field = manifest["config_schema"]["properties"]["openai_api_key"]
    assert field["x-secret"] is True


def test_save_pushes_the_key_to_the_platform():
    calls: list = []
    ok = platform_settings.push_settings(
        base="http://platform:10014", token="jwt",
        config={"openai_api_key": "sk-real"},
        transport=_client_transport(calls))
    assert calls == [("/api/settings/openai_api_key", {"value": "sk-real"})]
    assert ok == {"openai_api_key": True}


def test_blank_key_does_not_clear_the_platforms_value():
    """A blank field means "not configured here", never "clear it" — the
    platform UI can set this key too, and saving an unrelated field on this
    panel must not wipe it."""
    calls: list = []
    for blank in ("", "   ", None):
        platform_settings.push_settings(
            base="http://platform:10014", token="jwt",
            config={"openai_api_key": blank},
            transport=_client_transport(calls))
    assert calls == []


def test_push_never_raises_when_the_platform_is_down():
    """This runs inside ``on_config_saved``; a platform outage must not turn
    a settings save into an error that loses every other field."""
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    ok = platform_settings.push_settings(
        base="http://platform:10014", token="jwt",
        config={"openai_api_key": "sk-real"},
        transport=httpx.MockTransport(boom))
    assert ok == {"openai_api_key": False}


def test_push_reports_a_rejected_key():
    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "openai_api_key must be a string"})

    ok = platform_settings.push_settings(
        base="http://platform:10014", token="jwt",
        config={"openai_api_key": "sk-real"},
        transport=httpx.MockTransport(rejected))
    assert ok == {"openai_api_key": False}


def test_push_skipped_without_a_platform_link():
    """No base or no token is a not-configured app, not an error."""
    calls: list = []
    assert platform_settings.push_settings(
        base="", token="jwt", config={"openai_api_key": "sk"},
        transport=_client_transport(calls)) == {}
    assert platform_settings.push_settings(
        base="http://platform:10014", token="", config={"openai_api_key": "sk"},
        transport=_client_transport(calls)) == {}
    assert calls == []
