# Deliberately small. Targets exist here only where a command is long enough
# to mistype, or where "the local gate" needs one name -- not to wrap every
# tool this project uses.
.DEFAULT_GOAL := help
SHELL := bash
RELEASE_VERSION_RE := ^[0-9]+\.[0-9]+\.[0-9]+$$

define require_release_version
	@test -n "$(VERSION)" && printf '%s\n' "$(VERSION)" | grep -Eq '$(RELEASE_VERSION_RE)' \
		|| { echo "FAIL: VERSION must be MAJOR.MINOR.PATCH (for example, VERSION=1.2.3)." >&2; exit 1; }
endef

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make \033[36m<target>\033[0m\n"} \
		/^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

.PHONY: lint
lint: ## ruff
	uv run ruff check .

TESTDB_NAME = ach-memory-testdb
TESTDB_PORT ?= 5434

.PHONY: testdb
testdb: ## Start the test Postgres (idempotent, its own port, survives restarts)
	@docker start $(TESTDB_NAME) >/dev/null 2>&1 || \
	  docker run -d --name $(TESTDB_NAME) \
	    -e POSTGRES_USER=memory -e POSTGRES_PASSWORD=memory -e POSTGRES_DB=memory \
	    -p 127.0.0.1:$(TESTDB_PORT):5432 postgres:16-alpine >/dev/null
	@for i in $$(seq 1 30); do \
	  docker exec $(TESTDB_NAME) pg_isready -U memory >/dev/null 2>&1 && exit 0; \
	  sleep 1; \
	done; \
	echo "FAIL: $(TESTDB_NAME) was not ready within 30s." >&2; exit 1

.PHONY: testdb-rm
testdb-rm: ## Remove the test Postgres and its data
	@docker rm -f $(TESTDB_NAME) >/dev/null 2>&1 || true

# Deliberately NOT --rm and deliberately not the compose stack: a throwaway
# container on a shared port is what let the database change identity under a
# running suite. CI overrides MEMORY_TEST_DATABASE_URL and skips this entirely.
.PHONY: test
test: testdb ## Unit and API tests
	uv run pytest -m "not integration" -q

# Pinned, not `:latest`. An unpinned scanner silently changes what the gate
# means: 8.30.1's default ruleset newly flagged four strings that are not
# secrets (a tokenizer version, a project slug, a synthetic canary fixture),
# turning `verify` red on untouched history with no local change to explain
# it. Bump this deliberately and re-read the findings when you do.
GITLEAKS_VERSION = v8.30.1

.PHONY: secrets
secrets: ## gitleaks over the git history and the working tree
	docker run --rm -v "$(CURDIR):/repo:ro" zricethezav/gitleaks:$(GITLEAKS_VERSION) \
		detect --source=/repo --redact --no-banner --config=/repo/.gitleaks.toml

.PHONY: chart
chart: ## helm lint + render, including the must-refuse-without-a-master-key case
	helm lint deploy/helm/ach-memory
	helm template t deploy/helm/ach-memory \
		--set config.databaseUrl=postgresql+psycopg://u:p@h:5432/m \
		--set config.hindsight.url=http://hindsight:8888 \
		--set masterKeySecret.value=deadbeef >/dev/null
	@helm template t deploy/helm/ach-memory --set config.databaseUrl=x \
		--set config.hindsight.url=y >/dev/null 2>&1 \
		&& { echo "FAIL: chart rendered with no master key" >&2; exit 1; } \
		|| echo "chart correctly refuses without a master key"

.PHONY: verify
verify: lint test secrets chart ## The full local gate -- run this before pushing

# Every file that states the version. The plugin manifests were missing here
# and silently drifted to 0.1.0 while the package reached 0.1.2 -- a user
# reading `claude plugin list` was told a version that had not existed for two
# releases.
#
# The version is also the ONLY thing that propagates a plugin change. Hosts
# cache an installed plugin by version, so with an unchanged version both
# `claude plugin update` and `claude plugin marketplace update` report success
# and keep the stale copy. Any change under plugins/ therefore needs a release,
# not just a commit -- see docs/reference/RELEASING.md.
PLUGIN_MANIFESTS = .claude-plugin/marketplace.json \
	plugins/claude-code/.claude-plugin/plugin.json \
	plugins/codex/.codex-plugin/plugin.json

# Every file that pins an install source to a release tag: uvx --from
# git+...@vX.Y.Z. A pin left out of the rewrite keeps installing an old
# release for ever, and nothing else moves it. The SessionStart hook sat on
# v0.4.0 through four releases exactly this way -- it loads standing context
# under `2>/dev/null || true`, so the stale version could only ever show up
# as context that quietly failed to arrive.
TAG_PINNED = plugins/claude-code/.mcp.json \
	plugins/shared/scripts/session-start.sh \
	README.md

.PHONY: release-bump
release-bump: ## Update release metadata (VERSION=X.Y.Z)
	$(require_release_version)
	sed -i -E 's/^version = "[^"]*"$$/version = "$(VERSION)"/' pyproject.toml
	sed -i -E 's/^version: .*/version: $(VERSION)/' deploy/helm/ach-memory/Chart.yaml
	sed -i -E 's/^appVersion: ".*"$$/appVersion: "$(VERSION)"/' deploy/helm/ach-memory/Chart.yaml
	sed -i -E 's/^([[:space:]]*)"version": "[^"]*"/\1"version": "$(VERSION)"/' $(PLUGIN_MANIFESTS)
	# A stale tag in any of these means the new release keeps running the
	# previous one's proxy -- for the claude plugin's install source, for the
	# context load the SessionStart hook shells out to, and for the config
	# README tells a new user to copy.
	sed -i -E 's|(ach-memory@v)[0-9]+\.[0-9]+\.[0-9]+|\1$(VERSION)|' $(TAG_PINNED)
	# uv.lock names the root package too. Left out, v0.2.0 was tagged with a
	# lockfile still saying 0.1.2, and `uv run --frozen` reports that stale
	# version through importlib.metadata -- which is what `ach-memory init`
	# prints back to the user.
	uv lock
	@grep -qx 'version = "$(VERSION)"' pyproject.toml \
		&& grep -qx 'version: $(VERSION)' deploy/helm/ach-memory/Chart.yaml \
		&& grep -qx 'appVersion: "$(VERSION)"' deploy/helm/ach-memory/Chart.yaml \
		|| { echo "FAIL: release metadata was not updated." >&2; exit 1; }
	@for manifest in $(PLUGIN_MANIFESTS); do \
		grep -qE '^[[:space:]]*"version": "$(VERSION)"' "$$manifest" \
			|| { echo "FAIL: $$manifest was not updated." >&2; exit 1; }; \
	done
	@for pinned in $(TAG_PINNED); do \
		grep -q 'ach-memory@v$(VERSION)' "$$pinned" \
			|| { echo "FAIL: $$pinned still pins an old release tag." >&2; exit 1; }; \
	done
	@grep -qx 'version = "$(VERSION)"' uv.lock \
		|| { echo "FAIL: uv.lock was not updated." >&2; exit 1; }

.PHONY: release-cut
release-cut: ## Create and push the release marker (VERSION=X.Y.Z)
	$(require_release_version)
	@test "$$(git rev-parse --abbrev-ref HEAD)" = "main" \
		|| { echo "FAIL: release-cut must run on main." >&2; exit 1; }
	@test -z "$$(git status --porcelain)" \
		|| { echo "FAIL: release-cut requires a clean tree." >&2; exit 1; }
	@grep -qx 'version = "$(VERSION)"' pyproject.toml \
		&& grep -qx 'version: $(VERSION)' deploy/helm/ach-memory/Chart.yaml \
		&& grep -qx 'appVersion: "$(VERSION)"' deploy/helm/ach-memory/Chart.yaml \
		|| { echo "FAIL: run make release-bump VERSION=$(VERSION) and commit its changes first." >&2; exit 1; }
	@for manifest in $(PLUGIN_MANIFESTS); do \
		grep -qE '^[[:space:]]*"version": "$(VERSION)"' "$$manifest" \
			|| { echo "FAIL: run make release-bump VERSION=$(VERSION) and commit its changes first." >&2; exit 1; }; \
	done
	# verify BEFORE the marker commit. The marker is empty, so the tree verify
	# inspects is identical either way -- but committing first left a stray
	# `chore(release): vX` on local main every time verify failed, which then
	# tripped the clean-tree and metadata gates on the next attempt. Nothing
	# was ever pushed (push is last), so this only ever cost manual cleanup.
	$(MAKE) verify
	git commit --allow-empty -m "chore(release): v$(VERSION)"
	git push origin main

.PHONY: up
up: ## Start the local stack (migrations run before the api serves)
	docker compose up -d --build

.PHONY: smoke
smoke: ## REST + MCP smoke against a running stack (needs MEMORY_MASTER_KEY)
	./scripts/smoke.sh
	uv run python scripts/mcp-smoke.py

.PHONY: e2e
e2e: ## Isolated full E2E with MockLLM (no external LLM or credentials)
	./scripts/e2e-compose.sh

.PHONY: bench
bench: ## Capability differential vs vanilla Hindsight (MockLLM, deterministic)
	./scripts/bench-compose.sh scripts/bench.py

# Not part of `verify`, and never will be: it spends real model tokens and
# its numbers move between runs. Quality is reported as mean +/- stdev over
# BENCH_REPEATS runs precisely because a single run of an LLM-backed
# retrieval benchmark is an anecdote.
.PHONY: bench-quality
bench-quality: ## Retrieval quality vs vanilla Hindsight (REAL LLM, costs tokens)
	BENCH_REAL_LLM=1 ./scripts/bench-compose.sh scripts/bench_quality.py
