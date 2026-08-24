from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "e2e-compose.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fake_commands(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
set -eu
base_kind=external
key_kind=external
if [ "${HINDSIGHT_LLM_BASE_URL-}" = "http://127.0.0.1:9" ]; then base_kind=local; fi
if [ "${HINDSIGHT_LLM_API_KEY-}" = "e2e-mock-not-a-secret" ]; then key_kind=mock; fi
printf 'docker|%s|api_port=%s|hindsight_port=%s|postgres_port=%s\n' \
  "$*" "${MEMORY_API_PORT-}" "${MEMORY_HINDSIGHT_PORT-}" \
  "${MEMORY_POSTGRES_PORT-}" >>"$CALL_LOG"
printf 'llm|provider=%s|model=%s|base=%s|key=%s\n' \
  "${HINDSIGHT_LLM_PROVIDER-}" "${HINDSIGHT_LLM_MODEL-}" \
  "$base_kind" "$key_kind" >>"$CALL_LOG"
case "$*" in
  *" port api 8000") printf '127.0.0.1:49152\n' ;;
  *" port hindsight 8888") printf '127.0.0.1:49153\n' ;;
esac
""",
    )
    _write_executable(
        bin_dir / "uv",
        """#!/usr/bin/env bash
set -eu
printf 'uv|%s|API=%s|HINDSIGHT_URL=%s|master=%s\n' \
  "$*" "${API-}" "${HINDSIGHT_URL-}" "${MEMORY_MASTER_KEY:+set}" >>"$CALL_LOG"
if [ "${E2E_BLOCK-0}" = 1 ]; then
  : >"$E2E_READY_FILE"
  trap 'exit 143' TERM
  while :; do sleep 1; done
fi
exit "${E2E_EXIT-0}"
""",
    )
    return bin_dir, log


def _runner_env(tmp_path: Path, **extra: str) -> tuple[dict[str, str], Path]:
    bin_dir, log = _fake_commands(tmp_path)
    env = os.environ.copy()
    for name in (
        "HINDSIGHT_LLM_PROVIDER",
        "HINDSIGHT_LLM_MODEL",
        "HINDSIGHT_LLM_BASE_URL",
        "HINDSIGHT_LLM_API_KEY",
    ):
        env.pop(name, None)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CALL_LOG": str(log),
        }
    )
    env.update(extra)
    return env, log


def _lines(log: Path) -> list[str]:
    return log.read_text().splitlines()


def _project(line: str) -> str:
    fields = line.split("|")[1].split()
    return fields[fields.index("-p") + 1]


def test_runner_uses_random_loopback_ports_and_removes_its_project(tmp_path: Path) -> None:
    env, log = _runner_env(
        tmp_path,
        HINDSIGHT_LLM_PROVIDER="openai",
        HINDSIGHT_LLM_MODEL="real-model",
        HINDSIGHT_LLM_BASE_URL="https://external-llm.invalid",
        HINDSIGHT_LLM_API_KEY="must-not-be-used",
    )

    result = subprocess.run(["bash", str(RUNNER)], cwd=ROOT, env=env, check=False)

    assert result.returncode == 0
    lines = _lines(log)
    docker_lines = [line for line in lines if line.startswith("docker|")]
    llm_lines = [line for line in lines if line.startswith("llm|")]
    assert f"compose -f {ROOT / 'docker-compose.yml'} -p " in docker_lines[0]
    assert docker_lines[0].endswith("api_port=0|hindsight_port=0|postgres_port=0")
    assert set(llm_lines) == {"llm|provider=mock|model=mock-model|base=local|key=mock"}
    assert "must-not-be-used" not in log.read_text()
    assert " up -d --build --wait" in docker_lines[0]
    assert any(" exec -T api python -c " in line for line in docker_lines)
    assert any(
        line
        == "uv|run python scripts/e2e.py|API=http://127.0.0.1:49152|"
        "HINDSIGHT_URL=http://127.0.0.1:49153|master=set"
        for line in lines
    )
    assert " down -v --remove-orphans" in docker_lines[-1]
    projects = {_project(line) for line in docker_lines}
    assert len(projects) == 1
    assert next(iter(projects)).startswith("ach-memory-e2e-")


def test_runner_preserves_e2e_failure_and_still_tears_down(tmp_path: Path) -> None:
    env, log = _runner_env(tmp_path, E2E_EXIT="23")

    result = subprocess.run(["bash", str(RUNNER)], cwd=ROOT, env=env, check=False)

    assert result.returncode == 23
    docker_lines = [line for line in _lines(log) if line.startswith("docker|")]
    assert " down -v --remove-orphans" in docker_lines[-1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal contract")
def test_runner_tears_down_when_terminated(tmp_path: Path) -> None:
    ready = tmp_path / "e2e-ready"
    env, log = _runner_env(
        tmp_path,
        E2E_BLOCK="1",
        E2E_READY_FILE=str(ready),
    )
    process = subprocess.Popen(
        ["bash", str(RUNNER)],
        cwd=ROOT,
        env=env,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "runner never reached the E2E command"

        os.killpg(process.pid, signal.SIGTERM)
        assert process.wait(timeout=10) == 143
        docker_lines = [line for line in _lines(log) if line.startswith("docker|")]
        assert " down -v --remove-orphans" in docker_lines[-1]
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def _compose_config(env_overrides: dict[str, str]) -> dict:
    if subprocess.run(
        ["docker", "compose", "version"], capture_output=True, check=False
    ).returncode:
        pytest.skip("docker compose is unavailable")
    env = os.environ.copy()
    for name in (
        "MEMORY_POSTGRES_PORT",
        "MEMORY_HINDSIGHT_PORT",
        "MEMORY_API_PORT",
        "HINDSIGHT_LLM_PROVIDER",
        "HINDSIGHT_LLM_MODEL",
    ):
        env.pop(name, None)
    env.update(
        {
            "MEMORY_MASTER_KEY_HASH": "test-only-hash",
            "HINDSIGHT_LLM_BASE_URL": "https://llm.invalid",
            "HINDSIGHT_LLM_API_KEY": "test-only-key",
            **env_overrides,
        }
    )
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _published_ports(env_overrides: dict[str, str]) -> dict[int, str]:
    config = _compose_config(env_overrides)
    return {
        port["target"]: port["published"]
        for service in ("postgres", "hindsight", "api")
        for port in config["services"][service]["ports"]
    }


def test_compose_ports_keep_development_defaults_and_allow_random_publication() -> None:
    assert _published_ports({}) == {5432: "5433", 8888: "8888", 8000: "8000"}
    assert _published_ports(
        {
            "MEMORY_POSTGRES_PORT": "0",
            "MEMORY_HINDSIGHT_PORT": "0",
            "MEMORY_API_PORT": "0",
        }
    ) == {5432: "0", 8888: "0", 8000: "0"}


def test_compose_defaults_to_openai_but_allows_the_e2e_mock_provider() -> None:
    normal = _compose_config({})["services"]["hindsight"]["environment"]
    assert normal["HINDSIGHT_API_LLM_PROVIDER"] == "openai"

    mocked = _compose_config(
        {
            "HINDSIGHT_LLM_PROVIDER": "mock",
            "HINDSIGHT_LLM_MODEL": "mock-model",
        }
    )["services"]["hindsight"]["environment"]
    assert mocked["HINDSIGHT_API_LLM_PROVIDER"] == "mock"
    assert mocked["HINDSIGHT_API_LLM_MODEL"] == "mock-model"
