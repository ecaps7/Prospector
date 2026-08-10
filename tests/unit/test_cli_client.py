from __future__ import annotations

from prospector.cli.client import ProspectorClient


def test_client_ignores_system_http_proxy() -> None:
    client = ProspectorClient("http://127.0.0.1:7620")
    try:
        assert client.http.trust_env is False
    finally:
        client.close()
