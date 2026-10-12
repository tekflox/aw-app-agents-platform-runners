"""shared_redis.resolve() — the discovery chain that keeps a freshly created
workspace's Runner working without a hand-pasted secret."""
from __future__ import annotations

import pytest

from agents_platform_runners_app import shared_redis


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("AW_SHARED_REDIS_URL", "AW_SHARED_REDIS_DB", "AW_SHARED_REDIS_PORT",
                "AW_SHARED_REDIS_PASSWORD", "AW_SHARED_REDIS_USER"):
        monkeypatch.delenv(var, raising=False)
    shared_redis.reset_cache()
    yield
    shared_redis.reset_cache()


def _reachable(*hosts):
    """Stub _probe so only ``hosts`` answer "ok"; everything else is "down"."""
    return lambda host, port, user, password: "ok" if host in hosts else "down"


def _locked(*hosts):
    """Stub _probe so ``hosts`` answer "auth" (found, but refuses us)."""
    return lambda host, port, user, password: "auth" if host in hosts else "down"


def test_explicit_config_wins_over_everything(monkeypatch):
    monkeypatch.setenv("AW_SHARED_REDIS_URL", "redis://from-env:6379/9")
    monkeypatch.setattr(shared_redis, "_probe", _reachable("172.18.0.1"))
    assert shared_redis.resolve(
        {"shared_redis_url": "redis://:pw@explicit:6379/3"}
    ) == "redis://:pw@explicit:6379/3"


def test_env_used_when_config_blank(monkeypatch):
    monkeypatch.setenv("AW_SHARED_REDIS_URL", "redis://from-env:6379/9")
    monkeypatch.setattr(shared_redis, "_probe", _reachable("172.18.0.1"))
    assert shared_redis.resolve({"shared_redis_url": ""}) == "redis://from-env:6379/9"


def test_prefers_default_route_when_it_answers(monkeypatch):
    monkeypatch.setattr(shared_redis, "default_gateway_ip", lambda: "10.0.0.1")
    monkeypatch.setattr(shared_redis, "_probe", _reachable("10.0.0.1", "172.18.0.1"))
    assert shared_redis.resolve({}) == "redis://10.0.0.1:6379/1"


def test_skips_default_route_that_has_no_redis(monkeypatch):
    """The measured aw-remote-host topology: podman's gateway is the default
    route but the shared Redis lives on the docker bridge gateway."""
    monkeypatch.setattr(shared_redis, "default_gateway_ip", lambda: "10.89.0.1")
    monkeypatch.setattr(shared_redis, "_probe", _reachable("172.18.0.1"))
    assert shared_redis.resolve({}) == "redis://172.18.0.1:6379/1"


def test_db_and_port_overridable(monkeypatch):
    monkeypatch.setenv("AW_SHARED_REDIS_DB", "4")
    monkeypatch.setenv("AW_SHARED_REDIS_PORT", "6380")
    monkeypatch.setattr(shared_redis, "default_gateway_ip", lambda: None)
    monkeypatch.setattr(shared_redis, "_probe", _reachable("172.17.0.1"))
    assert shared_redis.resolve({}) == "redis://172.17.0.1:6380/4"


def test_none_when_nothing_answers(monkeypatch):
    """Callers must keep failing loudly rather than inventing an address."""
    monkeypatch.setattr(shared_redis, "default_gateway_ip", lambda: "10.89.0.1")
    monkeypatch.setattr(shared_redis, "_probe", _reachable())
    assert shared_redis.resolve({}) is None


def test_auth_required_and_no_credential_configured_fails_loudly(monkeypatch):
    """The whole point of the fix: a host that IS there but demands a
    credential we don't have must never be cached as a passwordless URL —
    collapsing this into "nothing answered" would recreate the silent
    degradation the probe used to produce once auth was turned on."""
    monkeypatch.setattr(shared_redis, "default_gateway_ip", lambda: "10.89.0.1")
    monkeypatch.setattr(shared_redis, "_probe", _locked("172.18.0.1"))
    assert shared_redis.resolve({}) is None


def test_credential_configured_is_injected_into_the_discovered_url(monkeypatch):
    monkeypatch.setenv("AW_SHARED_REDIS_PASSWORD", "s3cret")
    monkeypatch.setattr(shared_redis, "default_gateway_ip", lambda: "10.89.0.1")
    monkeypatch.setattr(shared_redis, "_probe", _reachable("172.18.0.1"))
    assert shared_redis.resolve({}) == "redis://default:s3cret@172.18.0.1:6379/1"


def test_credential_user_overridable(monkeypatch):
    monkeypatch.setenv("AW_SHARED_REDIS_PASSWORD", "s3cret")
    monkeypatch.setenv("AW_SHARED_REDIS_USER", "svc-runs")
    monkeypatch.setattr(shared_redis, "default_gateway_ip", lambda: "10.89.0.1")
    monkeypatch.setattr(shared_redis, "_probe", _reachable("172.18.0.1"))
    assert shared_redis.resolve({}) == "redis://svc-runs:s3cret@172.18.0.1:6379/1"


def test_probe_result_is_cached(monkeypatch):
    calls = []

    def _probe(host, port, user, password):
        calls.append(host)
        return "ok" if host == "172.18.0.1" else "down"

    monkeypatch.setattr(shared_redis, "default_gateway_ip", lambda: "10.89.0.1")
    monkeypatch.setattr(shared_redis, "_probe", _probe)
    assert shared_redis.resolve({}) == "redis://172.18.0.1:6379/1"
    before = len(calls)
    assert shared_redis.resolve({}) == "redis://172.18.0.1:6379/1"
    assert len(calls) == before, "second resolve() must not re-probe"


def test_candidate_hosts_dedupes_default_route(monkeypatch):
    monkeypatch.setattr(shared_redis, "default_gateway_ip", lambda: "172.18.0.1")
    hosts = shared_redis.candidate_hosts()
    assert hosts[0] == "172.18.0.1"
    assert hosts.count("172.18.0.1") == 1


def test_default_gateway_ip_parses_proc_net_route(monkeypatch, tmp_path):
    route = tmp_path / "route"
    # Real /proc/net/route shape: the default route is the 00000000 destination
    # row; addresses are little-endian hex. 010012AC == 172.18.0.1.
    route.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n"
        "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\n"
    )
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(route, *a, **k) if p == "/proc/net/route"
        else real_open(p, *a, **k),
    )
    assert shared_redis.default_gateway_ip() == "172.18.0.1"


def test_default_gateway_ip_none_when_unreadable(monkeypatch):
    def _boom(p, *a, **k):
        raise OSError("no /proc here")
    monkeypatch.setattr("builtins.open", _boom)
    assert shared_redis.default_gateway_ip() is None
