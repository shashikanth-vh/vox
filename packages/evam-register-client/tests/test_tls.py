"""Private certificate trust and proxy-prefix preservation."""
import shutil
import ssl
import subprocess

import httpx
import pytest

from evam_register_client import AsyncRegisterClient, RegisterClientConfig


def test_private_ca_augments_public_roots_and_keeps_verification(tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl required to generate a temporary test certificate")
    cert = tmp_path / "ca.pem"
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=register-test", "-addext", "basicConstraints=critical,CA:TRUE",
        "-keyout", str(tmp_path / "key.pem"), "-out", str(cert),
    ], check=True, capture_output=True)
    baseline = set(ssl.create_default_context().get_ca_certs(binary_form=True))
    context = RegisterClientConfig(ca_file=str(cert)).tls_verify()
    roots = set(context.get_ca_certs(binary_form=True))
    assert baseline < roots
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert RegisterClientConfig().tls_verify() is True
    with pytest.raises(FileNotFoundError):
        RegisterClientConfig(ca_file=str(tmp_path / "missing.pem")).tls_verify()


async def test_machine_prefix_and_delegation_survive_client_url_join():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [], "count": 0})

    config = RegisterClientConfig(
        base_url="https://services.example.test:8443/machine",
        api_key="test-service-key", tenant="EVAM",
        extra_headers={"X-Internal-Context": "signed-context"},
    )
    async with AsyncRegisterClient(config=config, transport=httpx.MockTransport(handle)) as client:
        await client.list("entities", limit=2)
    assert seen[0].url.path == "/machine/v1/entities"
    assert seen[0].headers["X-API-Key"] == "test-service-key"
    assert seen[0].headers["X-Tenant"] == "EVAM"
    assert seen[0].headers["X-Internal-Context"] == "signed-context"
