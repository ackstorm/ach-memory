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

.PHONY: test
test: ## Unit and API tests (needs Postgres; `make up` provides it)
	uv run pytest -m "not integration" -q

.PHONY: secrets
secrets: ## gitleaks over the git history and the working tree
	docker run --rm -v "$(CURDIR):/repo:ro" zricethezav/gitleaks:latest \
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

.PHONY: release-bump
release-bump: ## Update release metadata (VERSION=X.Y.Z)
	$(require_release_version)
	sed -i -E 's/^version = "[^"]*"$$/version = "$(VERSION)"/' pyproject.toml
	sed -i -E 's/^version: .*/version: $(VERSION)/' deploy/helm/ach-memory/Chart.yaml
	sed -i -E 's/^appVersion: ".*"$$/appVersion: "$(VERSION)"/' deploy/helm/ach-memory/Chart.yaml
	@grep -qx 'version = "$(VERSION)"' pyproject.toml \
		&& grep -qx 'version: $(VERSION)' deploy/helm/ach-memory/Chart.yaml \
		&& grep -qx 'appVersion: "$(VERSION)"' deploy/helm/ach-memory/Chart.yaml \
		|| { echo "FAIL: release metadata was not updated." >&2; exit 1; }

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
	git commit --allow-empty -m "chore(release): v$(VERSION)"
	$(MAKE) verify
	git push origin main

.PHONY: up
up: ## Start the local stack (migrations run before the api serves)
	docker compose up -d --build

.PHONY: smoke
smoke: ## REST + MCP smoke against a running stack (needs MEMORY_MASTER_KEY)
	./scripts/smoke.sh
	uv run python scripts/mcp-smoke.py

