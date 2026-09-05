# v0.4.0 test portfolio cut report

Baseline: integrated pre-Plan-4 `main` at `2cb1753`: 2,240 non-integration tests passed, 4 skipped and 6 live tests deselected in 124.66 seconds.

Retained tests remain grouped under identity/auth/governance; User/Project ownership; typed retain/currentness/expiry; mental-model governance; context delivery; Working State; and host integration, skill and packaging. The implementation branch contains no orphan tests for a non-shipped responsibility.

Current read-only counter is recomputed only after the complete non-live gate is green. Focused delivery, contracts and Working State gates pass (69 tests); the complete portfolio gate remains pending while mixed host/admin tests are being migrated to the current responsibilities. Duplicate clusters should be reviewed by responsibility and surface, not by test count.

Recommended cut: keep the retained portfolio for 0.4.0; any further consolidation is a named 0.4.x review because deleting governance or authorization coverage carries product risk.

PENDING_NONLIVE_GREEN_GATE
