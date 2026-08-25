# Releasing

```bash
make release-bump VERSION=X.Y.Z   # rewrite every file that states the version
git commit -am "chore(release): bump metadata to X.Y.Z"
make release-cut VERSION=X.Y.Z    # verify, then the marker commit, then push
```

`release-cut` runs `make verify` **before** creating the marker commit, so a
failed gate leaves nothing behind to clean up. It refuses to run off `main`,
with a dirty tree, or with release metadata that does not already match
`VERSION`.

The marker commit is the trigger: `.github/workflows/release.yml` fires on a
push to `main` whose head commit message starts with `chore(release): v`.
Nothing is tagged locally — the workflow creates the tag, the GitHub release,
the image and the chart.

## A change under `plugins/` needs a release, not just a commit

The version is the only thing that propagates a plugin change. Agent hosts
cache an installed plugin by version, so with the version unchanged both

```bash
claude plugin update ach-memory@ach-memory
claude plugin marketplace update ach-memory
```

report success, keep the stale copy, and the change reaches nobody who already
installed. Measured: a plugin kept running a hook set two commits old while
reporting itself up to date.

Recovering without a bump means

```bash
claude plugin uninstall ach-memory@ach-memory
rm -rf ~/.claude/plugins/cache/ach-memory
claude plugin install -y --scope user ach-memory@ach-memory
```

which is not something to ask users to do. Cut a release instead.

## Every file that states the version

| file | field |
| --- | --- |
| `pyproject.toml` | `version` |
| `deploy/helm/ach-memory/Chart.yaml` | `version`, `appVersion` |
| `.claude-plugin/marketplace.json` | `plugins[0].version` |
| `plugins/claude-code/.claude-plugin/plugin.json` | `version` |
| `plugins/codex/.codex-plugin/plugin.json` | `version` |

`release-bump` rewrites all of them; the JSON ones are listed in the Makefile as
`PLUGIN_MANIFESTS`. Two tests keep this honest: one asserts every file above
states pyproject's version, and one scans the tree for manifests carrying a
`version` field and fails on any that `release-bump` does not know about — the
drift that put the plugin manifests two releases behind started exactly that
way.
