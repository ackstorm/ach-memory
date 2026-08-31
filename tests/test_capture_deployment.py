"""Deployment/observability for the disabled-by-default capture worker
(SPEC Phase 3 Task 8): Helm renders nothing by default, Compose exposes it
only under an explicit profile, and neither ships a public Service/Ingress
-- the worker serves nothing, it only leases rows and calls Hindsight/the
database.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "ach-memory"

REQUIRED_SET = [
    "--set", "config.databaseUrl=postgresql://x/x",
    "--set", "config.hindsight.url=http://hindsight.test",
    "--set", "masterKeySecret.value=abcd",
]


def _render(*extra_args: str) -> list[dict]:
    result = subprocess.run(
        ["helm", "template", "test", str(CHART), *REQUIRED_SET, *extra_args],
        capture_output=True, text=True, check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _capture_worker_deployment(docs: list[dict]) -> dict | None:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith(
            "-capture-worker"
        ):
            return doc
    return None


def test_helm_renders_no_capture_worker_by_default():
    docs = _render()

    assert _capture_worker_deployment(docs) is None


def test_helm_renders_the_capture_worker_only_with_explicit_enablement():
    docs = _render("--set", "captureWorker.enabled=true")

    deployment = _capture_worker_deployment(docs)
    assert deployment is not None
    assert deployment["spec"]["replicas"] == 1


def test_the_capture_worker_env_defaults_to_disabled_even_when_rendered():
    """enabled=true only stages the rollout (image pulled, config wired) --
    the process inside must still refuse to lease anything unless
    workerEnabled is ALSO explicitly turned on."""
    docs = _render("--set", "captureWorker.enabled=true")

    env = {
        item["name"]: item.get("value")
        for item in _capture_worker_deployment(docs)["spec"]["template"]["spec"]["containers"][
            0
        ]["env"]
    }
    assert env["MEMORY_CAPTURE_WORKER_ENABLED"] == "false"


def test_the_capture_worker_shares_database_and_hindsight_configuration():
    docs = _render(
        "--set", "captureWorker.enabled=true", "--set", "captureWorker.workerEnabled=true",
    )

    env = {
        item["name"]: item.get("value")
        for item in _capture_worker_deployment(docs)["spec"]["template"]["spec"]["containers"][
            0
        ]["env"]
    }
    assert env["MEMORY_DATABASE_URL"] == "postgresql://x/x"
    assert env["MEMORY_HINDSIGHT_URL"] == "http://hindsight.test"
    assert env["MEMORY_CAPTURE_WORKER_ENABLED"] == "true"
    assert "MEMORY_MASTER_KEY_HASH" in env or any(
        item.get("valueFrom") for item in _capture_worker_deployment(docs)["spec"]["template"][
            "spec"
        ]["containers"][0]["env"]
        if item["name"] == "MEMORY_MASTER_KEY_HASH"
    )


def test_the_capture_worker_has_no_public_service_or_ingress():
    docs = _render(
        "--set", "captureWorker.enabled=true",
        "--set", "ingress.enabled=true",
        "--set", "ingress.host=ach.example.com",
    )

    kinds_by_component = {
        (doc.get("kind"), doc.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component"))
        for doc in docs
    }
    assert ("Service", "capture-worker") not in kinds_by_component
    assert ("Ingress", "capture-worker") not in kinds_by_component
    # And the container itself declares no ports -- nothing listens.
    container = _capture_worker_deployment(docs)["spec"]["template"]["spec"]["containers"][0]
    assert "ports" not in container


def test_the_capture_worker_grace_period_exceeds_the_lease():
    docs = _render(
        "--set", "captureWorker.enabled=true", "--set", "captureWorker.leaseSeconds=45",
    )

    spec = _capture_worker_deployment(docs)["spec"]["template"]["spec"]
    assert spec["terminationGracePeriodSeconds"] > 45


def test_helm_lint_passes():
    result = subprocess.run(
        ["helm", "lint", str(CHART), *REQUIRED_SET, "--set", "captureWorker.enabled=true"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# docker-compose.yml: the worker only under the explicit "capture" profile
# ---------------------------------------------------------------------------


def _compose_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env.setdefault("MEMORY_MASTER_KEY_HASH", "unused")
    env.setdefault("HINDSIGHT_LLM_BASE_URL", "http://x")
    env.setdefault("HINDSIGHT_LLM_API_KEY", "x")
    return env


def _compose_services(*extra_args: str) -> list[str]:
    for binary in (["docker", "compose"], ["docker-compose"]):
        result = subprocess.run(
            [*binary, "-f", str(ROOT / "docker-compose.yml"), *extra_args, "config", "--services"],
            capture_output=True, text=True, check=False, env=_compose_env(), cwd=ROOT,
        )
        if result.returncode == 0:
            return result.stdout.split()
    pytest.skip("no working docker compose CLI available")


def test_compose_excludes_the_capture_worker_by_default():
    services = _compose_services()

    assert "capture-worker" not in services


def test_compose_includes_the_capture_worker_under_the_capture_profile():
    services = _compose_services("--profile", "capture")

    assert "capture-worker" in services


def test_compose_config_is_valid_under_the_capture_profile():
    for binary in (["docker", "compose"], ["docker-compose"]):
        result = subprocess.run(
            [*binary, "-f", str(ROOT / "docker-compose.yml"), "--profile", "capture", "config", "--quiet"],
            capture_output=True, text=True, check=False, env=_compose_env(), cwd=ROOT,
        )
        if result.returncode == 0 or "unknown" not in result.stderr.lower():
            assert result.returncode == 0, result.stdout + result.stderr
            return
    pytest.skip("no working docker compose CLI available")
