import subprocess

from experiments.memory_quality.reliability import (
    WORKER_BOUNDARY_TESTS,
    reliability_matrix,
    run_capture_checkpoint_fault,
    run_fault_scenario,
)


def test_reliability_matrix_distinguishes_pre_and_post_ack():
    assert len(reliability_matrix()) == 22
    assert run_fault_scenario("ach_reliability", "lost_ack_after_commit").observed == "recovered_later"
    assert run_fault_scenario("official_reliability", "no_future_host_event").observed == "unrecoverable_without_outbox"


def test_older_checkpoint_is_rejected_as_stale():
    assert run_fault_scenario("ach_reliability", "older_checkpoint").observed == "rejected_as_stale"


def test_real_checkpoint_boundary_retries_lost_ack(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "https://example.invalid/acme/mq55.git"], check=True)
    env = {"MEMORY_CAPTURE_ENABLED": "true", "ACH_MEMORY_API_KEY": "mem_test_key", "ACH_MEMORY_URL": "http://mq55.test", "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"), "HOME": str(tmp_path)}
    event = {"transcript_path": str(__import__("pathlib").Path("tests/fixtures/claude-transcripts/basic.jsonl")), "session_id": "mq55", "cwd": str(tmp_path)}
    result = run_capture_checkpoint_fault("lost_ack_after_commit", hook_event=event, env=env)
    assert result.observed == "recovered_later"
    assert result.requests == 2


def test_worker_fault_matrix_is_bound_to_real_repository_tests():
    assert set(WORKER_BOUNDARY_TESTS) >= {"worker_death_after_extract", "worker_death_after_retain", "expired_lease", "older_checkpoint"}


def test_official_worker_and_checkpoint_faults_are_explicitly_not_applicable():
    expectations = {(item.fault, item.variant): item.expected for item in reliability_matrix()}
    assert expectations[("worker_death_after_extract", "official_reliability")] == "not_applicable"
