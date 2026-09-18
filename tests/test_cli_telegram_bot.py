"""``agents-platform telegram-bot`` — redaction is the point of this group
(GET /bots returns token/webhook_secret in plaintext; the 2026-08-13 leak
this design cites). Table AND --json must both redact by default.
"""
from __future__ import annotations

import argparse

import pytest

from agents_platform_runners_app.cli.groups import telegram_bot as tb


class _FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return [
            {"id": "aw-17", "name": "aw-17", "token": "8450abcdefgh1234nO0I",
             "webhook_secret": "deadbeefdeadbeefdeadbeefdeadbeef", "enabled": True,
             "is_sysadmin": False, "agent_slug": "telegram-sonnet", "admin_user_ids": []},
        ]

    def post(self, path, json_body=None, params=None):
        self.calls.append(("POST", path, json_body))
        if path.endswith("/register-webhook"):
            return {"ok": True, "webhook_url": "https://agents-platform.aw.tekflox.com/api/telegram/webhook/aw-17"}
        return {**json_body, "admin_user_ids": json_body.get("admin_user_ids", [])}

    def delete(self, path, params=None):
        self.calls.append(("DELETE", path, params))
        return None


def _ns(**kwargs):
    defaults = dict(as_json=False, show_secrets=False)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_list_table_redacts_by_default(capsys):
    client = _FakeClient()
    rc = tb._list(client, _ns())
    out = capsys.readouterr().out
    assert rc == 0
    assert "8450abcdefgh1234nO0I" not in out
    assert "deadbeefdeadbeefdeadbeefdeadbeef" not in out
    assert "8450" in out and "nO0I" in out


def test_list_json_also_redacts(capsys):
    client = _FakeClient()
    rc = tb._list(client, _ns(as_json=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "8450abcdefgh1234nO0I" not in out
    assert "deadbeefdeadbeefdeadbeefdeadbeef" not in out


def test_list_show_secrets_reveals_full_values(capsys):
    client = _FakeClient()
    tb._list(client, _ns(show_secrets=True))
    out = capsys.readouterr().out
    assert "8450abcdefgh1234nO0I" in out


def test_add_registers_webhook_by_default(capsys):
    client = _FakeClient()
    ns = argparse.Namespace(
        bot_id="aw-99", token="tok", name=None, agent_slug="coder", webhook_secret=None,
        disabled=False, sysadmin=False, admin_user_ids=[], no_register_webhook=False,
        show_secrets=False, as_json=False,
    )
    rc = tb._add(client, ns)
    assert rc == 0
    post_paths = [c[1] for c in client.calls if c[0] == "POST"]
    assert "/api/telegram/bots" in post_paths
    assert "/api/telegram/bots/aw-99/register-webhook" in post_paths
    out = capsys.readouterr().out
    assert "Created bot 'aw-99'" in out
    assert "Webhook registered" in out


def test_add_webhook_failure_reports_created_but_exits_1(capsys):
    class _FailingWebhookClient(_FakeClient):
        def post(self, path, json_body=None, params=None):
            if path.endswith("/register-webhook"):
                raise RuntimeError("telegram API unreachable")
            return super().post(path, json_body=json_body, params=params)

    client = _FailingWebhookClient()
    ns = argparse.Namespace(
        bot_id="aw-99", token="tok", name=None, agent_slug=None, webhook_secret=None,
        disabled=False, sysadmin=False, admin_user_ids=[], no_register_webhook=False,
        show_secrets=False, as_json=False,
    )
    rc = tb._add(client, ns)
    assert rc == 1
    err = capsys.readouterr().err
    assert "webhook registration failed" in err


def test_add_no_register_webhook_skips_it():
    client = _FakeClient()
    ns = argparse.Namespace(
        bot_id="aw-99", token="tok", name=None, agent_slug=None, webhook_secret=None,
        disabled=False, sysadmin=False, admin_user_ids=[], no_register_webhook=True,
        show_secrets=False, as_json=False,
    )
    rc = tb._add(client, ns)
    assert rc == 0
    post_paths = [c[1] for c in client.calls if c[0] == "POST"]
    assert "/api/telegram/bots/aw-99/register-webhook" not in post_paths


def test_add_generates_webhook_secret_when_omitted():
    client = _FakeClient()
    ns = argparse.Namespace(
        bot_id="aw-99", token="tok", name=None, agent_slug=None, webhook_secret=None,
        disabled=False, sysadmin=False, admin_user_ids=[], no_register_webhook=True,
        show_secrets=False, as_json=False,
    )
    tb._add(client, ns)
    body = next(c[2] for c in client.calls if c[0] == "POST" and c[1] == "/api/telegram/bots")
    assert len(body["webhook_secret"]) == 64  # secrets.token_hex(32)


def test_delete_requires_yes_on_non_tty(monkeypatch, capsys):
    client = _FakeClient()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    ns = argparse.Namespace(bot_id="aw-17", yes=False)
    rc = tb._delete(client, ns)
    assert rc == 1
    assert not any(c[0] == "DELETE" for c in client.calls)


def test_delete_with_yes_skips_prompt():
    client = _FakeClient()
    ns = argparse.Namespace(bot_id="aw-17", yes=True)
    rc = tb._delete(client, ns)
    assert rc == 0
    assert ("DELETE", "/api/telegram/bots/aw-17", None) in client.calls
