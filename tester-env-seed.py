#!/usr/bin/env python3
"""Deterministic seed and verify for the Backrest tester-env.

Talks to the backrest ConnectRPC JSON API from the host (no browser involved).

  seed   - first-run config (instance name), deterministic admin user via the
           server-side bcrypt hashing RPC, two local restic repositories
           (workshop-nas carrying the Add Repo dialog's default disabled
           forget policy), three
           plans over the committed /fixtures tree with schedules disabled, and
           a small pre-run backup history (2 + 1 + 1 snapshots) plus one repo
           stats and one repo check operation so the dashboard, plan, repo and
           operation views are populated.
  verify - asserts the seeded config, the login gate, the snapshot/operation
           history and the dashboard roll-up.

Idempotent: every step is skipped when its seeded state is already present, so
re-running `seed` on the same volume leaves the visible state unchanged.
Configuration comes from environment variables set by the `tester-env` CLI.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BACKREST_BASE_URL", "http://127.0.0.1:9898").rstrip("/")
INSTANCE_NAME = os.environ.get("BACKREST_INSTANCE_NAME", "lakeside-workshop")
ADMIN_USER = os.environ.get("BACKREST_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("BACKREST_ADMIN_PASSWORD", "BackrestAdmin!2026")

# Fixture theme: Lakeside Workshop, a small furniture workshop whose documents,
# client projects and build photos live under /fixtures (read-only, image-baked).
# The Add Repo dialog gives every new repo the same forget policy: a disabled
# schedule plus a time-bucketed retention (webui AddRepoModal repoDefaults). The
# disabled schedule means the repo-level forget never fires, so the per-plan
# forget follow-ups after each backup keep applying plan retention.
REPO_FORGET_POLICY = {
    "schedule": {"disabled": True, "clock": "CLOCK_LAST_RUN_TIME"},
    "retention": {"policyTimeBucketed": {"hourly": 24, "daily": 30, "monthly": 12}},
}

REPOS = [
    {
        "id": "workshop-nas",
        "uri": "/repos/workshop-nas",
        "password": "lakeside-nas-2026",
        "forgetPolicy": REPO_FORGET_POLICY,
    },
    {
        "id": "cold-archive",
        "uri": "/repos/cold-archive",
        "password": "lakeside-archive-2026",
    },
]

SCHEDULE_DISABLED = {"disabled": True, "clock": "CLOCK_LOCAL"}

PLANS = [
    {
        "id": "documents",
        "repo": "workshop-nas",
        "paths": ["/fixtures/documents"],
        "excludes": ["*.tmp", "~$*"],
        "schedule": SCHEDULE_DISABLED,
        "retention": {"policyKeepLastN": 5},
    },
    {
        "id": "project-files",
        "repo": "workshop-nas",
        "paths": ["/fixtures/projects"],
        "excludes": [".cache", "node_modules"],
        "schedule": SCHEDULE_DISABLED,
        "retention": {"policyKeepLastN": 10},
    },
    {
        "id": "photo-archive",
        "repo": "cold-archive",
        "paths": ["/fixtures/media"],
        "schedule": SCHEDULE_DISABLED,
        "retention": {"policyTimeBucketed": {"monthly": 6, "keepLastN": 2}},
    },
]

# Pre-run backups per plan, and how many files restic must see in each snapshot
# (documents has five files on disk; old-planning-notes.tmp is excluded by *.tmp).
BACKUP_TARGETS = {"documents": 2, "project-files": 1, "photo-archive": 1}
FIXTURE_FILE_COUNTS = {"documents": 4, "project-files": 6, "photo-archive": 3}

# One repo task per repo so the repo Stats tab and the operation history show
# system work next to the backup flows.
REPO_TASKS = [("workshop-nas", "TASK_STATS"), ("cold-archive", "TASK_CHECK")]

TERMINAL_STATUSES = {
    "STATUS_SUCCESS",
    "STATUS_WARNING",
    "STATUS_ERROR",
    "STATUS_SYSTEM_CANCELLED",
    "STATUS_USER_CANCELLED",
}
OP_KINDS = (
    "operationBackup",
    "operationIndexSnapshot",
    "operationForget",
    "operationPrune",
    "operationRestore",
    "operationStats",
    "operationRunHook",
    "operationCheck",
    "operationRunCommand",
)


class RpcError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code


def rpc(service: str, method: str, payload: dict, token: str | None = None) -> dict:
    url = f"{BASE_URL}/v1.{service}/{method}"
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connect-Protocol-Version": "1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            error = json.loads(raw)
            code = error.get("code", "unknown")
            message = error.get("message", raw.decode(errors="replace"))
        except json.JSONDecodeError:
            code, message = "unknown", raw.decode(errors="replace")
        raise RpcError(exc.code, code, message) from None
    return json.loads(raw) if raw else {}


def get_config(token: str | None = None) -> dict:
    return rpc("Backrest", "GetConfig", {}, token)


def set_config(config: dict, token: str | None = None) -> dict:
    return rpc("Backrest", "SetConfig", config, token)


def login() -> str:
    response = rpc(
        "Authentication",
        "Login",
        {"username": ADMIN_USER, "password": ADMIN_PASSWORD},
    )
    token = response.get("token", "")
    if not token:
        raise SystemExit("error: login returned no token")
    return token


def hash_password(password: str) -> str:
    # backrest stores bcrypt hashes base64-encoded (internal/auth.CreatePassword);
    # checkPassword decodes before comparing.
    response = rpc("Authentication", "HashPassword", {"value": password})
    hashed = response.get("value", "")
    try:
        decoded = base64.b64decode(hashed, validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if not decoded.startswith(b"$2"):
        raise SystemExit(f"error: unexpected password hash {hashed!r}")
    return hashed


def auth_enabled(config: dict) -> bool:
    # protojson omits default values, so an enabled Auth has no "disabled" key.
    auth = config.get("auth") or {}
    return bool(auth) and not auth.get("disabled", False)


def list_snapshots(repo_id: str, plan_id: str, token: str) -> list[dict]:
    # A backup flow also schedules the plan's forget task; both hold the restic
    # repo lock briefly after the Backup RPC returns. Retry the lock conflict
    # instead of surfacing a transient HTTP 500.
    for _ in range(30):
        try:
            response = rpc(
                "Backrest",
                "ListSnapshots",
                {"repoId": repo_id, "planId": plan_id},
                token,
            )
            return response.get("snapshots") or []
        except RpcError as exc:
            if "already locked" in str(exc) or "lock" in str(exc):
                time.sleep(1)
                continue
            raise
    raise SystemExit(f"error: repo {repo_id!r} stayed locked while listing snapshots")


def list_operations(token: str, repo_guid: str | None = None) -> list[dict]:
    selector = {"repoGuid": repo_guid} if repo_guid else {}
    response = rpc("Backrest", "GetOperations", {"selector": selector}, token)
    return response.get("operations") or []


def op_kind(op: dict) -> str | None:
    for kind in OP_KINDS:
        if kind in op:
            return kind
    return None


def wait_for_plan_flow(plan_id: str, repo_guid: str, token: str) -> None:
    """Wait until the plan's backup flow (backup + index + forget) has settled.

    Backup RPC returns when the backup task finishes, but the flow's index and
    forget tasks run right after it. Starting the next backup while they run
    makes restic lock contention flaky (the forget can fail with a repo lock),
    so serialize on a terminal forget operation per completed backup.
    """
    for _ in range(120):
        ops = [
            op
            for op in list_operations(token, repo_guid)
            if op.get("planId") == plan_id
        ]
        backups = [op for op in ops if op_kind(op) == "operationBackup"]
        forgets = [
            op
            for op in ops
            if op_kind(op) == "operationForget" and op.get("status") in TERMINAL_STATUSES
        ]
        active = [
            op
            for op in ops
            if op.get("status") in ("STATUS_PENDING", "STATUS_INPROGRESS")
        ]
        if backups and len(forgets) >= len(backups) and not active:
            return
        time.sleep(1)
    raise SystemExit(f"error: backup flow for plan {plan_id!r} did not settle in 120s")


def wait_for_operation(operation_id: str, repo_guid: str, token: str) -> dict:
    for _ in range(120):
        for op in list_operations(token, repo_guid):
            if str(op.get("id")) == str(operation_id):
                if op.get("status") in TERMINAL_STATUSES:
                    return op
        time.sleep(1)
    raise SystemExit(
        f"error: operation {operation_id} on repo guid {repo_guid} did not finish in 120s"
    )


def seed() -> None:
    token: str | None = None
    try:
        config = get_config()
    except RpcError as exc:
        if exc.status != 401:
            raise
        # Auth already enabled by a previous seed: authenticate before reading.
        token = login()
        config = get_config(token)

    # --- first-run instance name + auth -------------------------------------
    if config.get("instance") != INSTANCE_NAME:
        config["instance"] = INSTANCE_NAME
        config = set_config(config, token)
        print(f"    instance: set to {INSTANCE_NAME!r}")

    if not auth_enabled(config):
        users = (config.get("auth") or {}).get("users") or []
        if not any(user.get("name") == ADMIN_USER for user in users):
            users = [{"name": ADMIN_USER, "passwordBcrypt": hash_password(ADMIN_PASSWORD)}]
        config["auth"] = {"disabled": False, "users": users}
        config = set_config(config, None)
        print(f"    auth: enabled with user {ADMIN_USER!r}")

    token = login()
    print(f"    login: {ADMIN_USER} -> ok")

    # --- local restic repositories ------------------------------------------
    config = get_config(token)
    existing_repos = {repo.get("id") for repo in config.get("repos") or []}
    for repo in REPOS:
        if repo["id"] in existing_repos:
            continue
        fields = ("id", "uri", "password", "forgetPolicy")
        payload = {key: repo[key] for key in fields if key in repo}
        config = rpc("Backrest", "AddRepo", {"repo": payload}, token)
        note = " with the default disabled forget policy" if "forgetPolicy" in repo else ""
        print(f"    repo: initialized {repo['id']!r} at {repo['uri']}{note}")

    # --- plans over the fixture tree, schedules disabled --------------------
    config = get_config(token)
    plans = config.get("plans") or []
    existing_plans = {plan.get("id"): plan for plan in plans}
    for wanted in PLANS:
        if wanted["id"] in existing_plans:
            continue
        plans.append(json.loads(json.dumps(wanted)))
        print(f"    plan: added {wanted['id']!r} over {wanted['paths']} (schedule disabled)")
    if len(plans) != len(config.get("plans") or []):
        config["plans"] = plans
        config = set_config(config, token)

    # --- pre-run backup history ---------------------------------------------
    config = get_config(token)
    repo_guids = {repo.get("id"): repo.get("guid") for repo in config.get("repos") or []}
    for plan in PLANS:
        plan_id = plan["id"]
        target = BACKUP_TARGETS[plan_id]
        snapshots = list_snapshots(plan["repo"], plan_id, token)
        attempts = 0
        while len(snapshots) < target:
            if attempts >= 5:
                raise SystemExit(
                    f"error: plan {plan_id!r} has {len(snapshots)} snapshots, expected {target}"
                )
            attempts += 1
            rpc("Backrest", "Backup", {"value": plan_id}, token)
            wait_for_plan_flow(plan_id, repo_guids[plan["repo"]], token)
            snapshots = list_snapshots(plan["repo"], plan_id, token)
        print(f"    backups: {plan_id!r} -> {len(snapshots)} snapshot(s) ({plan['repo']})")

    # --- repo maintenance history -------------------------------------------
    for repo_id, task in REPO_TASKS:
        repo_guid = repo_guids.get(repo_id)
        kind = "operationStats" if task == "TASK_STATS" else "operationCheck"
        if any(op_kind(op) == kind for op in list_operations(token, repo_guid)):
            continue
        response = rpc(
            "Backrest",
            "DoRepoTask",
            {"repoId": repo_id, "task": task},
            token,
        )
        operation_id = response.get("operationId")
        op = wait_for_operation(operation_id, repo_guid, token)
        print(
            f"    repo task: {task} on {repo_id!r} ->"
            f" {op.get('status', 'STATUS_UNKNOWN')}"
        )

    # --- summary ------------------------------------------------------------
    config = get_config(token)
    operations = list_operations(token)
    backups = [op for op in operations if op_kind(op) == "operationBackup"]
    kind_counts: dict[str, int] = {}
    for op in operations:
        kind = op_kind(op) or "unknown"
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    print(
        "    seeded:"
        f" instance={config.get('instance')}"
        f" repos={[repo.get('id') for repo in config.get('repos') or []]}"
        f" plans={[plan.get('id') for plan in config.get('plans') or []]}"
        f" backups={len(backups)}"
        f" operations={dict(sorted(kind_counts.items()))}"
    )


def verify() -> None:
    errors: list[str] = []

    # SPA shell is served without auth; the API behind it is gated.
    with urllib.request.urlopen(f"{BASE_URL}/", timeout=20) as response:
        shell = response.read()
        if response.status != 200 or b'<div id="app">' not in shell:
            errors.append(f"GET / did not return the SPA shell (HTTP {response.status})")

    try:
        rpc("Backrest", "GetConfig", {})
        errors.append("GetConfig succeeded without a token; auth is not enforced")
    except RpcError as exc:
        if exc.status != 401:
            errors.append(f"unauthenticated GetConfig returned HTTP {exc.status}, expected 401")

    token = None
    try:
        token = login()
    except RpcError as exc:
        errors.append(f"login as {ADMIN_USER!r} failed (HTTP {exc.status}); has the seed run?")

    if token is not None:
        try:
            rpc("Authentication", "Login", {"username": ADMIN_USER, "password": "wrong-password"})
            errors.append("login with a wrong password succeeded")
        except RpcError as exc:
            if exc.status != 401:
                errors.append(f"wrong-password login returned HTTP {exc.status}, expected 401")

        config = get_config(token)
    else:
        config = {}

    if config.get("instance") != INSTANCE_NAME:
        errors.append(f"instance is {config.get('instance')!r}, expected {INSTANCE_NAME!r}")
    if not auth_enabled(config):
        errors.append("auth is disabled, expected the deterministic admin gate")
    elif not any(user.get("name") == ADMIN_USER for user in config["auth"].get("users") or []):
        errors.append(f"auth user {ADMIN_USER!r} is missing")

    # --- repositories -------------------------------------------------------
    repos = {repo.get("id"): repo for repo in config.get("repos") or []}
    if sorted(repos) != sorted(repo["id"] for repo in REPOS):
        errors.append(f"repos are {sorted(repos)}, expected {sorted(r['id'] for r in REPOS)}")
    repo_guids: dict[str, str] = {}
    for wanted in REPOS:
        repo = repos.get(wanted["id"])
        if repo is None:
            errors.append(f"repo {wanted['id']!r} is missing")
            continue
        if repo.get("uri") != wanted["uri"]:
            errors.append(
                f"repo {wanted['id']!r} uri is {repo.get('uri')!r}, expected {wanted['uri']!r}"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", repo.get("guid", "")):
            errors.append(
                f"repo {wanted['id']!r} guid is not a 64-char hex value: {repo.get('guid')!r}"
            )
        else:
            repo_guids[wanted["id"]] = repo["guid"]
        wanted_forget = wanted.get("forgetPolicy")
        forget = repo.get("forgetPolicy")
        if wanted_forget is None:
            if forget is not None:
                errors.append(f"repo {wanted['id']!r} has an unexpected forgetPolicy: {forget!r}")
        elif forget != wanted_forget:
            errors.append(
                f"repo {wanted['id']!r} forgetPolicy is {forget!r}, expected {wanted_forget!r}"
            )

    # --- plans --------------------------------------------------------------
    plans = {plan.get("id"): plan for plan in config.get("plans") or []}
    if sorted(plans) != sorted(plan["id"] for plan in PLANS):
        errors.append(f"plans are {sorted(plans)}, expected {sorted(p['id'] for p in PLANS)}")
    for wanted in PLANS:
        plan = plans.get(wanted["id"])
        if plan is None:
            errors.append(f"plan {wanted['id']!r} is missing")
            continue
        if plan.get("repo") != wanted["repo"]:
            errors.append(
                f"plan {wanted['id']!r} repo is {plan.get('repo')!r}, expected {wanted['repo']!r}"
            )
        if plan.get("paths") != wanted["paths"]:
            errors.append(
                f"plan {wanted['id']!r} paths are {plan.get('paths')!r}, expected {wanted['paths']!r}"
            )
        if (plan.get("excludes") or []) != wanted.get("excludes", []):
            errors.append(
                f"plan {wanted['id']!r} excludes are {plan.get('excludes')!r},"
                f" expected {wanted.get('excludes', [])!r}"
            )
        schedule = plan.get("schedule") or {}
        if schedule.get("disabled") is not True:
            errors.append(f"plan {wanted['id']!r} schedule is not disabled: {schedule!r}")
        if (plan.get("retention") or {}) != wanted["retention"]:
            errors.append(
                f"plan {wanted['id']!r} retention is {plan.get('retention')!r},"
                f" expected {wanted['retention']!r}"
            )

    # --- snapshot history ---------------------------------------------------
    if token is not None:
        snapshot_counts: dict[str, int] = {}
        for wanted in PLANS:
            plan_id = wanted["id"]
            try:
                snapshots = list_snapshots(wanted["repo"], plan_id, token)
            except RpcError as exc:
                errors.append(f"ListSnapshots({plan_id!r}) failed: {exc}")
                continue
            snapshot_counts[plan_id] = len(snapshots)
            if len(snapshots) < BACKUP_TARGETS[plan_id]:
                errors.append(
                    f"plan {plan_id!r} has {len(snapshots)} snapshots,"
                    f" expected at least {BACKUP_TARGETS[plan_id]}"
                )
                continue
            for snapshot in snapshots:
                if snapshot.get("paths") != wanted["paths"]:
                    errors.append(
                        f"snapshot {snapshot.get('id', '?')[:12]} of {plan_id!r} has paths"
                        f" {snapshot.get('paths')!r}, expected {wanted['paths']!r}"
                    )
                    break
                tags = snapshot.get("tags") or []
                if f"plan:{plan_id}" not in tags or f"created-by:{INSTANCE_NAME}" not in tags:
                    errors.append(
                        f"snapshot of {plan_id!r} is missing the plan/instance tags: {tags!r}"
                    )
                    break
            expected_files = FIXTURE_FILE_COUNTS[plan_id]
            file_counts = [
                int((snapshot.get("summary") or {}).get("totalFilesProcessed") or 0)
                for snapshot in snapshots
            ]
            if expected_files not in file_counts:
                errors.append(
                    f"no snapshot of {plan_id!r} processed {expected_files} files;"
                    f" observed {sorted(file_counts)} (fixture tree or excludes changed)"
                )
        # Operation history --------------------------------------------------
        operations = list_operations(token)
        per_plan_backups: dict[str, int] = {}
        for op in operations:
            if op_kind(op) == "operationBackup" and op.get("status") == "STATUS_SUCCESS":
                per_plan_backups[op.get("planId", "")] = (
                    per_plan_backups.get(op.get("planId", ""), 0) + 1
                )
        for plan_id, target in BACKUP_TARGETS.items():
            if per_plan_backups.get(plan_id, 0) < target:
                errors.append(
                    f"plan {plan_id!r} has {per_plan_backups.get(plan_id, 0)} successful"
                    f" backup operations, expected at least {target}"
                )
        total_backups = sum(per_plan_backups.values())
        for kind, minimum in (
            ("operationIndexSnapshot", total_backups),
            ("operationForget", total_backups),
        ):
            successes = sum(
                1
                for op in operations
                if op_kind(op) == kind and op.get("status") == "STATUS_SUCCESS"
            )
            if successes < minimum:
                errors.append(
                    f"successful {kind} count is {successes}, expected at least {minimum}"
                )
        stats_ops = [
            op
            for op in operations
            if op_kind(op) == "operationStats"
            and repo_guids.get("workshop-nas")
            and op.get("repoGuid") == repo_guids.get("workshop-nas")
        ]
        check_ops = [
            op
            for op in operations
            if op_kind(op) == "operationCheck"
            and repo_guids.get("cold-archive")
            and op.get("repoGuid") == repo_guids.get("cold-archive")
        ]
        if not stats_ops:
            errors.append("no repo stats operation for 'workshop-nas' (Stats tab would be empty)")
        elif int((stats_ops[-1].get("operationStats") or {}).get("stats", {}).get("snapshotCount") or 0) < 3:
            errors.append("the 'workshop-nas' stats operation does not report at least 3 snapshots")
        if not check_ops:
            errors.append("no repo check operation for 'cold-archive'")

        # Dashboard roll-up --------------------------------------------------
        dashboard = rpc("Backrest", "GetSummaryDashboard", {}, token)
        plan_summaries = {s.get("id"): s for s in dashboard.get("planSummaries") or []}
        for plan_id, target in BACKUP_TARGETS.items():
            summary = plan_summaries.get(plan_id)
            if summary is None:
                errors.append(f"dashboard plan summary for {plan_id!r} is missing")
                continue
            success = int(summary.get("backupsSuccessLast30days") or 0)
            if success < target:
                errors.append(
                    f"dashboard plan {plan_id!r} reports {success} successful backups,"
                    f" expected at least {target}"
                )
            if int(summary.get("protectedBytes") or 0) <= 0:
                errors.append(f"dashboard plan {plan_id!r} reports no protected bytes")
            history_success = sum(
                int(count.get("count") or 0)
                for bucket in summary.get("historyLast30days") or []
                for count in bucket.get("statusCounts") or []
                if count.get("status") == "STATUS_SUCCESS"
            )
            if history_success < target:
                errors.append(
                    f"dashboard history for {plan_id!r} shows {history_success} successful"
                    f" backups, expected at least {target}"
                )
        repo_summaries = {s.get("id"): s for s in dashboard.get("repoSummaries") or []}
        for repo_id, target in (("workshop-nas", 3), ("cold-archive", 1)):
            summary = repo_summaries.get(repo_id)
            if summary is None:
                errors.append(f"dashboard repo summary for {repo_id!r} is missing")
                continue
            success = int(summary.get("backupsSuccessLast30days") or 0)
            if success < target:
                errors.append(
                    f"dashboard repo {repo_id!r} reports {success} successful backups,"
                    f" expected at least {target}"
                )

    if errors:
        print("error: seeded state check failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise SystemExit(1)

    print(
        "    verified: SPA shell 200, login gate 401 + admin login, wrong password rejected,"
        f" instance={INSTANCE_NAME!r},"
        f" repos={sorted(repos)} (initialized; workshop-nas forget policy disabled"
        " with hourly 24 / daily 30 / monthly 12 retention),"
        f" plans={sorted(plans)} (schedule disabled),"
        f" snapshots={dict(sorted(snapshot_counts.items()))},"
        f" backups={dict(sorted(per_plan_backups.items()))},"
        " repo tasks=stats(workshop-nas)+check(cold-archive), dashboard populated"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("seed", "verify"):
        print("usage: tester-env-seed.py <seed|verify>", file=sys.stderr)
        return 2
    try:
        if argv[1] == "seed":
            seed()
        else:
            verify()
    except RpcError as exc:
        print(f"error: backrest RPC failed: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"error: cannot reach {BASE_URL}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
