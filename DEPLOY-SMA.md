# DEPLOY-SMA — Backrest

> Source-build, seed, reset, and browser-evidence notes for the `backrest`
> RL environment.
>
> - Upstream: <https://github.com/garethgeorge/backrest>
> - Baseline: release tag `v1.14.1` = `875c9cb51e626a75276a64da42c6a9ec0695010a`
>   (2026-07-11; latest stable Release, published 2026-07-12)
> - Fork: <https://github.com/sma-rl-dev/backrest>, branch `tester-env-baseline`
> - Baseline delta vs upstream `v1.14.1`: only tester-env files —
>   `tester-env`, `tester-env-seed.py`, `docker-compose.tester-env.yml`,
>   `Dockerfile.tester-env`, `.tester-env-fixtures/`, and this file. No upstream
>   source, dependency, or build file is modified.

## Quick Start

```bash
./tester-env deploy    # Build image from editable source, start, run the seed
./tester-env verify    # Assert seeded state (login gate, repos, plans, snapshots, operations, dashboard)
./tester-env reset     # Stop + remove container, network and the four named volumes
./tester-env status    # RUNNING / NOT RUNNING + URLs
./tester-env logs      # Follow container logs
```

`deploy` is idempotent-ish: it rebuilds from the working tree, recreates the
container if needed, and then runs the idempotent seed. A second `seed` on a
running stack is a no-op.

## Build

Three-stage fork-owned `Dockerfile.tester-env` (upstream only ships
goreleaser-fed `Dockerfile.alpine` / `Dockerfile.scratch`, which `COPY` a
prebuilt binary and cannot be used for mutation scenarios):

1. `node:24-bookworm-slim` + pnpm `9.15.9`: `pnpm install --frozen-lockfile`
   (keyed on `webui/package.json` + `webui/pnpm-lock.yaml`, BuildKit pnpm-store
   cache mount), then `pnpm run build` (`vite build`) → `webui/dist`.
2. `golang:1.26`: `go mod download` (keyed on `go.mod` + `go.sum`), then
   `CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags "-s -w -X main.version=v1.14.1" ./cmd/backrest`
   with `webui/dist` copied in for `//go:embed dist`. `-trimpath -buildvcs=false`
   keeps the binary reproducible so pass variants of one baseline collapse into
   one content-addressed image.
3. `alpine:3.22` runtime with restic `0.19.1` downloaded from the GitHub release,
   SHA256-verified (amd64 `f4154156…`, arm64 `a5f64aaa…`) and version-asserted at
   image-build time, plus `tini`, `ca-certificates`, `tzdata`.

```bash
docker build -t tester-env-backrest:dev -f Dockerfile.tester-env .
```

Measured on this host (28 cores, warm network): cold `./tester-env deploy`
(build + start + seed) 1m09s; Go-only source edit rebuild 6.7s; webui (vite)
source edit rebuild 14.2s; warm reset + deploy + verify 6.5s.

## Run

`docker-compose.tester-env.yml` starts one service; there is no database,
worker, cache, or broker.

```bash
docker compose -p tester-env-backrest -f docker-compose.tester-env.yml up -d --build
```

- Local URL: `http://localhost:9898/` (host port from `--port` / `PORT`).
- Browser-visible URL for containerised browsers:
  `http://host.docker.internal:9898/` (`TESTER_ENV_BROWSER_ORIGIN` wins).
- Container env: `BACKREST_PORT=0.0.0.0:9898`, `BACKREST_DATA=/data`,
  `BACKREST_CONFIG=/config/config.json`, `XDG_CACHE_HOME=/cache`, `TMPDIR=/tmp`,
  `BACKREST_RESTIC_COMMAND=/usr/local/bin/restic`, `TZ=UTC`.
- Named volumes: `backrest-config` → `/config` (config.json, JWT signing key,
  SSH keys), `backrest-data` → `/data` (oplog.sqlite, process logs),
  `backrest-cache` → `/cache` (restic cache), `backrest-repos` → `/repos`
  (local restic repositories). `/fixtures` and `/restore` are image paths.
- Image naming: `IMAGE_TAG` (set by `scripts/rl-env` to a content-addressed tag)
  names the image; `RUN_ID` only suffixes the container, compose project and
  volumes, never the image.
- No app URL configuration is needed: the SPA is built with base `./` and calls
  the API relatively, so it works on any host/port.

## Credentials

- UI login: `admin` / `BackrestAdmin!2026` (single user type, no roles).
- Auth is disabled in a fresh config; the seed enables it through the
  server-side `HashPassword` RPC and stores the bcrypt hash (base64) in config.
- Restic repo passwords (seeded, not needed in the UI): `lakeside-nas-2026`
  (`/repos/workshop-nas`), `lakeside-archive-2026` (`/repos/cold-archive`).

## Reset

```bash
./tester-env reset     # docker compose -p <project> -f docker-compose.tester-env.yml down -v --remove-orphans
./tester-env deploy    # rebuild/restart cleanly
```

Reset removes the container, network and the four named volumes (config, data,
cache, repos) but **keeps the image** — content-addressed tags may be shared
with in-flight runs; image GC is `scripts/rl-env gc --prune-images`. No manual
UI setup or host cleanup is required; every deploy recreates first-run state and
re-seeds. Schedules are seeded disabled, so no cron job can repopulate state
after a reset.

## Deterministic Seed Data

- Seed: `./tester-env seed` → `tester-env-seed.py` (ConnectRPC JSON API from the
  host; no browser, no SQL, no manual steps). Idempotent and deterministic.
- Verify: `./tester-env verify` (checks image restic `0.19.1`, both on-disk
  restic repos, then the API-side seed contract).
- Full cycle: `reset -> deploy -> seed -> verify`.

Instance: `lakeside-workshop` (a small furniture workshop; the instance id is on
every seeded operation and as the `created-by:lakeside-workshop` snapshot tag).

| Repo | URI | Backups | Retention plans |
|---|---|---|---|
| `workshop-nas` | `/repos/workshop-nas` | 3 OK | `documents`, `project-files` |
| `cold-archive` | `/repos/cold-archive` | 1 OK | `photo-archive` |

| Plan | Repo | Paths | Excludes | Retention | Snapshots |
|---|---|---|---|---|---|
| `documents` | `workshop-nas` | `/fixtures/documents` | `*.tmp`, `~$*` | keep-last 5 | 2 |
| `project-files` | `workshop-nas` | `/fixtures/projects` | `.cache`, `node_modules` | keep-last 10 | 1 |
| `photo-archive` | `cold-archive` | `/fixtures/media` | — | monthly 6 + keep-last 2 | 1 |

All three schedules are `{disabled: true, clock: CLOCK_LOCAL}`.

`workshop-nas` also carries the Add Repo dialog's default repo-level forget
policy (`forgetPolicy`: schedule `{disabled: true, clock: CLOCK_LAST_RUN_TIME}`,
retention time-bucketed hourly 24 / daily 30 / monthly 12): configured but never
scheduled, so per-plan retention still drives the per-backup forgets and the repo
action menu offers `Forget Now` for `workshop-nas` only (`cold-archive` has no
`forgetPolicy`).

History: the 4 real restic backups produce 4 snapshots and
`Backup` + `Snapshot` + `Forget` operations (12), plus one repo `Stats` task on
`workshop-nas` and one repo `Check` task on `cold-archive` — 14 operations, all
`STATUS_SUCCESS`.

Fixture tree (committed in `.tester-env-fixtures/`, baked read-only to
`/fixtures`; every file is small business content):

```text
documents/handbook.md, inventory.csv, planning-notes.txt,
          supplier-agreements/northline-abrasives-2026.md   (4 backed-up files)
documents/old-planning-notes.tmp                             (excluded by *.tmp)
projects/front-room-shelving/{README.md,cut-list.csv,finish-samples.md}
projects/oak-dining-table/{README.md,cut-list.csv}
projects/workshop-layout.md                                  (6 files)
media/build-photos/2026-06-shelving/index.txt
media/build-photos/2026-10-dust-extractor/index.txt
media/brand/workshop-logo.svg                                (3 files)
```

The `documents` snapshot reports **4** processed files (not 5) because
`old-planning-notes.tmp` is excluded; the SnapshotBrowser never lists the `.tmp`.

`verify` asserts, without a browser: SPA shell 200; `GetConfig` 401 without a
token; admin login OK and wrong password 401; instance/auth user; both repos
with expected URIs and 64-hex GUIDs plus `workshop-nas`' disabled repo forget
policy (and its absence on `cold-archive`); all three plans with repo/paths/excludes/
disabled schedule/retention; snapshot counts ≥ 2/1/1 with matching paths,
`plan:<id>` + `created-by:lakeside-workshop` tags and fixture file counts 4/6/3;
successful backup/index/forget counts; stats op for `workshop-nas` and check op
for `cold-archive`; dashboard success counts 2/1/1 and repo counts 3/1.

## Browser Evidence (gates passed pre-fork)

- Source build: `./tester-env deploy` built from editable source at
  `875c9cb`; `GET http://127.0.0.1:9898/` returns 200 with the SPA shell;
  container logs show `backrest starting {"version": "v1.14.1"}` and
  `starting web server {"addr": "0.0.0.0:9898"}` with no restic download.
- Browser smoke (browser-checker, 2026-11-14): the environment loads at
  `http://host.docker.internal:9898/`; first-run Settings is already resolved
  (instance `lakeside-workshop`); `admin` / `BackrestAdmin!2026` logs in; the
  sidebar lists 3 plans (`documents`, `photo-archive`, `project-files`) and
  2 repositories (`cold-archive`, `workshop-nas`); the plan view and dashboard
  show 4 completed backups, and the `documents` plan view shows the seeded
  snapshot with 4 files added. Reset + redeploy returned the same visible state.
- Seed (browser-checked, 2026-11-14): `data_populated` — 2 repos, 3 plans,
  4 backups visible in the UI; recorded from the browser-checker return.

## Known Nondeterminism

Snapshot ids, operation ids, repo GUIDs, the JWT signing key and all timestamps
are generated at seed time; repo stats `total_size` may vary by a couple of
bytes because restic stores real file bytes. Assert counts, names, paths, tags
and statuses — never ids, absolute dates or byte totals.

## Caveats

- Backup/restore are real async restic runs; the seed waits for success but
  scenario assertions should target persisted rows/counts, not transient toasts.
- Restore defaults to `$HOME/Downloads` in-container; pass an explicit writable
  target (e.g. `/restore`).
- Remote backends (S3/B2/GCS/SFTP/rclone), shoutrrr notifications and multihost
  pairing need external services and stay out of scope; local-path repos only.
- `vite` emits gzip-only assets (`deleteOriginFile`); the Go `statigz` server
  serves the `.gz` files from the embedded `dist`, so never hand-serve
  `webui/dist` as static files.
