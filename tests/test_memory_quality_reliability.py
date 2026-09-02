from experiments.memory_quality.reliability import reliability_matrix, run_fault_scenario


def test_reliability_matrix_distinguishes_pre_and_post_ack():
    assert len(reliability_matrix()) == 22
    assert run_fault_scenario("ach_reliability", "lost_ack_after_commit").observed == "recovered_later"
    assert run_fault_scenario("official_reliability", "no_future_host_event").observed == "unrecoverable_without_outbox"


def test_older_checkpoint_is_rejected_as_stale():
    assert run_fault_scenario("ach_reliability", "older_checkpoint").observed == "rejected_as_stale"
