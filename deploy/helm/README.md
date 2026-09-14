# ach-memory Helm chart

## Install

```bash
helm install ach-memory deploy/helm/ach-memory \
  --set config.databaseUrl=postgresql+psycopg://memory:memory@postgres:5432/memory \
  --set config.hindsight.url=http://hindsight:8888
```

`image.repository` defaults to `ghcr.io/ackstorm/ach-memory`; override with
`--set image.repository=<registry>/ach-memory` elsewhere. That install grants
nobody operator authority — the safe default; ordinary callers don't need it.

## Upgrade / grant an operator

```bash
helm upgrade ach-memory deploy/helm/ach-memory --reuse-values \
  --set master.users=juancarlos@example.com \
  --set master.issuer=https://idp.example.com
```

`master.users`/`master.groups` name identities your IdP asserts, not secrets.
Both default to empty; `master.issuer` is required once more than one auth
provider is enabled.

## Values that matter

- `config.databaseUrl`, `config.hindsight.url` — `required(...)`, must be set
  on every `helm template`/`install`.
- `config.mcpAllowedHosts` — the MCP SDK's Host guard is literal, port
  included; set unless `ingress.host` already covers it.
- `config.auth.jwt.issuer` must match the token's `iss` claim exactly, even
  when `jwksUri` points in-cluster.

## Flux roll

Patch the `HelmRelease`'s `spec.chart.spec.version`, then
`flux reconcile helmrelease ach-memory --with-source`. GitOps re-pins the
value afterwards.
