"""Render deployment contracts without cluster credentials or provider access."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "deploy/helm/prism/charts/chitti"
VALUES = {
    "artifactId": "fixture-a", "credentials.existingSecret": "chitti-credentials",
    "provider.baseUrl": "https://provider.invalid/v1", "provider.model": "fixture",
}


def render(values):
    if not shutil.which("helm"):
        pytest.skip("Helm CLI required for chart verification")
    command = ["helm", "template", "fixture", str(CHART)]
    for key, value in values.items():
        command += ["--set", f"{key}={value}"]
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603


@pytest.mark.parametrize("missing", VALUES)
def test_chitti_chart_requires_artifact_and_provider_configuration(missing):
    result = render({key: value for key, value in VALUES.items() if key != missing})
    assert result.returncode != 0
    assert missing in result.stderr


def test_chitti_chart_has_internal_services_persistent_artifacts_and_bounded_runtime():
    result = render(VALUES)
    assert result.returncode == 0, result.stderr
    objects = list(yaml.safe_load_all(result.stdout))
    assert all(obj["spec"]["type"] == "ClusterIP" for obj in objects if obj["kind"] == "Service")
    claims = [obj for obj in objects if obj["kind"] == "PersistentVolumeClaim"]
    assert len(claims) == 2
    assert all(obj["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep" for obj in claims)
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and
                      obj["metadata"]["name"] == "prism-chitti")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["resources"]["limits"]["memory"] == "4Gi"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    env = {item["name"]: item for item in container["env"]}
    assert env["CHITTI_REQUIRE_DELEGATION"]["value"] == "true"
    assert env["CHITTI_LOG_PIPELINE"]["value"] == "false"
    assert env["CHITTI_API_KEYS"]["valueFrom"]["secretKeyRef"]["name"] == "chitti-credentials"
    job = next(obj for obj in objects if obj["kind"] == "Job")
    preparation = job["spec"]["template"]["spec"]["containers"][0]
    assert preparation["args"][0].count("--reuse-existing") == 2
    assert not any("valueFrom" in item for item in preparation["env"])
    assert len([obj for obj in objects if obj["kind"] == "NetworkPolicy"]) == 2
