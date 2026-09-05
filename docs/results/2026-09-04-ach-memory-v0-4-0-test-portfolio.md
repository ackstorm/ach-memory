# v0.4.0 test portfolio cut report

Baseline: integrated pre-Plan-4 `main` at `2cb1753`: 2,240 non-integration tests passed, 4 skipped and 6 live tests deselected in 124.66 seconds.

Removed tests and files belong to the retired capture, profile and memory-quality/INDEX-FULL product paths. Retained tests remain grouped under identity/auth/governance; User/Project ownership; typed retain/currentness/expiry; mental-model governance; context delivery; Working State; and host integration, skill and packaging.

The explicit retired-test deletion boundary was used; no retained-product tests were removed merely to lower the count. A follow-up counter should record current `src/memory` and `tests` file/line totals after the non-live release gate. Orphan tests with no retained surface: zero expected after `test_removed_product_surface.py` passes. Duplicate clusters should be reviewed by responsibility and surface, not by test count.

Recommended cut: keep retained tests for 0.4.0; any further consolidation is a named 0.4.x review because deleting governance or authorization coverage carries product risk.

READY_FOR_TEST_PORTFOLIO_CUT_REVIEW
