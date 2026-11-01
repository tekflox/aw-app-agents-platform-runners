"""Unit tests for identity_token.py (Kanban
feature:ap-runners-auto-mint-identity-token) — the mint+persist half of the
auto-refreshed agents_platform_token. Same stub-the-transport style as
test_notion_token_sync.py. No real network, no aw-backend, no workspace API.
"""
from __future__ import annotations

import base64
import json
import logging
import time

import httpx
import pytest

from agents_platform_runners_app import identity_token as idt
from agents_platform_runners_app import plugin as plugin_mod

ENV = {
    "AW_BACKEND_URL": "http://aw-backend",
    "AW_WORKSPACE": "aw",
    "AW_WORKSPACE_HOST_TOKEN": "awlk_host_tok",
    "AW_WORKSPACE_API_URL": "http://aw-workspace-api",
    "AW_WORKSPACE_API_KEY": "wsapikey",
}

# Passed explicitly as ``agents_platform_base`` wherever a test exercises the
# aw-skills credential push: platform_base.resolve() otherwise derives a real
# public hostname from the ambient AW_BACKEND_URL, which is a confusing thing
# to read in an assertion even with the transport stubbed.
AP_MT = "http://agents-platform-multitenant:10014"
SKILLS_TOKEN_URL = f"{AP_MT}/api/runners/skills-token"


@pytest.fixture(autouse=True)
def _workspace_env_stub(monkeypatch):
    monkeypatch.setattr(plugin_mod, "_workspace_env", lambda name: ENV.get(name, ""))


def _token(iat: float, exp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": iat, "exp": exp}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _stub_client(monkeypatch, responder, recorder=None):
    """Replace httpx.Client inside identity_token with one whose .post(url,
    **kwargs) is answered by responder(url, kwargs) — a FakeResponse, or an
    exception instance to raise."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            if recorder is not None:
                recorder.append((url, kwargs))
            result = responder(url, kwargs)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(idt.httpx, "Client", _Client)


# --- needs_refresh: the decode + half-life policy, no network --------------


def test_no_token_configured_needs_refresh():
    assert idt.needs_refresh(None) is True
    assert idt.needs_refresh("") is True


def test_token_past_half_life_needs_refresh():
    now = 1_000_000.0
    token = _token(iat=now - 20 * 86400, exp=now + 10 * 86400)  # 20/30 days elapsed
    assert idt.needs_refresh(token, now=now) is True


def test_token_at_90pct_life_remaining_does_not_need_refresh():
    now = 1_000_000.0
    token = _token(iat=now - 3 * 86400, exp=now + 27 * 86400)  # 3/30 days elapsed = 10%
    assert idt.needs_refresh(token, now=now) is False


def test_garbage_non_jwt_token_needs_refresh_no_exception():
    assert idt.needs_refresh("not-a-jwt") is True
    assert idt.needs_refresh("a.b") is True
    assert idt.needs_refresh("a.b.c") is True
    assert idt.needs_refresh("...") is True


# --- refresh(): the mint + persist round trip --------------------------------


def test_refresh_attempts_mint_when_no_token_configured(monkeypatch):
    seen = []

    def responder(url, kwargs):
        if url.endswith("/identity-token"):
            return FakeResponse(200, {"token": _token(0, 9_999_999_999)})
        return FakeResponse(200, {})

    _stub_client(monkeypatch, responder, seen)
    result = idt.refresh({})

    assert result is not None
    assert any(u.endswith("/api/workspaces/aw/identity-token") for u, _ in seen)


def test_refresh_not_attempted_with_plenty_of_life_left(monkeypatch):
    now = time.time()
    token = _token(iat=now - 3600, exp=now + 30 * 86400)  # far under half-life
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    result = idt.refresh({"agents_platform_token": token, "agents_platform_base": AP_MT})

    assert result is None
    # No mint, no persist — but the aw-skills credential push still happens on
    # this pass (see test_skills_token_pushed_even_when_no_mint_was_due).
    assert not any(u.endswith("/identity-token") or u.endswith("/config") for u, _ in seen)


def test_refresh_returns_none_on_backend_500_no_raise_config_untouched(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(500, None, "boom"))
    config = {}

    result = idt.refresh(config)

    assert result is None
    assert config == {}


def test_refresh_returns_none_on_connection_error_no_raise_config_untouched(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: httpx.ConnectError("connection refused"))
    config = {"agents_platform_token": "garbage"}

    result = idt.refresh(config)

    assert result is None
    assert config == {"agents_platform_token": "garbage"}


def test_refresh_persists_via_own_config_endpoint_with_api_key_header(monkeypatch):
    seen = []

    def responder(url, kwargs):
        if url.endswith("/identity-token"):
            return FakeResponse(200, {"token": _token(0, 9_999_999_999)})
        return FakeResponse(200, {"config": {}})

    _stub_client(monkeypatch, responder, seen)
    result = idt.refresh({})

    assert result is not None
    persist_calls = [(u, k) for u, k in seen if u.endswith("/config")]
    assert len(persist_calls) == 1
    url, kwargs = persist_calls[0]
    assert url == "http://aw-workspace-api/api/apps/agents-platform-runners/config"
    assert kwargs["headers"] == {"X-Api-Key": "wsapikey"}
    assert kwargs["json"] == {"config": {"agents_platform_token": result}}


def test_refresh_missing_env_vars_returns_none_no_raise(monkeypatch):
    monkeypatch.setattr(plugin_mod, "_workspace_env", lambda name: "")

    result = idt.refresh({})

    assert result is None


def test_refresh_returns_none_when_persist_target_env_missing(monkeypatch):
    partial_env = {**ENV, "AW_WORKSPACE_API_URL": "", "AW_WORKSPACE_API_KEY": ""}
    monkeypatch.setattr(plugin_mod, "_workspace_env", lambda name: partial_env.get(name, ""))

    seen = []

    def responder(url, kwargs):
        return FakeResponse(200, {"token": _token(0, 9_999_999_999)})

    _stub_client(monkeypatch, responder, seen)
    result = idt.refresh({})

    assert result is None
    # the mint was attempted, but nothing was ever posted to a /config URL
    assert not any(u.endswith("/config") for u, _ in seen)


# --- push_skills_token(): hop 3, the aw-skills credential (DESIGN §17.1) -----
#
# Best-effort by contract: none of these may raise, and none may change what
# refresh() returns. The AP-MT side is an idempotent upsert that derives the
# tenant from the presented token (agents-platform-multitenant
# api/runners.py::register_skills_token), so this client's whole job is to
# present the token and survive every way that can fail.


def test_skills_token_pushed_even_when_no_mint_was_due(monkeypatch):
    """The reason this is not gated on a mint: at release time every workspace
    holds a token that is NOT yet due, so a mint-gated push would leave them
    all uncredentialed for up to half a token lifetime."""
    now = time.time()
    token = _token(iat=now - 3600, exp=now + 30 * 86400)
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {"configured": True}), seen)

    result = idt.refresh({"agents_platform_token": token, "agents_platform_base": AP_MT})

    assert result is None  # no mint happened
    pushes = [(u, k) for u, k in seen if u == SKILLS_TOKEN_URL]
    assert len(pushes) == 1
    _url, kwargs = pushes[0]
    assert kwargs["headers"] == {"Authorization": f"Bearer {token}"}
    assert kwargs["json"] == {"workspace": "aw"}


def test_skills_token_push_sends_the_freshly_minted_token_not_the_old_one(monkeypatch):
    seen = []
    minted = _token(0, 9_999_999_999)

    def responder(url, kwargs):
        if url.endswith("/identity-token"):
            return FakeResponse(200, {"token": minted})
        return FakeResponse(200, {})

    _stub_client(monkeypatch, responder, seen)
    result = idt.refresh({"agents_platform_token": "stale-garbage",
                          "agents_platform_base": AP_MT})

    assert result == minted
    pushes = [k for u, k in seen if u == SKILLS_TOKEN_URL]
    assert len(pushes) == 1
    assert pushes[0]["headers"] == {"Authorization": f"Bearer {minted}"}


def test_skills_token_push_carries_no_credential_and_no_tenant_in_the_body(monkeypatch):
    """The security property the AP-MT route is built on: the body names only
    the workspace. A token field, or a tenant field, would make the stored
    credential something the caller asserts rather than something it proved."""
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    idt.push_skills_token({"agents_platform_base": AP_MT}, "tok")

    _url, kwargs = seen[0]
    assert set(kwargs["json"]) == {"workspace"}


def test_skills_token_push_omits_workspace_when_this_workspace_cannot_name_itself(monkeypatch):
    """AP-MT falls back to the token's sole membership on an absent workspace,
    and errors loudly when the token has several — both better than a guess."""
    monkeypatch.setattr(plugin_mod, "_workspace_env",
                        lambda name: "" if name == "AW_WORKSPACE" else ENV.get(name, ""))
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    idt.push_skills_token({"agents_platform_base": AP_MT}, "tok")

    _url, kwargs = seen[0]
    assert kwargs["json"] == {}


def test_skills_token_push_never_posts_an_empty_token(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    assert idt.push_skills_token({"agents_platform_base": AP_MT}, None) is False
    assert idt.push_skills_token({"agents_platform_base": AP_MT}, "") is False
    assert seen == []


# --- the hard requirement: a broken AP-MT cannot break the refresh cycle -----


@pytest.mark.parametrize("outcome", [
    FakeResponse(404, None, "Not Found"),          # older AP-MT, no route yet
    FakeResponse(410, None, "Gone"),
    FakeResponse(401, None, "unauthorized"),
    FakeResponse(403, None, "not a member of that workspace"),
    FakeResponse(503, None, "secret encryption unavailable"),
    FakeResponse(500, None, "boom"),
    httpx.ConnectError("connection refused"),
    httpx.ReadTimeout("timed out"),
    httpx.HTTPError("something else entirely"),
])
def test_a_failing_skills_token_push_still_lets_the_token_rotate(monkeypatch, outcome):
    """The card's hard requirement: "se o endpoint estiver indisponível no meio
    do refresh, o ciclo de renovação do identity token não pode falhar/quebrar
    por isso". The mint and the persist must both still have happened, and
    refresh() must still return the new token so the caller updates
    ``_live_config`` and rewrites mcp.json."""
    seen = []
    minted = _token(0, 9_999_999_999)

    def responder(url, kwargs):
        if url == SKILLS_TOKEN_URL:
            return outcome
        if url.endswith("/identity-token"):
            return FakeResponse(200, {"token": minted})
        return FakeResponse(200, {})

    _stub_client(monkeypatch, responder, seen)
    result = idt.refresh({"agents_platform_base": AP_MT})

    assert result == minted
    assert any(u.endswith("/config") for u, _ in seen)
    assert any(u == SKILLS_TOKEN_URL for u, _ in seen)


def test_push_skills_token_returns_false_and_never_raises_on_every_failure(monkeypatch):
    for outcome in (FakeResponse(404, None, "Not Found"),
                    FakeResponse(500, None, "boom"),
                    httpx.ConnectError("refused"),
                    httpx.ReadTimeout("timed out")):
        _stub_client(monkeypatch, lambda url, kwargs: outcome)
        assert idt.push_skills_token({"agents_platform_base": AP_MT}, "tok") is False


def test_a_404_from_an_older_apmt_is_logged_as_info_not_a_warning(monkeypatch, caplog):
    """DESIGN §17.9 risk 5: app updates are pull-based, so a workspace can run
    this client against an AP-MT without the route for a while. Logging that at
    WARNING every 6h, forever, is how a healthy deployment learns to ignore its
    own warnings."""
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(404, None, "Not Found"))

    with caplog.at_level(logging.DEBUG, logger=idt.log.name):
        assert idt.push_skills_token({"agents_platform_base": AP_MT}, "tok") is False

    levels = {r.levelno for r in caplog.records}
    assert logging.INFO in levels
    assert logging.WARNING not in levels
    assert logging.ERROR not in levels


def test_the_push_goes_to_the_configured_platform_base(monkeypatch):
    """Resolution is platform_base.resolve()'s, not a second opinion — same
    address every other push in this app uses."""
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    idt.push_skills_token({"agents_platform_base": "https://ap-mt.example.com/"}, "tok")

    assert seen[0][0] == "https://ap-mt.example.com/api/runners/skills-token"


def test_an_unexpected_exception_in_the_push_still_lets_the_token_rotate(monkeypatch):
    """The structural half of the guarantee above: even if the push raises
    something it never anticipated, a mint that already succeeded and persisted
    must still be reported to the caller as the new token."""
    minted = _token(0, 9_999_999_999)
    seen = []

    def responder(url, kwargs):
        if url.endswith("/identity-token"):
            return FakeResponse(200, {"token": minted})
        return FakeResponse(200, {})

    _stub_client(monkeypatch, responder, seen)

    def _boom(config, token):
        raise RuntimeError("something nobody enumerated")

    monkeypatch.setattr(idt, "push_skills_token", _boom)

    result = idt.refresh({"agents_platform_base": AP_MT})

    assert result == minted
    assert any(u.endswith("/config") for u, _ in seen)
