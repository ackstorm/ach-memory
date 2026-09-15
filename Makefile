SHELL := bash
RELEASE_VERSION_RE := ^[0-9]+\.[0-9]+\.[0-9]+$$

define require_release_version
	@test -n "$(VERSION)" && printf '%s\n' "$(VERSION)" | grep -Eq '$(RELEASE_VERSION_RE)' \
		|| { echo "FAIL: VERSION must be MAJOR.MINOR.PATCH (for example, VERSION=1.2.3)." >&2; exit 1; }
endef

.PHONY: lint
lint: # ruff
	uv run ruff check .

TESTDB_NAME = ach-memory-testdb
TESTDB_PORT ?= 5434

.PHONY: testdb
testdb: # Start the test Postgres (idempotent, its own port, survives restarts)
	@docker start $(TESTDB_NAME) >/dev/null 2>&1 || \
	  docker run -d --name $(TESTDB_NAME) \
	    -e POSTGRES_USER=memory -e POSTGRES_PASSWORD=memory -e POSTGRES_DB=memory \
	    -p 127.0.0.1:$(TESTDB_PORT):5432 postgres:17-alpine >/dev/null
	@for i in $$(seq 1 30); do \
	  docker exec $(TESTDB_NAME) pg_isready -U memory >/dev/null 2>&1 && exit 0; \
	  sleep 1; \
	done; \
	echo "FAIL: $(TESTDB_NAME) was not ready within 30s." >&2; exit 1

.PHONY: testdb-rm
testdb-rm: # Remove the test Postgres and its data
	@docker rm -f $(TESTDB_NAME) >/dev/null 2>&1 || true

.PHONY: test
test: testdb # Unit and API tests
	uv run pytest -q

GITLEAKS_VERSION = v8.30.1

.PHONY: secrets
secrets: # gitleaks over the git history and the working tree (mounts .git: a worktree checkout scans zero commits otherwise)
	docker run --rm -v "$(CURDIR):/repo:ro" zricethezav/gitleaks:$(GITLEAKS_VERSION) \
		detect --source=/repo --redact --no-banner --config=/repo/.gitleaks.toml

.PHONY: chart
chart: # helm lint + render, and pyproject/package/Chart.yaml versions agree (with VERSION, or each other)
	helm lint deploy/helm/ach-memory
	helm template t deploy/helm/ach-memory \
		--set config.databaseUrl=postgresql+psycopg://u:p@h:5432/m \
		--set config.hindsight.url=http://hindsight:8888 >/dev/null
	@v=$${VERSION:-$$(sed -n 's/^version = "\(.*\)"$$/\1/p' pyproject.toml)}; \
	grep -qx "version = \"$$v\"" pyproject.toml \
		&& grep -qx "__version__ = \"$$v\"" src/memory/__init__.py \
		&& grep -qx "version: $$v" deploy/helm/ach-memory/Chart.yaml \
		&& grep -qx "appVersion: \"$$v\"" deploy/helm/ach-memory/Chart.yaml \
		&& grep -qx "version = \"$$v\"" uv.lock \
		&& grep -q "\"version\": \"$$v\"" .claude-plugin/marketplace.json plugins/claude-code/.claude-plugin/plugin.json plugins/codex/.codex-plugin/plugin.json \
		&& ! grep -rL "@v$$v" plugins/claude-code/.mcp.json plugins/codex/.mcp.json plugins/shared/scripts/session-start.sh plugins/shared/scripts/pre-compact.sh | grep -q . \
		|| { echo "FAIL: release metadata does not agree on $$v." >&2; exit 1; }

PLUGIN_HOSTS = plugins/claude-code plugins/codex plugins/opencode plugins/pi
.PHONY: plugins plugins-check
plugins: # Sync plugins/shared/* into each host plugin (idempotent)
	@for h in $(PLUGIN_HOSTS); do \
		mkdir -p $$h/skills/ach-memory $$h/scripts; \
		cp plugins/shared/ach-memory/SKILL.md $$h/skills/ach-memory/SKILL.md; \
		cp plugins/shared/activation.txt plugins/shared/activation.subagent.json $$h/; \
		cp plugins/shared/scripts/session-start.sh plugins/shared/scripts/subagent-start.sh $$h/scripts/; \
	done
	cp plugins/shared/scripts/pre-compact.sh plugins/claude-code/scripts/pre-compact.sh
plugins-check: # Fail if a committed plugin copy drifted from plugins/shared
	@for h in $(PLUGIN_HOSTS); do \
		cmp -s plugins/shared/ach-memory/SKILL.md $$h/skills/ach-memory/SKILL.md \
		&& cmp -s plugins/shared/activation.txt $$h/activation.txt \
		&& cmp -s plugins/shared/activation.subagent.json $$h/activation.subagent.json \
		&& cmp -s plugins/shared/scripts/session-start.sh $$h/scripts/session-start.sh \
		&& cmp -s plugins/shared/scripts/subagent-start.sh $$h/scripts/subagent-start.sh \
		|| { echo "FAIL: $$h drifted from plugins/shared -- run 'make plugins'." >&2; exit 1; }; \
	done
	@cmp -s plugins/shared/scripts/pre-compact.sh plugins/claude-code/scripts/pre-compact.sh \
		|| { echo "FAIL: claude-code pre-compact.sh drifted -- run 'make plugins'." >&2; exit 1; }

.PHONY: verify
verify: lint test secrets chart plugins-check # The full local gate -- run this before pushing

.PHONY: release-bump
release-bump: # Update release metadata (VERSION=X.Y.Z)
	$(require_release_version)
	sed -i -E 's/^version = "[^"]*"$$/version = "$(VERSION)"/' pyproject.toml
	sed -i -E 's/^__version__ = "[^"]*"$$/__version__ = "$(VERSION)"/' src/memory/__init__.py
	sed -i -E 's/^version: .*/version: $(VERSION)/' deploy/helm/ach-memory/Chart.yaml
	sed -i -E 's/^appVersion: ".*"$$/appVersion: "$(VERSION)"/' deploy/helm/ach-memory/Chart.yaml
	sed -i -E 's/"version": "[^"]*"/"version": "$(VERSION)"/' .claude-plugin/marketplace.json plugins/claude-code/.claude-plugin/plugin.json plugins/codex/.codex-plugin/plugin.json
	sed -i -E 's/@v[0-9]+\.[0-9]+\.[0-9]+/@v$(VERSION)/g' plugins/claude-code/.mcp.json plugins/codex/.mcp.json plugins/shared/scripts/session-start.sh plugins/shared/scripts/pre-compact.sh docs/hosts.md README.md
	$(MAKE) plugins
	uv lock
	$(MAKE) chart VERSION=$(VERSION)

.PHONY: release-cut
release-cut: # Create and push the release marker (VERSION=X.Y.Z)
	$(require_release_version)
	@test "$$(git rev-parse --abbrev-ref HEAD)" = "main" \
		|| { echo "FAIL: release-cut must run on main." >&2; exit 1; }
	@test -z "$$(git status --porcelain)" \
		|| { echo "FAIL: release-cut requires a clean tree." >&2; exit 1; }
	$(MAKE) chart VERSION=$(VERSION)
	$(MAKE) verify
	git commit --allow-empty -m "chore(release): v$(VERSION)"
	git tag -a "v$(VERSION)" -m "v$(VERSION)"
	git push origin main "v$(VERSION)"

.PHONY: up
up: # Start the local stack (migrations run before the api serves)
	docker compose up -d --build

.PHONY: e2e
e2e: # Full local gate: build, wait for health, run the mcp smoke test; always tears the stack down
	@trap 'docker compose down' EXIT; \
	docker compose up -d --build; \
	ok=; \
	for i in $$(seq 1 60); do \
	  curl -sf http://localhost:8000/health >/dev/null 2>&1 && { ok=1; break; }; \
	  sleep 1; \
	done; \
	[ -n "$$ok" ] || { echo "FAIL: API never became healthy within 60s." >&2; exit 1; }; \
	uv run python scripts/mcp-smoke.py --url http://localhost:8000/mcp/ --key alice --header Authorization
