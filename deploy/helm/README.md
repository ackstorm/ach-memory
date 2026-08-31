# ach-memory Helm chart

## Before you install

This chart is published publicly (`oci://ghcr.io/ackstorm/charts`); the
service's source repository is not.

- `image.repository` defaults to `ghcr.io/ackstorm/ach-memory`, this
  organisation's registry -- no override needed to install from ackstorm.
  Anyone reusing this chart elsewhere must set
  `--set image.repository=<their registry>/ach-memory`.
- `masterKeySecret.name` (an existing Secret) is the **recommended** way to
  supply `MEMORY_MASTER_KEY_HASH`. `masterKeySecret.value` puts the hash in
  your values file -- fine for a local trial, wrong for anything shared. That
  credential reaches every bank in the tenant.
- The chart runs the service only. Postgres and Hindsight are dependencies you
  point it at; an in-chart database is how test data ends up in production.
- Licence: `MIT`.
- GHCR visibility flip (Task 12, Step 5b): pending -- occurs after this
  repository's first push and first tagged release. Record the date here once
  `ach-memory` and `charts/ach-memory` are confirmed pullable with no
  credentials.

Ships the `ach-memory` service only. Postgres and Hindsight are dependencies
you point it at (`config.databaseUrl`, `config.hindsight.url`) — this chart
does not run either, so no test data ever ends up in a production database by
accident of `helm install`.

## Install

```bash
helm install ach-memory deploy/helm/ach-memory \
  --set config.databaseUrl=postgresql+psycopg://memory:memory@postgres:5432/memory \
  --set config.hindsight.url=http://hindsight:8888 \
  --set masterKeySecret.name=mem-master-key
```

`image.repository` defaults to `ghcr.io/ackstorm/ach-memory`, this
organisation's registry, so the install above works with no override.
Anyone reusing this chart outside ackstorm must set `--set
image.repository=<their registry>/ach-memory`.

`masterKeySecret.name` must reference an existing `Secret` in the target
namespace containing the key `master-key-hash` (configurable via
`masterKeySecret.key`) — e.g.:

```bash
MASTER_HASH=$(python3 -c \
  "import hashlib,os; print(hashlib.sha256(os.environ['MEMORY_MASTER_KEY'].encode()).hexdigest())")
kubectl create secret generic mem-master-key --from-literal=master-key-hash="$MASTER_HASH"
```

Alternatively, set `masterKeySecret.value` to have the chart create the
`Secret` for you from a value passed on the command line — still never
committed to `values.yaml`. **Rendering fails if you set neither**:
`MEMORY_MASTER_KEY_HASH` is the credential that reaches every bank in the
tenant, so there is no default for it, silent or otherwise.

## MEMORY_MCP_ALLOWED_HOSTS — read this before enabling Ingress

The MCP SDK's DNS-rebinding guard matches the incoming `Host` header
**literally, including the port when it is non-default**. If the hostname (or
`host:port`) a client actually sends does not appear in
`MEMORY_MCP_ALLOWED_HOSTS`, every MCP call gets `421 Misdirected Request`
while every REST call keeps working fine — this looks exactly like an MCP bug
and is not one; it has already cost real debugging time in this project's own
compose setup (see `docker-compose.yml`).

`config.mcpAllowedHosts` defaults from `ingress.host` when
`ingress.enabled=true` and no explicit list is set. If you front the service
any other way (a `NodePort`, a different Ingress per environment, a
non-standard port on the same host), set `config.mcpAllowedHosts` explicitly
to the exact host clients will send — including the port if it is not 80/443.

## The rate limiter is in-process

See the comment beside `replicaCount` in `values.yaml`. `config.writeLimit` /
`config.writeWindowSeconds` are enforced per pod, not per Deployment: running
`replicaCount: 5` with the default `writeLimit: 60` gives an effective ceiling
of 300 writes per window, not 60. Lower `writeLimit` when you scale up if you
want to keep the same effective ceiling. There is no distributed rate limiter
in this build — see `docs/PROJECT-STATE.md`.

## Migrations

`templates/migration-job.yaml` runs `python -m alembic upgrade head` as a
`pre-install,pre-upgrade` Helm hook Job (`migration.enabled`, default `true`).
Hook Jobs run to completion before Helm applies the rest of the release, so
the schema is current before the Deployment's pods are ever created — not a
race with it.

## Probes

No dedicated health-check route ships in this build. Both probes hit `/docs`
(FastAPI's built-in Swagger UI page, no auth required) — the same
unauthenticated signal `scripts/smoke.sh` already polls to know the API is
serving. It proves the process is up and answering HTTP; it does not check
database or Hindsight connectivity.

## Validate

`config.databaseUrl` and `config.hindsight.url` are `required(...)` in
`templates/deployment.yaml` and `templates/migration-job.yaml` — set them on
every `helm template`/`helm install`, the same way `## Install` above does,
or rendering fails with `execution error` instead of producing anything to
inspect.

```bash
helm lint deploy/helm/ach-memory \
  --set config.databaseUrl=postgresql+psycopg://memory:memory@postgres:5432/memory \
  --set config.hindsight.url=http://hindsight:8888 \
  --set masterKeySecret.name=mem-secret
helm template ach-memory deploy/helm/ach-memory \
  --set config.databaseUrl=postgresql+psycopg://memory:memory@postgres:5432/memory \
  --set config.hindsight.url=http://hindsight:8888 \
  --set masterKeySecret.name=mem-secret
helm template ach-memory deploy/helm/ach-memory \
  --set config.databaseUrl=postgresql+psycopg://memory:memory@postgres:5432/memory \
  --set config.hindsight.url=http://hindsight:8888 \
  --set masterKeySecret.name=mem-secret --set replicaCount=3
```

Rendering with neither `masterKeySecret.name` nor `masterKeySecret.value` set
must fail, not silently produce a Deployment referencing a Secret that does
not exist.

## MEMORY_AUTH_JWT_ISSUER — read this before pointing JWKS in-cluster

`config.auth.jwt.issuer` and `config.auth.jwt.jwksUri` look interchangeable
and are not. The issuer is **compared to the token's `iss` claim**; the JWKS
URI is only **where the signing keys are fetched from**.

ACH sets `iss` to its own `ACH_BASE_URL` verbatim, which is the public URL. So
the tempting optimization — repointing `issuer` at `http://ach.ach.svc` to keep
key fetches inside the cluster — rejects **every** token, because `iss` no
longer matches what the issuer actually stamped. The symptom is a uniform 401
with `token rejected` and nothing in the JWKS logs to explain it, since the
fetch succeeded.

Keep `issuer` public and set `jwksUri` separately:

```yaml
config:
  auth:
    jwt:
      enabled: true
      issuer: https://ach.example.com                              # matches `iss`
      jwksUri: http://ach.ach.svc/.well-known/jwks.json            # where keys come from
      audience: mcp:ach-memory
```

Leave `jwksUri` empty and it derives as `<issuer>/.well-known/jwks.json`, which
is right for ACH but not for Dex — Dex publishes its keys at `/keys`.

Neither URL has to be HTTPS. In-cluster service URLs are the expected shape and
nothing refuses them; the service logs one startup warning per plaintext URL so
a *public* hostname reached over `http` by mistake is visible rather than
silent.

## The capture worker is not something `helm install` turns on

`captureWorker.enabled` renders the Deployment; `captureWorker.workerEnabled`
(`MEMORY_CAPTURE_WORKER_ENABLED`) is whether the process inside it ever leases
a row. Both default `false`. Turning `enabled` on with `workerEnabled` still
`false` stages the rollout — image pulled, database/Hindsight config wired,
pod healthy — without leasing anything, which is as far as this chart carries
you on its own.

Enabling either flag is not production enablement by itself. The full rollout
this chart is one step of, in order, is:

1. Run the pending-install migration (this chart's `migration-job.yaml`
   already does this on every install/upgrade).
2. Run `ach-memory capture-check --scope ... --project ...` (read-only; never
   mutates config or memory) against the target bank(s) and confirm it
   reports OK.
3. Review the config diff `capture-check` prints and apply it through
   whatever change process the target Hindsight deployment requires — this
   chart has no PATCH lever for it, on purpose.
4. Stage an isolated canary worker (`captureWorker.enabled: true`,
   `workerEnabled: true`, pointed at a single test bank or a low-traffic
   tenant) and watch it process real checkpoints.
5. Replay-test: submit the same checkpoint twice and confirm no duplicate
   evidence, then crash/restart the canary mid-slice and confirm it resumes
   without re-filing anything already filed.
6. Only then register the Claude Code hooks broadly (the plugin's
   `capture-checkpoint.sh` on `Stop`/`PreCompact`) so checkpoints actually
   start arriving.

This is deliberately not a single command. The separate, explicit approval
and exact mutation procedure for steps 2–3 belongs to a later phase — this
chart stages the plumbing for the whole sequence, not the decision to run it.
