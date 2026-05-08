import pytest

from mcp_email_server.app import mcp
from mcp_email_server.cli import (
    WILDCARD_IPV4_BIND_HOST,
    _build_transport_security_settings,
    _configure_http_transport,
    _expand_allowed_hosts,
    _expand_allowed_origins,
    _split_csv,
)


def test_split_csv_trims_empty_items():
    assert _split_csv(" localhost:*, mcp-email-server:* ,, ") == ["localhost:*", "mcp-email-server:*"]


def test_expand_allowed_hosts_adds_wildcard_port_for_bare_host():
    assert _expand_allowed_hosts(["mcp-email-server", "localhost:*", "[::1]"]) == [
        "mcp-email-server",
        "mcp-email-server:*",
        "localhost:*",
        "[::1]",
        "[::1]:*",
    ]


def test_expand_allowed_origins_adds_wildcard_port_for_bare_origin():
    assert _expand_allowed_origins(["http://mcp-email-server", "http://localhost:*"]) == [
        "http://mcp-email-server",
        "http://mcp-email-server:*",
        "http://localhost:*",
    ]


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", " OFF "])
def test_transport_security_can_be_disabled_with_false_values(monkeypatch, value):
    monkeypatch.setenv("MCP_ENABLE_DNS_REBINDING_PROTECTION", value)

    settings = _build_transport_security_settings(WILDCARD_IPV4_BIND_HOST, 9557)

    assert settings.enable_dns_rebinding_protection is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "unexpected", ""])
def test_transport_security_stays_enabled_with_non_false_values(monkeypatch, value):
    monkeypatch.setenv("MCP_ENABLE_DNS_REBINDING_PROTECTION", value)

    settings = _build_transport_security_settings(WILDCARD_IPV4_BIND_HOST, 9557)

    assert settings.enable_dns_rebinding_protection is True


def test_transport_security_uses_explicit_allowed_hosts(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp-email-server,localhost:*")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "http://mcp-email-server,http://localhost:*")

    settings = _build_transport_security_settings(WILDCARD_IPV4_BIND_HOST, 9557)

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["mcp-email-server", "mcp-email-server:*", "localhost:*"]
    assert settings.allowed_origins == [
        "http://mcp-email-server",
        "http://mcp-email-server:*",
        "http://localhost:*",
    ]


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("MCP_ALLOWED_HOSTS", "*"),
        ("MCP_ALLOWED_ORIGINS", "*"),
    ],
)
def test_transport_security_wildcard_allowlist_disables_protection(monkeypatch, env_name, env_value):
    monkeypatch.setenv(env_name, env_value)

    settings = _build_transport_security_settings(WILDCARD_IPV4_BIND_HOST, 9557)

    assert settings.enable_dns_rebinding_protection is False


@pytest.mark.parametrize("host", [WILDCARD_IPV4_BIND_HOST, "::", ""])
def test_transport_security_defaults_to_loopback_for_wildcard_bind(monkeypatch, host):
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

    settings = _build_transport_security_settings(host, 9557)

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert settings.allowed_origins == [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]


def test_transport_security_defaults_include_named_host(monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

    settings = _build_transport_security_settings("mcp-email-server", 9557)

    assert settings.enable_dns_rebinding_protection is True
    assert "mcp-email-server" in settings.allowed_hosts
    assert "mcp-email-server:9557" in settings.allowed_hosts
    assert "mcp-email-server:*" in settings.allowed_hosts
    assert "http://mcp-email-server:9557" in settings.allowed_origins
    assert "http://mcp-email-server:*" in settings.allowed_origins


def test_configure_http_transport_updates_mcp_settings(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp-email-server:*")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "http://mcp-email-server:*")

    original_host = mcp.settings.host
    original_port = mcp.settings.port
    original_transport_security = mcp.settings.transport_security

    try:
        _configure_http_transport(WILDCARD_IPV4_BIND_HOST, 9557)

        assert mcp.settings.host == WILDCARD_IPV4_BIND_HOST
        assert mcp.settings.port == 9557
        assert mcp.settings.transport_security.allowed_hosts == ["mcp-email-server:*"]
        assert mcp.settings.transport_security.allowed_origins == ["http://mcp-email-server:*"]
    finally:
        mcp.settings.host = original_host
        mcp.settings.port = original_port
        mcp.settings.transport_security = original_transport_security
