#!/usr/bin/env python3
"""
script.py - Create or close Veracode baseline scan trigger issues across all repos in a GitHub org.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from datetime import datetime, timezone

ISSUE_TITLE = "Veracode Baseline Scans"
ISSUE_BODY = "Veracode All Scans"
IAC_LANGUAGES = frozenset({"HCL", "Bicep"})
SUBPROCESS_TIMEOUT = 60
REPO_LIST_TIMEOUT = 300
# --paginate walks every page in one gh process; 60s is too short on busy repos.
PAGINATE_TIMEOUT = 180
DEFAULT_STALE_DAYS = 30
SECONDARY_RATE_LIMIT_SLEEP = 60
SECONDARY_BACKOFF_CAP = 600
MAX_RATE_LIMIT_RETRIES = 3
PARTIAL_STDERR_LIMIT = 500
DEFAULT_WORKERS = 5
MAX_WORKERS = 50

# GitHub asks for at least 1s between mutative requests.
MUTATION_MIN_GAP = 1.0
# Global minimum gap between issue creations. GitHub's issue-creation limit is
# undisclosed and fails silently, so this is deliberately conservative.
DEFAULT_CREATE_INTERVAL = 25.0
DEFAULT_BATCH_PAUSE = 900.0
# Delete mode: issue closes per minute, global. Closing (PATCH state) is not
# content creation, so it is only bound by this and MUTATION_MIN_GAP.
DEFAULT_CLOSE_PER_MINUTE = 40
CLOSE_ATTEMPTS = 2
INFLIGHT_POLL_SECONDS = 60
# --max-inflight: how long Actions-API errors may last before a repo gives up
# waiting, how long a full queue may block one repo, and how long a reserved
# slot is counted before its run must be visible in the central repo.
INFLIGHT_ERROR_TIMEOUT = 1800
INFLIGHT_MAX_WAIT = 7 * 3600
INFLIGHT_GRACE_SECONDS = 300
VERIFY_DELAY_SECONDS = 3
VERIFY_ATTEMPTS = 3
COMMENT_SINCE_MARGIN_SECONDS = 120

VERACODE_REGIONS = {
    "commercial": "https://api.veracode.com/appsec/v1",
    "eu": "https://api.veracode.eu/appsec/v1",
    "federal": "https://api.veracode.us/appsec/v1",
}
VERACODE_PAGE_SIZE = 500
VERACODE_TIMEOUT = 60
VERACODE_MAX_RETRIES = 3
VERACODE_RETRY_SLEEP = 5
# 7th character of the 8-character API key id prefix (e.g. "vera01ei-...").
# Ids without a prefix are commercial.
_VERACODE_KEY_REGION = {"g": "commercial", "e": "eu", "f": "federal"}
_VERACODE_KEY_PREFIX_RE = re.compile(r"^vera[a-z0-9]{4}-", re.IGNORECASE)


def resolve_veracode_region(explicit: str | None, key_id: str | None) -> tuple[str, str | None]:
    """(region, warning). An explicit --veracode-region wins; otherwise the
    region is inferred from the API key id prefix, else commercial. Warns when
    an explicit region contradicts the key id."""
    inferred = None
    if key_id and _VERACODE_KEY_PREFIX_RE.match(key_id.strip()):
        inferred = _VERACODE_KEY_REGION.get(key_id.strip()[6].lower())
    if explicit is None:
        return inferred or "commercial", None
    if inferred and inferred != explicit:
        return explicit, (
            f"--veracode-region {explicit} does not match VERACODE_API_KEY_ID, "
            f"which looks like a {inferred} key. Using {explicit}."
        )
    return explicit, None


_VALID_ORG_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,37}[a-zA-Z0-9])?$")
_VALID_REPO_RE = re.compile(r"^[a-zA-Z0-9._*?\[\]-]+$")
_RETRY_AFTER_RE = re.compile(r"(?:retry[- ]after|wait)[:\s]+(\d+)", re.IGNORECASE)

_REPO_JQ = (
    "[.[] | {"
    "nameWithOwner: .nameWithOwner, "
    "hasIssuesEnabled: .hasIssuesEnabled, "
    "primaryLanguage: (.primaryLanguage.name // \"N/A\"), "
    "isArchived: .isArchived, "
    "defaultBranch: (.defaultBranchRef.name // \"\")"
    "}]"
)

_RL_JQ = (
    "[.resources.core.remaining, .resources.core.reset, "
    "(.resources.graphql.remaining // 9999), (.resources.graphql.reset // 0)] | .[]"
)

_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "secondary rate",
    "abuse detection",
    "api rate limit exceeded",
    "was submitted too quickly",
    "submitted too quickly",
)

_SECONDARY_RATE_LIMIT_PATTERNS = (
    "submitted too quickly",
    "secondary rate",
    "abuse detection",
)

_AUTH_FAILURE_PATTERNS = (
    "must have admin rights",
    "resource not accessible",
    "bad credentials",
    "requires authentication",
)

_DEFAULT_CENTRAL_REPO = "veracode"
# Actions runs in the central repo: total_count first, then one JSON string per
# run (display title + name) so a run can be matched to the repo it scans.
_RUNS_JQ = '.total_count, (.workflow_runs[] | ((.display_title // "") + " " + (.name // "")) | @json)'
# Check runs: only the completion times of runs named veracode*, nothing else
# (check output can contain arbitrary text).
_CHECK_RUNS_JQ = (
    r'.check_runs[] | select((.name // "") | ascii_downcase | test("^\\s*veracode"))'
    ' | .completed_at // empty'
)


class OrgFileError(Exception):
    pass


class RepoFileError(Exception):
    pass


class OrgRepoFileError(Exception):
    pass


class VeracodeError(Exception):
    pass


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        print(
            f"Warning: {key}={val!r} is not a valid integer, using default {default}",
            file=sys.stderr,
        )
        return default


CORE_MIN_REMAINING = _env_int("GH_RL_MIN_REMAINING", 100)
CORE_CHECK_EVERY = _env_int("GH_RL_CHECK_EVERY", 50)
DEFAULT_REPO_LIST_LIMIT = _env_int("REPO_LIST_LIMIT", 1000)

GH_POINTS_PER_MIN = _env_int("GH_POINTS_PER_MIN", 700)
GH_CONTENT_PER_MIN = _env_int("GH_CONTENT_PER_MIN", 20)
GH_CONTENT_PER_HOUR = _env_int("GH_CONTENT_PER_HOUR", 140)
GH_SEARCH_PER_MIN = _env_int("GH_SEARCH_PER_MIN", 25)


@dataclass(frozen=True)
class CallCost:
    points: int
    content: bool = False
    mutation: bool = False
    search: bool = False
    close: bool = False


COST_READ = CallCost(points=1)
COST_SEARCH = CallCost(points=1, search=True)
COST_WRITE = CallCost(points=5, mutation=True)
COST_CONTENT = CallCost(points=5, content=True, mutation=True)
# REST PATCH state=closed: a mutation, but it creates no content, so it is not
# charged against the content/minute and content/hour budgets.
COST_CLOSE = CallCost(points=5, mutation=True, close=True)


class Aborted(Exception):
    """Raised inside a worker when the run is stopping (Ctrl-C)."""


# Set on Ctrl-C. Workers stop before their next write; waits wake up early.
STOP = threading.Event()


def check_stop() -> None:
    if STOP.is_set():
        raise Aborted()


def _sleep(seconds: float) -> None:
    """Interruptible sleep: returns early once STOP is set (Ctrl-C)."""
    if seconds > 0:
        STOP.wait(seconds)


class Throttle:
    """Shared across all worker threads. Paces every gh call against GitHub's
    secondary rate limits: combined points/minute, content/minute, content/hour,
    search/minute, a minimum gap between mutations, plus a global back-off window
    so a single secondary-limit hit stalls every thread instead of all of them
    retrying at once."""

    def __init__(
        self,
        points_per_min: int,
        content_per_min: int,
        content_per_hour: int,
        search_per_min: int = GH_SEARCH_PER_MIN,
        close_per_min: int = 0,
    ) -> None:
        self._lock = threading.Lock()
        self._points: deque[tuple[float, int]] = deque()
        self._content_min: deque[float] = deque()
        self._content_hour: deque[float] = deque()
        self._search_min: deque[float] = deque()
        self._close_min: deque[float] = deque()
        self._last_mutation = 0.0
        self._pause_until = 0.0
        self.points_per_min = points_per_min
        self.content_per_min = content_per_min
        self.content_per_hour = content_per_hour
        self.search_per_min = search_per_min
        self.close_per_min = close_per_min

    @staticmethod
    def _prune(dq: deque, now: float, period: float, key=lambda e: e) -> None:
        cutoff = now - period
        while dq and key(dq[0]) <= cutoff:
            dq.popleft()

    def register_block(self, seconds: float) -> None:
        with self._lock:
            self._pause_until = max(self._pause_until, time.monotonic() + seconds)

    def acquire(self, cost: CallCost) -> None:
        while True:
            if STOP.is_set():
                self._acquire_stopping(cost)
                return
            with self._lock:
                now = time.monotonic()
                wait = 0.0

                if self._pause_until > now:
                    wait = self._pause_until - now
                else:
                    self._prune(self._points, now, 60.0, key=lambda e: e[0])
                    used = sum(p for _, p in self._points)
                    if used + cost.points > self.points_per_min and self._points:
                        wait = max(wait, self._points[0][0] + 60.0 - now)

                    if cost.mutation:
                        gap = MUTATION_MIN_GAP - (now - self._last_mutation)
                        if gap > 0:
                            wait = max(wait, gap)

                    if cost.search:
                        self._prune(self._search_min, now, 60.0)
                        if len(self._search_min) + 1 > self.search_per_min and self._search_min:
                            wait = max(wait, self._search_min[0] + 60.0 - now)

                    if cost.content:
                        self._prune(self._content_min, now, 60.0)
                        self._prune(self._content_hour, now, 3600.0)
                        if len(self._content_min) + 1 > self.content_per_min and self._content_min:
                            wait = max(wait, self._content_min[0] + 60.0 - now)
                        if len(self._content_hour) + 1 > self.content_per_hour and self._content_hour:
                            wait = max(wait, self._content_hour[0] + 3600.0 - now)

                    if cost.close and self.close_per_min > 0:
                        self._prune(self._close_min, now, 60.0)
                        if len(self._close_min) + 1 > self.close_per_min and self._close_min:
                            wait = max(wait, self._close_min[0] + 60.0 - now)

                    if wait <= 0:
                        self._points.append((now, cost.points))
                        if cost.mutation:
                            self._last_mutation = now
                        if cost.search:
                            self._search_min.append(now)
                        if cost.close:
                            self._close_min.append(now)
                        if cost.content:
                            self._content_min.append(now)
                            self._content_hour.append(now)
                        return

            _sleep(min(wait, 5.0) + random.uniform(0.0, 0.15))

    def _acquire_stopping(self, cost: CallCost) -> None:
        """After Ctrl-C only Issues restores (and their reads) still run. Keep
        the mutation gap, skip budgets and back-off windows so shutdown is quick."""
        while True:
            with self._lock:
                now = time.monotonic()
                gap = MUTATION_MIN_GAP - (now - self._last_mutation) if cost.mutation else 0.0
                if gap <= 0:
                    self._points.append((now, cost.points))
                    if cost.mutation:
                        self._last_mutation = now
                    return
            time.sleep(gap)


class CreateGate:
    """Serialises issue creation across every worker and every org: a minimum gap
    between creations plus an optional batch pause. This is what stops the
    Workflow App from queueing hundreds of scans at once.

    Only successful creates/comments (record_success) count toward a batch.
    A caller-supplied recheck runs inside the gate, right before the write, so
    a repo that gained an issue while waiting gives its turn back."""

    def __init__(
        self,
        interval: float,
        batch_size: int = 0,
        batch_pause: float = 0.0,
    ) -> None:
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._last = 0.0
        self._count = 0
        self._pause_due = False
        self.interval = interval
        self.batch_size = batch_size
        self.batch_pause = batch_pause

    def wait(self, log: "RepoLogger", recheck=None) -> bool:
        """Blocks until this caller may write. Returns False (slot not used) if
        recheck() says the write is no longer wanted."""
        # Lock is deliberately held across the sleep: creations must be serial.
        with self._lock:
            check_stop()
            with self._count_lock:
                pause, self._pause_due = self._pause_due, False
            if pause:
                log.log(
                    f"Batch of {self.batch_size} issued. "
                    f"Draining {self.batch_pause:.0f}s before the next batch."
                )
                _sleep(self.batch_pause)
                check_stop()
            gap = self.interval - (time.monotonic() - self._last)
            if gap > 0:
                _sleep(gap + random.uniform(0.0, 0.5))
                check_stop()
            if recheck is not None and not recheck():
                return False
            self._last = time.monotonic()
            return True

    def record_success(self, log: "RepoLogger") -> None:
        with self._count_lock:
            self._count += 1
            if self.batch_size and self._count % self.batch_size == 0:
                self._pause_due = True


class InflightTracker:
    """Caps how many scans may be queued or running at once. Without this, a
    bulk run queues more Actions jobs than the org can execute, and jobs age
    out past the 6h job / GITHUB_TOKEN lifetime and fail.

    The Workflow App runs scans in the org's central repo (default
    `veracode`), not in the scanned repo, so in-flight = queued + in_progress
    runs there, plus slots this run reserved whose run is not visible yet.
    reserve() is atomic: waiters are serialised, and the slot is taken before
    the create and given back with release() if nothing was written. API
    errors count as 'busy' (fail closed) but give up after error_timeout;
    a full queue gives up after max_wait. Both are logged."""

    def __init__(
        self,
        cap: int,
        ctx: "GhContext",
        poll_seconds: float = INFLIGHT_POLL_SECONDS,
        central_repo: str = _DEFAULT_CENTRAL_REPO,
        error_timeout: float = INFLIGHT_ERROR_TIMEOUT,
        max_wait: float = INFLIGHT_MAX_WAIT,
        grace: float = INFLIGHT_GRACE_SECONDS,
    ) -> None:
        self.cap = cap
        self.ctx = ctx
        self.poll_seconds = poll_seconds
        self.central_repo = central_repo
        self.error_timeout = error_timeout
        self.max_wait = max_wait
        self.grace = grace
        self._wait_lock = threading.Lock()  # one waiter at a time
        self._lock = threading.Lock()  # guards _reserved
        # repo -> (org, confirmed_at); confirmed_at None while the write is pending
        self._reserved: dict[str, tuple[str, float | None]] = {}

    def _active_runs(self, org: str) -> tuple[int, list[str]] | None:
        """(queued + in_progress count, run titles) in the central repo, or
        None on any API/parse error."""
        total = 0
        titles: list[str] = []
        for status in ("queued", "in_progress"):
            ok, out, _ = gh_call(
                [
                    "gh", "api", f"repos/{org}/{self.central_repo}/actions/runs",
                    "-X", "GET",
                    "-f", f"status={status}",
                    "-f", "per_page=100",
                    "--jq", _RUNS_JQ,
                ],
                self.ctx,
                cost=COST_READ,
            )
            if not ok:
                return None
            lines = out.splitlines()
            try:
                total += int(lines[0])
                titles.extend(str(json.loads(line)).lower() for line in lines[1:] if line.strip())
            except (IndexError, ValueError):
                return None
        return total, titles

    def _count(self, org: str, runs: tuple[int, list[str]]) -> int:
        total, titles = runs
        now = time.monotonic()
        with self._lock:
            for repo, (r_org, confirmed) in list(self._reserved.items()):
                if r_org.lower() != org.lower() or confirmed is None:
                    continue
                if now - confirmed >= self.grace or any(repo.lower() in t for t in titles):
                    # Its run is visible (or should be by now): stop double counting.
                    del self._reserved[repo]
            pending = sum(1 for r_org, _ in self._reserved.values() if r_org.lower() == org.lower())
        return total + pending

    def reserve(self, org: str, repo: str, log: "RepoLogger") -> bool:
        """Take a slot for repo. False if the wait timed out (nothing reserved)."""
        if self.cap <= 0:
            return True
        where = f"{org}/{self.central_repo}"
        with self._wait_lock:
            start = time.monotonic()
            first_error: float | None = None
            while True:
                check_stop()
                runs = self._active_runs(org)
                now = time.monotonic()
                if runs is None:
                    first_error = now if first_error is None else first_error
                    if now - first_error >= self.error_timeout:
                        log.warn(
                            f"Could not read Actions runs in {where} for "
                            f"{self.error_timeout:.0f}s. Giving up on this repo."
                        )
                        return False
                    log.warn(f"Could not read Actions runs in {where}. Treating as busy.")
                else:
                    first_error = None
                    in_flight = self._count(org, runs)
                    if in_flight < self.cap:
                        with self._lock:
                            self._reserved[repo] = (org, None)
                        return True
                    log.log(
                        f"{in_flight} scan(s) in flight in {where} (cap {self.cap}). "
                        f"Waiting {self.poll_seconds:.0f}s."
                    )
                if now - start >= self.max_wait:
                    log.warn(f"Waited {self.max_wait:.0f}s for an in-flight slot. Giving up on this repo.")
                    return False
                _sleep(self.poll_seconds)

    def confirm(self, repo: str) -> None:
        """The write landed: keep the slot until its run shows up."""
        with self._lock:
            if repo in self._reserved:
                self._reserved[repo] = (self._reserved[repo][0], time.monotonic())

    def release(self, repo: str) -> None:
        """Nothing was written: give the slot back."""
        with self._lock:
            self._reserved.pop(repo, None)


@dataclass
class RateLimitState:
    call_count: int = 0
    remaining: int = 9999
    reset_epoch: int = 0
    bucket: str = "core"
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class GhContext:
    env: dict[str, str]
    state: RateLimitState
    throttle: Throttle
    min_remaining: int = CORE_MIN_REMAINING
    check_every: int = CORE_CHECK_EVERY


@dataclass
class OrgStats:
    org: str
    total_repos: int = 0
    archived: int = 0
    iac: int = 0
    skipped_perm: int = 0
    skipped_existing: int = 0
    skipped_recent: int = 0
    skipped_veracode_profile: int = 0
    created: int = 0
    commented: int = 0
    skipped_no_issues: int = 0
    deleted: int = 0
    # create/comment write failures (create mode)
    failed: int = 0
    would_close: int = 0
    needs_enable: int = 0
    enable_failed: int = 0
    query_failed: int = 0
    close_failed: int = 0
    restore_failed: int = 0
    crashed: int = 0
    org_failed: int = 0
    aborted: int = 0
    stale_check_failed: int = 0
    not_found: int = 0

    @property
    def total_failed(self) -> int:
        return (
            self.failed + self.enable_failed + self.query_failed + self.close_failed
            + self.restore_failed + self.crashed + self.org_failed
            + self.stale_check_failed + self.not_found
        )


@dataclass
class RepoResult:
    repo_name: str
    row: dict
    logs: list[str] = field(default_factory=list)
    stat_deltas: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DeleteOptions:
    close_comment: str | None = None
    issue_author: str | None = None
    match_body: bool = False


class RepoLogger:
    """Per-repo log buffer. Flushed atomically when the repo finishes."""

    def __init__(self, repo_name: str) -> None:
        self.repo_name = repo_name
        self.lines: list[str] = []

    def log(self, msg: str) -> None:
        self.lines.append(msg)

    def warn(self, msg: str) -> None:
        self.lines.append(f"[WARN] {msg}")


def build_gh_env(gh_hostname: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if gh_hostname:
        env["GH_HOST"] = gh_hostname
    return env


def validate_org_name(org: str) -> bool:
    return bool(_VALID_ORG_RE.match(org))


def validate_repo_name(repo: str) -> bool:
    return bool(_VALID_REPO_RE.match(repo))


def _load_lines(file_path: str, exc_cls: type[Exception], kind: str) -> list[str]:
    if not os.path.isfile(file_path):
        raise exc_cls(f"{kind.capitalize()} file not found: {file_path}")

    with open(file_path, encoding="utf-8") as fh:
        items = [
            stripped for line in fh
            if (stripped := line.strip()) and not stripped.startswith("#")
        ]

    if not items:
        raise exc_cls(
            f"No {kind} names found in '{file_path}' (all lines blank or comments)."
        )
    return items


def dedupe_orgs(orgs: list[str]) -> list[str]:
    """GitHub org names are case-insensitive. Processing the same org twice
    in one run could create duplicate issues, so keep the first spelling."""
    seen: set[str] = set()
    unique: list[str] = []
    for org in orgs:
        key = org.lower()
        if key in seen:
            print(f"Warning: org '{org}' listed more than once. Processing it once.", file=sys.stderr)
            continue
        seen.add(key)
        unique.append(org)
    return unique


def load_orgs_from_file(file_path: str) -> list[str]:
    orgs = _load_lines(file_path, OrgFileError, "org")
    invalid = [o for o in orgs if not validate_org_name(o)]
    if invalid:
        raise OrgFileError(f"Invalid org name(s) in '{file_path}': {', '.join(invalid)}")
    return dedupe_orgs(orgs)


def load_repos_from_file(file_path: str) -> list[str]:
    repos = _load_lines(file_path, RepoFileError, "repo")
    invalid = [r for r in repos if not validate_repo_name(r)]
    if invalid:
        raise RepoFileError(f"Invalid repo name(s) in '{file_path}': {', '.join(invalid)}")
    return repos


def load_org_repos_from_file(file_path: str) -> list[tuple[str, str]]:
    if not os.path.isfile(file_path):
        raise OrgRepoFileError(f"Org-repo file not found: {file_path}")

    pairs: list[tuple[str, str]] = []
    with open(file_path, encoding="utf-8") as fh:
        reader = csv.reader(fh, quotechar='"', skipinitialspace=True)
        line_num = 0
        for row in reader:
            line_num += 1
            if not row or (len(row) == 1 and not row[0].strip()):
                continue
            if row[0].lstrip().startswith("#"):
                continue
            if len(row) != 2:
                raise OrgRepoFileError(
                    f"Line {line_num}: Expected 2 columns (org, repo), got {len(row)}"
                )
            org, repo = row[0].strip(), row[1].strip()
            if not org or not repo:
                raise OrgRepoFileError(f"Line {line_num}: org and repo cannot be empty")
            if not validate_org_name(org):
                raise OrgRepoFileError(f"Line {line_num}: Invalid org name: {org!r}")
            if not validate_repo_name(repo):
                raise OrgRepoFileError(f"Line {line_num}: Invalid repo name: {repo!r}")
            pairs.append((org, repo))

    if not pairs:
        raise OrgRepoFileError(f"No org-repo pairs found in '{file_path}'")
    return pairs


def filter_repos_by_names(repos: list[dict], target_names: list[str]) -> list[dict]:
    """Case-insensitive. Entries with glob characters (* ? [) match via fnmatch."""
    exact = {name.lower() for name in target_names if not _has_glob(name)}
    patterns = [name.lower() for name in target_names if _has_glob(name)]
    result = []
    for r in repos:
        short = r["nameWithOwner"].split("/", 1)[-1].lower()
        if short in exact or any(fnmatch.fnmatchcase(short, p) for p in patterns):
            result.append(r)
    return result


def _has_glob(name: str) -> bool:
    return any(ch in name for ch in "*?[")


def filter_repos_by_wildcard(repos: list[dict], pattern: str) -> list[dict]:
    pattern_lower = pattern.lower()
    return [
        r for r in repos
        if fnmatch.fnmatch(r["nameWithOwner"].split("/", 1)[-1].lower(), pattern_lower)
    ]


def query_rate_limit(ctx: GhContext) -> bool:
    """Reads BOTH the core and graphql buckets. gh uses GraphQL for repo list,
    issue list and repo edit, so watching core alone never fires."""
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit", "--jq", _RL_JQ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            env=ctx.env,
            timeout=SUBPROCESS_TIMEOUT,
        )
        lines = (result.stdout or "").strip().splitlines()
        if len(lines) < 4:
            return False
        core_rem, core_reset, gql_rem, gql_reset = (int(x) for x in lines[:4])
        with ctx.state.lock:
            if core_rem <= gql_rem:
                ctx.state.remaining = core_rem
                ctx.state.reset_epoch = core_reset
                ctx.state.bucket = "core"
            else:
                ctx.state.remaining = gql_rem
                ctx.state.reset_epoch = gql_reset
                ctx.state.bucket = "graphql"
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return False


def maybe_pause_for_rate_limit(ctx: GhContext) -> None:
    with ctx.state.lock:
        ctx.state.call_count += 1
        should_check = (
            ctx.state.call_count == 1
            or ctx.state.call_count % ctx.check_every == 0
        )

    if not should_check:
        return
    if not query_rate_limit(ctx):
        return

    with ctx.state.lock:
        remaining = ctx.state.remaining
        reset_epoch = ctx.state.reset_epoch
        bucket = ctx.state.bucket

    if remaining <= ctx.min_remaining:
        sleep_for = reset_epoch - int(time.time()) + 1
        if sleep_for > 0:
            reset_human = datetime.fromtimestamp(
                reset_epoch, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(
                f"{bucket} rate limit low (remaining={remaining}). "
                f"Sleeping ~{sleep_for}s until reset ({reset_human}).",
                file=sys.stderr,
            )
            ctx.throttle.register_block(sleep_for)
            _sleep(sleep_for)


def _run_subprocess(
    args: list[str],
    env: dict[str, str],
    timeout: int = SUBPROCESS_TIMEOUT,
) -> tuple[bool, str, str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            # gh always emits UTF-8. Without this, Windows decodes with the
            # ANSI code page and an undecodable byte leaves stdout as None.
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        partial_stderr = ""
        if exc.stderr:
            raw = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
            partial_stderr = raw[:PARTIAL_STDERR_LIMIT]
        print(f"Timeout ({timeout}s) running: {' '.join(args)}", file=sys.stderr)
        if partial_stderr:
            print(f"Partial stderr: {partial_stderr}", file=sys.stderr)
        return False, "", "timeout"


def _parse_retry_after(text: str) -> int | None:
    match = _RETRY_AFTER_RE.search(text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _graphql_error_text(stdout: str) -> str | None:
    """GraphQL can answer HTTP 200 (exit 0) with an error body when rate
    limited. Returns the error text if stdout is a JSON object with a
    non-empty top-level `errors` (or a bare `message` and no `data`), else
    None. Parsed, never substring-matched: successful output may contain any
    text (repo names, titles)."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        parts = []
        for err in errors:
            if isinstance(err, dict):
                parts.append(f"{err.get('type') or ''} {err.get('message') or ''}".strip())
            else:
                parts.append(str(err))
        return "; ".join(parts) or "errors"
    if "data" not in data and isinstance(data.get("message"), str):
        return data["message"]
    return None


def _is_graphql_call(args: list[str]) -> bool:
    """gh repo/issue subcommands and `gh api graphql` go through GraphQL.
    `gh api <rest path>` does not: a REST error is a non-zero exit."""
    if len(args) < 2:
        return False
    if args[1] in ("repo", "issue"):
        return True
    return args[1] == "api" and len(args) > 2 and args[2] == "graphql"


def _looks_silently_rate_limited(stdout: str) -> bool:
    """True if a successful GraphQL call's body is really a rate-limit error."""
    text = _graphql_error_text(stdout)
    return text is not None and _is_rate_limited(text)


def _is_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(pat in lowered for pat in _RATE_LIMIT_PATTERNS)


def _is_auth_failure(text: str) -> bool:
    lowered = text.lower()
    return any(pat in lowered for pat in _AUTH_FAILURE_PATTERNS)


def gh_call(
    args: list[str],
    ctx: GhContext,
    cost: CallCost = COST_READ,
    timeout: int = SUBPROCESS_TIMEOUT,
    max_retries: int = MAX_RATE_LIMIT_RETRIES,
) -> tuple[bool, str, str]:
    """max_retries=0 makes a single attempt. Use it for non-idempotent writes
    (issue create, comment) whose callers re-check before retrying."""
    delay = SECONDARY_RATE_LIMIT_SLEEP

    for attempt in range(max_retries + 1):
        maybe_pause_for_rate_limit(ctx)
        ctx.throttle.acquire(cost)
        success, stdout, stderr = _run_subprocess(args, ctx.env, timeout=timeout)

        if success:
            # Only GraphQL can fail with exit 0. REST bodies (check runs,
            # commit messages, titles) may mention rate limits harmlessly.
            silent = _graphql_error_text(stdout) if _is_graphql_call(args) else None
            if silent is None:
                return True, stdout, stderr
            combined = (stderr + " " + silent).lower()
        else:
            combined = (stderr + stdout).lower()

        if any(pat in combined for pat in _AUTH_FAILURE_PATTERNS):
            print(stderr, file=sys.stderr)
            return False, stdout, stderr

        is_rate_limited = any(pat in combined for pat in _RATE_LIMIT_PATTERNS)
        if not is_rate_limited:
            print(stderr, file=sys.stderr)
            return False, stdout, stderr

        if attempt == max_retries:
            if max_retries > 0:
                print(
                    f"Rate limit retries exhausted ({max_retries}). Giving up.",
                    file=sys.stderr,
                )
            print(stderr, file=sys.stderr)
            return False, stdout, stderr

        retry_after = _parse_retry_after(combined)
        is_secondary = any(pat in combined for pat in _SECONDARY_RATE_LIMIT_PATTERNS)

        if is_secondary or retry_after is not None:
            block_for = retry_after if retry_after is not None else delay
            print(
                f"Secondary rate limit (attempt {attempt + 1}/{max_retries}). "
                f"Pausing all workers {block_for}s...",
                file=sys.stderr,
            )
            ctx.throttle.register_block(block_for)
            _sleep(block_for)
            delay = min(delay * 2, SECONDARY_BACKOFF_CAP)
        elif query_rate_limit(ctx):
            with ctx.state.lock:
                reset_epoch = ctx.state.reset_epoch
            sleep_for = max(reset_epoch - int(time.time()) + 1, 30)
            print(f"Primary rate limit. Pausing all workers {sleep_for}s until reset...", file=sys.stderr)
            ctx.throttle.register_block(sleep_for)
            _sleep(sleep_for)
        else:
            print("Rate limited (could not query reset). Pausing all workers 30s...", file=sys.stderr)
            ctx.throttle.register_block(30)
            _sleep(30)

        if STOP.is_set():
            # Stopping: do not keep a worker retrying through a back-off.
            return False, stdout, stderr

    return False, "", ""


def parse_json_safe(stdout: str, context: str = "") -> object | None:
    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        print(f"Error: Failed to parse JSON ({context}): {exc}", file=sys.stderr)
        return None


def fetch_veracode_profiles(region: str) -> set[str]:
    try:
        import requests
        from veracode_api_signing.plugin_requests import RequestsAuthPluginVeracodeHMAC
    except ImportError as exc:
        raise VeracodeError(
            "Missing Python packages. Install with: "
            "pip install veracode-api-signing requests"
        ) from exc

    base_url = VERACODE_REGIONS.get(region)
    if not base_url:
        raise VeracodeError(f"Unknown Veracode region: {region!r}")

    if not os.environ.get("VERACODE_API_KEY_ID") and not os.path.isfile(
        os.path.expanduser("~/.veracode/credentials")
    ):
        raise VeracodeError(
            "No Veracode credentials found. Set VERACODE_API_KEY_ID/"
            "VERACODE_API_KEY_SECRET env vars or create ~/.veracode/credentials"
        )

    auth = RequestsAuthPluginVeracodeHMAC()
    headers = {"User-Agent": "vcbaseline-trigger-script"}
    profile_names: set[str] = set()
    page = 0
    total_pages: int | None = None

    print(f"Fetching Veracode application profiles from {base_url}...")

    while True:
        url = f"{base_url}/applications"
        params = {"page": page, "size": VERACODE_PAGE_SIZE}

        last_exc: Exception | None = None
        response = None
        for attempt in range(1, VERACODE_MAX_RETRIES + 1):
            try:
                response = requests.get(
                    url, auth=auth, headers=headers, params=params,
                    timeout=VERACODE_TIMEOUT,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == VERACODE_MAX_RETRIES:
                        break  # no point sleeping before giving up
                    retry_after = response.headers.get("Retry-After")
                    try:
                        sleep_for = int(retry_after) if retry_after else VERACODE_RETRY_SLEEP * attempt
                    except ValueError:
                        sleep_for = VERACODE_RETRY_SLEEP * attempt
                    print(
                        f"  Veracode HTTP {response.status_code} on page {page} "
                        f"(attempt {attempt}/{VERACODE_MAX_RETRIES}). "
                        f"Retrying in {sleep_for}s...",
                        file=sys.stderr,
                    )
                    time.sleep(sleep_for)
                    continue
                break
            except requests.RequestException as exc:
                last_exc = exc
                print(
                    f"  Veracode request error on page {page} "
                    f"(attempt {attempt}/{VERACODE_MAX_RETRIES}): {exc}",
                    file=sys.stderr,
                )
                if attempt < VERACODE_MAX_RETRIES:
                    time.sleep(VERACODE_RETRY_SLEEP * attempt)

        if response is None:
            raise VeracodeError(f"Veracode request failed after retries: {last_exc}")

        if not response.ok:
            raise VeracodeError(
                f"Veracode API returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise VeracodeError(f"Failed to parse Veracode response: {exc}") from exc

        embedded = data.get("_embedded") or {}
        apps = embedded.get("applications") or []
        for app in apps:
            profile = app.get("profile") or {}
            name = profile.get("name")
            if name:
                profile_names.add(name.strip().lower())

        page_meta = data.get("page") or {}
        if total_pages is None:
            total_pages = page_meta.get("total_pages", 0)
            total_elements = page_meta.get("total_elements", "?")
            print(f"  Total profiles to fetch: {total_elements} across {total_pages} page(s)")

        page += 1
        if not apps or (total_pages is not None and page >= total_pages):
            break

    if not profile_names:
        raise VeracodeError(
            "Veracode returned 0 application profiles. Refusing to continue: the "
            "skip check would match nothing and every repo would be triggered."
        )

    print(f"  Loaded {len(profile_names)} unique Veracode profile name(s).")
    return profile_names


def build_veracode_profile_name(repo_full_name: str) -> str:
    """The Workflow App always names profiles '<org>/<repo>'."""
    return repo_full_name.strip().lower()


def check_org_access(org: str, ctx: GhContext) -> bool:
    success, _, _ = gh_call(["gh", "api", f"orgs/{org}", "--silent"], ctx, cost=COST_READ)
    return success


def _check_and_report(org: str, ctx: GhContext) -> bool:
    print(f"  Checking {org}...", end=" ", flush=True)
    ok = check_org_access(org, ctx)
    print("OK" if ok else "FAILED")
    return ok


def fetch_repos(org: str, limit: int, ctx: GhContext) -> list[dict] | None:
    """Repos of org, [] for an empty org, None if the list call failed."""
    success, stdout, _ = gh_call(
        [
            "gh", "repo", "list", org,
            "--limit", str(limit),
            "--json", "nameWithOwner,hasIssuesEnabled,primaryLanguage,isArchived,defaultBranchRef",
            "--jq", _REPO_JQ,
        ],
        ctx,
        cost=COST_READ,
        timeout=REPO_LIST_TIMEOUT,
    )
    if not success:
        print(f"Error: Failed to fetch repos for org '{org}'.", file=sys.stderr)
        return None
    result = parse_json_safe(stdout, f"fetch_repos org={org}")
    if not isinstance(result, list):
        if not stdout.strip():
            return []
        return None
    if len(result) >= limit:
        print(
            f"Warning: org '{org}' returned {len(result)} repos, equal to --repo-limit "
            f"({limit}). The list may be truncated. Raise --repo-limit.",
            file=sys.stderr,
        )
    return result


# Same shape as _REPO_JQ, from the REST repo object.
_ONE_REPO_JQ = (
    "{nameWithOwner: .full_name, hasIssuesEnabled: .has_issues, "
    "primaryLanguage: (.language // \"N/A\"), isArchived: .archived, "
    "defaultBranch: (.default_branch // \"\")}"
)
REPO_NOT_FOUND = "not_found"


def fetch_repo(full_name: str, ctx: GhContext) -> dict | str | None:
    """One repo by name: its dict, REPO_NOT_FOUND on 404, None on other errors.
    Exact targets are fetched this way so they are found even beyond
    --repo-limit, and missing ones can be reported."""
    ok, out, err = gh_call(
        ["gh", "api", f"repos/{full_name}", "--jq", _ONE_REPO_JQ], ctx, cost=COST_READ,
    )
    if not ok:
        lowered = (err + out).lower()
        if "http 404" in lowered or "not found" in lowered:
            return REPO_NOT_FOUND
        return None
    data = parse_json_safe(out, f"fetch_repo {full_name}")
    if isinstance(data, dict) and data.get("nameWithOwner"):
        return data
    return None


def resolve_targets(
    org: str,
    names: list[str],
    limit: int,
    ctx: GhContext,
    skip: set[str] | None = None,
) -> tuple[list[dict] | None, list[tuple[str, str]]]:
    """Repos for an explicit target list (--repo-file / --org-repo-file).

    Exact names are fetched directly; the org is only listed when the list has
    glob entries (and exact names the listing missed are still fetched). skip
    holds lower-cased org/repo names to leave out (--resume).

    Returns (repos, missing) where missing is [(org/repo, action)] with action
    'not_found' or 'failed_repo_lookup'. repos is None if a needed org listing
    failed."""
    skip = skip or set()
    globs = [n for n in names if _has_glob(n)]
    exact: list[str] = []
    seen: set[str] = set()
    for n in names:
        if not _has_glob(n) and n.lower() not in seen:
            seen.add(n.lower())
            exact.append(n)

    repos: list[dict] = []
    if globs:
        listing = fetch_repos(org, limit, ctx)
        if listing is None:
            return None, []
        repos = filter_repos_by_names(listing, names)
    have = {r["nameWithOwner"].split("/", 1)[-1].lower() for r in repos}

    missing: list[tuple[str, str]] = []
    for n in exact:
        full = f"{org}/{n}"
        if n.lower() in have or full.lower() in skip:
            continue
        found = fetch_repo(full, ctx)
        if isinstance(found, dict):
            repos.append(found)
            have.add(n.lower())
        else:
            missing.append((full, "not_found" if found == REPO_NOT_FOUND else "failed_repo_lookup"))
    return repos, missing


def _query_open_issues(
    repo: str,
    title: str,
    ctx: GhContext,
    author: str | None = None,
    match_body: bool = False,
) -> tuple[list[int] | None, str]:
    """find_open_issues plus the raw stderr, so callers can tell 'Issues are
    disabled' (HTTP 410) apart from other errors."""
    cond = f".pull_request == null and .title == {json.dumps(title)}"
    if match_body:
        cond += r' and ((.body // "") | gsub("^\\s+|\\s+$"; "")) == ' + json.dumps(ISSUE_BODY)
    args = [
        "gh", "api", f"repos/{repo}/issues",
        "-X", "GET",
        "-f", "state=open",
        "-f", "per_page=100",
    ]
    if author:
        args += ["-f", f"creator={author}"]
    args += ["--paginate", "--jq", f".[] | select({cond}) | .number"]
    success, stdout, stderr = gh_call(args, ctx, cost=COST_READ, timeout=PAGINATE_TIMEOUT)
    if not success:
        return None, stderr
    numbers: list[int] = []
    for line in stdout.split():
        try:
            numbers.append(int(line))
        except ValueError:
            print(
                f"Error: unexpected output from issue lookup (repo={repo}): {line[:100]!r}",
                file=sys.stderr,
            )
            return None, stderr
    return sorted(set(numbers)), stderr


def find_open_issues(
    repo: str,
    title: str,
    ctx: GhContext,
    author: str | None = None,
    match_body: bool = False,
) -> list[int] | None:
    """Lists open issues directly (REST, paginated) and matches the title exactly.

    Deliberately NOT `gh issue list --search`: search is index-backed and lags,
    so a just-created issue can be invisible for seconds or longer. That lag
    let re-runs and the read-back check miss existing issues and create
    duplicates. The REST issues list also returns pull requests (items with a
    `pull_request` key), which are filtered out.

    author filters server-side by creator; match_body also requires the body
    to equal ISSUE_BODY (whitespace-trimmed).

    Returns issue numbers sorted oldest first, [] if none, None on API error."""
    return _query_open_issues(repo, title, ctx, author, match_body)[0]


def verify_issue_created(repo: str, ctx: GhContext, log: RepoLogger) -> bool | None:
    """GitHub's issue-creation limit is undisclosed and can silently drop or hide
    a creation while still returning success. Read it back, a few times.
    True = visible, False = not visible, None = could not verify."""
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        _sleep(VERIFY_DELAY_SECONDS * attempt)
        nums = find_open_issues(repo, ISSUE_TITLE, ctx)
        if nums is None:
            log.warn("Could not verify issue creation (API error).")
            return None
        if nums:
            return True
    log.warn(
        f"Create reported success but the issue is not visible after "
        f"{VERIFY_ATTEMPTS} checks. Treating as FAILED."
    )
    return False


def _write_backoff(attempt: int, stdout: str, stderr: str) -> int:
    """Back-off before retrying a non-idempotent write."""
    retry_after = _parse_retry_after((stderr + stdout).lower())
    if retry_after is not None:
        return retry_after
    return min(SECONDARY_RATE_LIMIT_SLEEP * (2 ** attempt), SECONDARY_BACKOFF_CAP)


def _retryable_write_failure(stdout: str, stderr: str) -> bool:
    """Rate limits and timeouts may or may not have landed; worth a re-check and
    retry. Anything else (auth, validation, 404) will not succeed on retry."""
    combined = stderr + stdout
    if _is_auth_failure(combined):
        return False
    return stderr == "timeout" or _is_rate_limited(combined)


def create_trigger_issue(name: str, ctx: GhContext, log: RepoLogger) -> str:
    """Creates the trigger issue without ever blindly resending the create.

    A create can fail on our side (rate-limit response, timeout) after GitHub
    already stored the issue. Retrying straight away was a source of
    duplicates, so before every retry the repo is re-checked and the retry is
    skipped if the issue is already there.

    Returns the CSV action string."""
    attempted = False

    def landed_anyway(why: str) -> bool:
        """After an ambiguous failure: is the issue there? Errors read as no."""
        existing = find_open_issues(name, ISSUE_TITLE, ctx)
        if existing:
            log.log(f"Issue #{existing[0]} exists despite the {why}. Not creating another.")
            return True
        return False

    try:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            check_stop()
            attempted = True
            success, stdout, stderr = gh_call(
                [
                    "gh", "issue", "create", "--repo", name,
                    "--title", ISSUE_TITLE, "--body", ISSUE_BODY,
                ],
                ctx,
                cost=COST_CONTENT,
                max_retries=0,
            )

            if success:
                verified = verify_issue_created(name, ctx, log)
                if verified is False:
                    return "failed_silent_block"
                return "created" if verified else "created_unverified"

            if not _retryable_write_failure(stdout, stderr):
                # A 5xx or dropped connection may still have stored the issue.
                # Auth/permission failures cannot have.
                if not _is_auth_failure(stderr + stdout) and landed_anyway("error"):
                    return "created"
                log.log("Failed to create issue.")
                return "failed_create"

            block_for = _write_backoff(attempt, stdout, stderr)
            log.warn(
                f"Create hit a rate limit or timeout (attempt {attempt + 1}). "
                f"Waiting {block_for}s, then checking whether it landed anyway."
            )
            ctx.throttle.register_block(block_for)
            _sleep(block_for)

            existing = find_open_issues(name, ISSUE_TITLE, ctx)
            if existing is None:
                log.log("Could not re-check for the issue (API error). Not retrying, to avoid a duplicate.")
                return "failed_create_unknown"
            if existing:
                log.log(f"Issue #{existing[0]} exists despite the error. Not creating another.")
                return "created"
    except Exception:
        # Crash or Ctrl-C after a create was sent: report what really happened.
        if attempted and landed_anyway("interruption"):
            return "created"
        raise

    log.log("Failed to create issue after retries.")
    return "failed_create"


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_trigger_comments_since(
    repo: str, issue: int, since_ts: float, ctx: GhContext,
) -> list[int] | None:
    """IDs of comments on `issue` whose body is exactly ISSUE_BODY, updated after
    since_ts. None on API error."""
    jq = f".[] | select(.body == {json.dumps(ISSUE_BODY)}) | .id"
    success, stdout, _ = gh_call(
        [
            "gh", "api", f"repos/{repo}/issues/{issue}/comments",
            "-X", "GET",
            "-f", f"since={_utc_iso(since_ts)}",
            "-f", "per_page=100",
            "--paginate",
            "--jq", jq,
        ],
        ctx,
        cost=COST_READ,
    )
    if not success:
        return None
    try:
        return [int(x) for x in stdout.split()]
    except ValueError:
        return None


def comment_trigger_issue(name: str, issue: int, ctx: GhContext, log: RepoLogger) -> str:
    """Comments ISSUE_BODY on an existing trigger issue to re-trigger the scan.
    Same no-blind-retry rule as create_trigger_issue: a duplicate comment would
    start a duplicate scan. Returns the CSV action string."""
    # Margin covers clock skew between this machine and GitHub. A matching
    # comment from anyone else in that window has already triggered a scan,
    # so treating it as done is correct.
    since_ts = time.time() - COMMENT_SINCE_MARGIN_SECONDS

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        check_stop()
        success, stdout, stderr = gh_call(
            [
                "gh", "issue", "comment", str(issue), "--repo", name,
                "--body", ISSUE_BODY,
            ],
            ctx,
            cost=COST_CONTENT,
            max_retries=0,
        )
        if success:
            return "commented_existing"

        if not _retryable_write_failure(stdout, stderr):
            log.log(f"Failed to comment on issue #{issue}.")
            return "failed_comment"

        block_for = _write_backoff(attempt, stdout, stderr)
        log.warn(
            f"Comment hit a rate limit or timeout (attempt {attempt + 1}). "
            f"Waiting {block_for}s, then checking whether it landed anyway."
        )
        ctx.throttle.register_block(block_for)
        _sleep(block_for)

        landed = find_trigger_comments_since(name, issue, since_ts, ctx)
        if landed is None:
            log.log("Could not re-check comments (API error). Not retrying, to avoid a duplicate scan.")
            return "failed_comment_unknown"
        if landed:
            log.log("Comment exists despite the error. Not posting another.")
            return "commented_existing"

    log.log(f"Failed to comment on issue #{issue} after retries.")
    return "failed_comment"


class CheckLookupError(Exception):
    """The last-scan lookup for --stale-days failed (API error, missing default
    branch, unparsable output). The repo must not be treated as stale."""


def get_last_veracode_check(repo: str, branch: str, ctx: GhContext) -> datetime | None:
    """Most recent completion of a check run named veracode* on the head of
    the default branch; None if there is none. Raises CheckLookupError when
    the lookup itself fails, so an error never reads as 'never scanned'.

    Paginated, and filtered with --jq so only completion times come back.

    Note: check runs only exist for the head commit of the default branch. A
    repo committed to since its last scan will report no checks and be treated
    as stale."""
    if not branch:
        raise CheckLookupError("no default branch (empty repository?)")

    ref = urllib.parse.quote(branch, safe="/")
    success, stdout, stderr = gh_call(
        [
            "gh", "api", f"repos/{repo}/commits/{ref}/check-runs",
            "-X", "GET",
            "-f", "per_page=100",
            "--paginate",
            "--jq", _CHECK_RUNS_JQ,
        ],
        ctx,
        cost=COST_READ,
        timeout=PAGINATE_TIMEOUT,
    )
    if not success:
        raise CheckLookupError(f"check-runs lookup failed: {stderr.strip()[:200]}")

    completed_times: list[datetime] = []
    for line in stdout.split():
        try:
            completed_times.append(datetime.fromisoformat(line.replace("Z", "+00:00")))
        except ValueError as exc:
            raise CheckLookupError(f"unexpected check-runs output: {line[:100]!r}") from exc

    return max(completed_times) if completed_times else None


def calculate_days_since(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).days


CREATE_FIELDNAMES = [
    "org", "repo", "primary_language", "issues_enabled", "is_archived",
    "veracode_profile_exists", "last_check_date", "days_since_check", "action",
]

DELETE_FIELDNAMES = [
    "org", "repo", "primary_language", "is_archived",
    "veracode_profile_exists", "issues_deleted", "action",
]


def _row_create(
    org: str,
    repo: dict,
    action: str,
    veracode_profile_exists: str = "",
    last_check_date: str = "",
    days_since_check: str = "",
) -> dict:
    return {
        "org": org,
        "repo": repo["nameWithOwner"],
        "primary_language": repo["primaryLanguage"],
        "issues_enabled": repo["hasIssuesEnabled"],
        "is_archived": repo["isArchived"],
        "veracode_profile_exists": veracode_profile_exists,
        "last_check_date": last_check_date,
        "days_since_check": days_since_check,
        "action": action,
    }


def _row_delete(
    org: str,
    repo: dict,
    issues_deleted: int,
    action: str,
    veracode_profile_exists: str = "",
) -> dict:
    return {
        "org": org,
        "repo": repo["nameWithOwner"],
        "primary_language": repo["primaryLanguage"],
        "is_archived": repo["isArchived"],
        "veracode_profile_exists": veracode_profile_exists,
        "issues_deleted": issues_deleted,
        "action": action,
    }


def read_has_issues(repo: str, ctx: GhContext) -> bool | None:
    """Current has_issues setting, or None if it could not be read."""
    ok, out, _ = gh_call(["gh", "api", f"repos/{repo}", "--jq", ".has_issues"], ctx, cost=COST_READ)
    value = out.strip().lower() if ok else ""
    return {"true": True, "false": False}.get(value)


def _restore_issues_disabled(name: str, ctx: GhContext, log: RepoLogger) -> bool:
    log.log("Restoring state: Disabling issues...")
    success, out, err = gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], ctx, cost=COST_WRITE)
    if not success and _retryable_write_failure(out, err) and read_has_issues(name, ctx) is False:
        log.log("Restore errored but Issues are disabled.")
        success = True
    if not success:
        log.warn(f"Failed to restore issues-disabled state on {name}")
    return success


def _issues_disabled_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return "http 410" in lowered or "issues are disabled" in lowered


class IssuesToggle:
    """Temporarily enables Issues on one repo and guarantees the restore."""

    def __init__(self, name: str, ctx: GhContext, log: RepoLogger) -> None:
        self.name = name
        self.ctx = ctx
        self.log = log
        self.needs_restore = False
        self.restore_ok: bool | None = None

    def enable(self) -> bool:
        check_stop()
        ok, out, err = gh_call(
            ["gh", "repo", "edit", self.name, "--enable-issues"], self.ctx, cost=COST_WRITE,
        )
        if ok:
            self.needs_restore = True
            return True
        if _retryable_write_failure(out, err):
            # A timeout or rate-limit response may still have landed.
            if read_has_issues(self.name, self.ctx) is not False:
                self.log.warn("Enable errored but Issues may now be on. Restoring.")
                self.needs_restore = True
        return False

    def restore(self) -> None:
        if not self.needs_restore:
            return
        self.needs_restore = False
        try:
            self.restore_ok = _restore_issues_disabled(self.name, self.ctx, self.log)
        except Exception as exc:  # noqa: BLE001 - must never escape a worker
            self.log.warn(f"Restore crashed: {exc!r}")
            self.restore_ok = False


def _guard_repo(log: RepoLogger, deltas: dict[str, int], toggle: IssuesToggle, body) -> str:
    """Runs body() -> action with the Issues restore in a finally, so no path
    (early return, interrupt, unexpected exception) leaves Issues enabled.
    A failed restore is appended to the action and counted on every path."""
    try:
        action = body()
    except Aborted:
        log.warn("Run interrupted. Stopped before the next write.")
        deltas["aborted"] = 1
        action = "aborted"
    except Exception as exc:  # noqa: BLE001
        log.warn(f"Unexpected error: {exc!r}")
        deltas["crashed"] = 1
        action = "worker_crashed"
    finally:
        toggle.restore()
    if toggle.restore_ok is False:
        deltas["restore_failed"] = 1
        action = f"{action}_restore_failed"
    return action


def read_issue_state(repo: str, issue: int, ctx: GhContext) -> str | None:
    """'open' / 'closed', or None if it could not be read."""
    ok, out, _ = gh_call(
        ["gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".state"],
        ctx, cost=COST_READ,
    )
    state = out.strip().lower() if ok else ""
    return state if state in ("open", "closed") else None


def close_issue(
    repo: str, issue: int, ctx: GhContext, log: RepoLogger, comment: str | None = None,
) -> bool:
    """Closes one issue via REST PATCH (state=closed, not_planned).

    The PATCH is idempotent, so it may be retried (gh_call retries rate limits).
    A timeout may have landed, so the state is read back before retrying or
    counting the close as failed. The optional comment is a content write and
    is sent exactly once (max_retries=0); a failed comment does not stop the
    close."""
    if comment:
        ok, _, _ = gh_call(
            [
                "gh", "api", "-X", "POST", f"repos/{repo}/issues/{issue}/comments",
                "-f", f"body={comment}", "--silent",
            ],
            ctx, cost=COST_CONTENT, max_retries=0,
        )
        if not ok:
            log.warn(f"Could not comment on issue #{issue}. Closing it anyway (comment not retried).")

    for attempt in range(1, CLOSE_ATTEMPTS + 1):
        if attempt > 1:
            check_stop()
        ok, out, err = gh_call(
            [
                "gh", "api", "-X", "PATCH", f"repos/{repo}/issues/{issue}",
                "-f", "state=closed", "-f", "state_reason=not_planned", "--silent",
            ],
            ctx, cost=COST_CLOSE,
        )
        if ok:
            return True
        if not _retryable_write_failure(out, err):
            return False
        state = read_issue_state(repo, issue, ctx)
        if state == "closed":
            log.log(f"Close of #{issue} errored but the issue is closed.")
            return True
        if state is None:
            log.warn(f"Close of #{issue} errored and its state could not be read back.")
            return False
        if attempt < CLOSE_ATTEMPTS:
            log.warn(f"Close of #{issue} errored and it is still open. Retrying.")
    return False


def _process_delete_repo(
    org: str,
    repo: dict,
    ctx: GhContext,
    veracode_profiles: set[str] | None,
    dry_run: bool,
    opts: DeleteOptions | None = None,
) -> RepoResult:
    opts = opts or DeleteOptions()
    name = repo["nameWithOwner"]
    issues_enabled = repo["hasIssuesEnabled"]
    log = RepoLogger(name)
    deltas: dict[str, int] = {"total_repos": 1}

    vc_exists_str = ""
    check_veracode = veracode_profiles is not None

    if repo["isArchived"]:
        deltas["archived"] = 1
        log.log("Repository is archived. Skipping.")
        return RepoResult(
            name, _row_delete(org, repo, 0, "skipped_archived"), log.lines, deltas,
        )

    if check_veracode:
        expected_profile = build_veracode_profile_name(name)
        profile_exists = expected_profile in veracode_profiles
        vc_exists_str = "true" if profile_exists else "false"
        if profile_exists:
            log.log(f"Veracode profile '{name}' exists on platform. Skipping close.")
            deltas["skipped_veracode_profile"] = 1
            return RepoResult(
                name,
                _row_delete(org, repo, 0, "skipped_veracode_profile_exists", vc_exists_str),
                log.lines, deltas,
            )

    toggle = IssuesToggle(name, ctx, log)
    closed = {"n": 0}

    def body() -> str:
        check_stop()
        lookup = {"author": opts.issue_author, "match_body": opts.match_body}
        # On a repo with Issues disabled the LIST endpoint answers 200 [] even
        # when issues exist (only single-issue GETs return 410), so an empty
        # list proves nothing there: a disabled repo is always enabled, listed,
        # closed and restored. Repos listed as enabled are listed first; a 410
        # (Issues turned off since the repo listing) falls back to the toggle.
        nums: list[int] | None = None
        needs_toggle = not issues_enabled
        if not needs_toggle:
            nums, err = _query_open_issues(name, ISSUE_TITLE, ctx, **lookup)
            if nums is None and _issues_disabled_error(err):
                needs_toggle = True
            elif nums is None:
                log.log("Could not query issues (API error). Skipping.")
                deltas["query_failed"] = 1
                return "failed_query_issues"
            elif not nums:
                log.log("No matching issues found.")
                deltas["skipped_no_issues"] = 1
                return "no_issues_found"

        if dry_run:
            if nums:
                deltas["would_close"] = len(nums)
                closed["n"] = len(nums)
            if needs_toggle:
                deltas["needs_enable"] = 1
                log.log("[DRY RUN] Issues disabled. Would enable, close any trigger issues, then re-disable.")
                return "dry_run_needs_issues_enabled"
            log.log(f"[DRY RUN] Would close issue(s): {nums}")
            return "dry_run_would_close"

        if needs_toggle:
            log.log("Issues are disabled. Temporarily enabling to find and close trigger issues...")
            if not toggle.enable():
                log.log("Could not enable issues. Skipping.")
                deltas["enable_failed"] = 1
                return "failed_enable_issues"
            if nums is None:
                nums, _ = _query_open_issues(name, ISSUE_TITLE, ctx, **lookup)
                if nums is None:
                    log.log("Could not query issues (API error). Skipping.")
                    deltas["query_failed"] = 1
                    return "failed_query_issues"
                if not nums:
                    log.log("No matching issues found.")
                    deltas["skipped_no_issues"] = 1
                    return "no_issues_found"

        failed = 0
        for issue_num in nums:
            check_stop()
            log.log(f"Closing issue #{issue_num}...")
            if close_issue(name, issue_num, ctx, log, opts.close_comment):
                closed["n"] += 1
                deltas["deleted"] = deltas.get("deleted", 0) + 1
            else:
                log.log(f"Failed to close issue #{issue_num}")
                failed += 1
                deltas["close_failed"] = deltas.get("close_failed", 0) + 1

        if failed == 0:
            return "deleted"
        return "partial_delete" if closed["n"] else "failed_close"

    action = _guard_repo(log, deltas, toggle, body)
    return RepoResult(
        name,
        _row_delete(org, repo, closed["n"], action, vc_exists_str),
        log.lines, deltas,
    )


def _process_create_repo(
    org: str,
    repo: dict,
    ctx: GhContext,
    stale_days: int | None,
    veracode_profiles: set[str] | None,
    gate: CreateGate,
    inflight: InflightTracker,
    dry_run: bool,
    rescan_existing: bool = False,
) -> RepoResult:
    name = repo["nameWithOwner"]
    issues_enabled = repo["hasIssuesEnabled"]
    primary_lang = repo["primaryLanguage"]
    is_archived = repo["isArchived"]

    log = RepoLogger(name)
    deltas: dict[str, int] = {"total_repos": 1}

    last_check_date = ""
    days_since_check = ""
    vc_exists_str = ""

    check_stale = stale_days is not None and stale_days > 0
    check_veracode = veracode_profiles is not None

    if is_archived:
        deltas["archived"] = 1
        log.log("Repository is archived. Skipping.")
        return RepoResult(
            name, _row_create(org, repo, "skipped_archived"), log.lines, deltas,
        )

    if primary_lang in IAC_LANGUAGES:
        deltas["iac"] = 1

    if check_veracode:
        expected_profile = build_veracode_profile_name(name)
        profile_exists = expected_profile in veracode_profiles
        vc_exists_str = "true" if profile_exists else "false"
        if profile_exists:
            log.log(f"Veracode profile '{name}' already exists on platform. Skipping.")
            deltas["skipped_veracode_profile"] = 1
            return RepoResult(
                name,
                _row_create(org, repo, "skipped_veracode_profile_exists", vc_exists_str),
                log.lines, deltas,
            )

    if check_stale:
        log.log(f"Checking for Veracode checks in the last {stale_days} days...")
        default_branch = repo.get("defaultBranch") or ""
        try:
            last_check = get_last_veracode_check(name, default_branch, ctx)
        except CheckLookupError as exc:
            # Fail closed: an unknown scan date must not trigger a scan.
            log.warn(f"Could not determine the last Veracode check ({exc}). Skipping.")
            deltas["stale_check_failed"] = 1
            return RepoResult(
                name,
                _row_create(org, repo, "failed_stale_check", vc_exists_str),
                log.lines, deltas,
            )
        days_since = calculate_days_since(last_check)

        if last_check:
            last_check_date = last_check.strftime("%Y-%m-%d %H:%M:%S UTC")
        if days_since is not None:
            days_since_check = str(days_since)

        if last_check and days_since is not None and days_since < stale_days:
            log.log(f"Recent Veracode check found ({days_since} days ago). Skipping.")
            deltas["skipped_recent"] = 1
            return RepoResult(
                name,
                _row_create(
                    org, repo, "skipped_recent_check",
                    vc_exists_str, last_check_date, days_since_check,
                ),
                log.lines, deltas,
            )

        if last_check:
            log.log(f"Last Veracode check was {days_since} days ago. Proceeding.")
        else:
            log.log("No Veracode checks found on the default branch head. Proceeding.")

    if dry_run and not issues_enabled:
        log.log("[DRY RUN] Issues disabled. Would enable, create, then re-disable.")
        deltas["needs_enable"] = 1
        return RepoResult(
            name,
            _row_create(
                org, repo, "dry_run_needs_issues_enabled",
                vc_exists_str, last_check_date, days_since_check,
            ),
            log.lines, deltas,
        )

    toggle = IssuesToggle(name, ctx, log)

    def body() -> str:
        check_stop()
        if not issues_enabled:
            log.log("Issues are disabled. Temporarily enabling...")
            if not toggle.enable():
                log.log("Could not enable issues. Skipping issue creation.")
                deltas["skipped_perm"] = 1
                return "skipped_cant_enable_issues"

        existing_issues = find_open_issues(name, ISSUE_TITLE, ctx)

        if existing_issues is None:
            log.log("Could not query existing issues (API error). Skipping to avoid duplicates.")
            deltas["query_failed"] = 1
            return "failed_query_issues"

        if len(existing_issues) > 1:
            log.warn(
                f"{len(existing_issues)} open '{ISSUE_TITLE}' issues already exist "
                f"(#{', #'.join(map(str, existing_issues))}). Duplicates from an earlier run."
            )

        if existing_issues and not rescan_existing:
            log.log(f"Open trigger issue #{existing_issues[0]} already exists. Skipping.")
            deltas["skipped_existing"] = 1
            return "skipped_existing_issue"

        if dry_run:
            if existing_issues:
                log.log(f"[DRY RUN] Would comment on existing issue #{existing_issues[0]} to rescan.")
                deltas["commented"] = 1
                return "dry_run_would_comment"
            log.log("[DRY RUN] Would create issue.")
            deltas["created"] = 1
            return "dry_run_would_create"

        def recheck() -> str | None:
            """The waits can be long (in-flight cap, batch pause, gate). Look
            again so an issue opened meanwhile (another run, a person) is not
            duplicated. None = go ahead, else the action to record."""
            nonlocal existing_issues
            if existing_issues:
                return None  # commenting on a known issue; nothing to duplicate
            found = find_open_issues(name, ISSUE_TITLE, ctx)
            if found is None:
                log.log("Could not re-check existing issues (API error). Skipping to avoid duplicates.")
                deltas["query_failed"] = 1
                return "failed_query_issues"
            if found and not rescan_existing:
                log.log(f"Trigger issue #{found[0]} appeared while waiting. Skipping.")
                deltas["skipped_existing"] = 1
                return "skipped_existing_issue"
            existing_issues = found
            return None

        if not inflight.reserve(org, name, log):
            deltas["failed"] = 1
            return "failed_inflight_wait"
        action = "aborted"
        try:
            early = recheck()  # before queueing for the gate
            if early is not None:
                action = early
                return action
            inside: dict[str, str | None] = {}

            def gate_check() -> bool:
                inside["action"] = recheck()  # once more, right before writing
                return inside["action"] is None

            if not gate.wait(log, recheck=gate_check):
                action = inside["action"] or "failed_query_issues"
                return action
            check_stop()

            if existing_issues:
                target = existing_issues[0]
                log.log(f"Commenting on existing issue #{target} to rescan...")
                action = comment_trigger_issue(name, target, ctx, log)
            else:
                log.log("Creating issue...")
                action = create_trigger_issue(name, ctx, log)
        finally:
            if action in ("created", "created_unverified", "commented_existing"):
                inflight.confirm(name)
                gate.record_success(log)
            elif action.endswith("_unknown"):
                inflight.confirm(name)  # may have landed: keep the slot counted
            else:
                inflight.release(name)

        if action in ("created", "created_unverified"):
            deltas["created"] = 1
        elif action == "commented_existing":
            deltas["commented"] = 1
        else:
            deltas["failed"] = 1
        return action

    action = _guard_repo(log, deltas, toggle, body)

    return RepoResult(
        name,
        _row_create(
            org, repo, action,
            vc_exists_str, last_check_date, days_since_check,
        ),
        log.lines, deltas,
    )


def _apply_deltas(stats: OrgStats, deltas: dict[str, int]) -> None:
    for key, value in deltas.items():
        setattr(stats, key, getattr(stats, key) + value)


def _org_row(org: str, action: str) -> dict:
    """Org-level CSV row (empty repo column) for org failures."""
    return {"org": org, "repo": "", "action": action}


# Cells starting with these are run as formulas by Excel/Sheets.
_FORMULA_PREFIXES = ("=", "+", "-", "@", chr(9), chr(13))


def _csv_safe(value):
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _csv_unsafe(value: str) -> str:
    if value.startswith("'") and value[1:].startswith(_FORMULA_PREFIXES):
        return value[1:]
    return value


def _write_row(csv_writer: csv.DictWriter, csv_file, csv_lock: threading.Lock, row: dict) -> None:
    """One row, formula-safe, written and flushed under the lock so a crash
    loses nothing."""
    safe = {key: _csv_safe(value) for key, value in row.items()}
    with csv_lock:
        csv_writer.writerow(safe)
        csv_file.flush()


# Rows with these actions are done; --resume skips the repo. A "_restore_failed"
# suffix keeps the base action's meaning (the restore needs manual cleanup).
TERMINAL_ACTIONS = frozenset({
    "deleted", "no_issues_found", "skipped_archived", "skipped_veracode_profile_exists",
    "created", "created_unverified", "commented_existing", "skipped_existing_issue",
    "skipped_recent_check",
})
_RESTORE_FAILED_SUFFIX = "_restore_failed"


def is_terminal_action(action: str) -> bool:
    if action.endswith(_RESTORE_FAILED_SUFFIX):
        action = action[: -len(_RESTORE_FAILED_SUFFIX)]
    return action in TERMINAL_ACTIONS


def default_output_name(delete_mode: bool, dry_run: bool, now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    mode = "delete" if delete_mode else "create"
    return f"vcbaseline-{mode}{'-dryrun' if dry_run else ''}-{stamp}.csv"


class ResumeError(Exception):
    pass


def load_resume(path: str, fieldnames: list[str]) -> set[str]:
    """Lower-cased org/repo names that already reached a terminal action."""
    if not os.path.isfile(path):
        raise ResumeError(f"--resume file not found: {path}")
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != fieldnames:
            raise ResumeError(
                f"--resume file '{path}' has columns {reader.fieldnames}, expected "
                f"{fieldnames}. Was it written in the other mode?"
            )
        return {
            _csv_unsafe(row["repo"]).lower()
            for row in reader
            if row.get("repo") and is_terminal_action(row.get("action") or "")
        }


def _drive_workers(
    org: str,
    repos: list[dict],
    csv_writer: csv.DictWriter,
    csv_file: io.TextIOWrapper,
    csv_lock: threading.Lock,
    workers: int,
    worker_fn,
) -> OrgStats:
    """Run worker_fn across repos with ThreadPoolExecutor. Aggregate stats and
    flush per-repo logs and CSV rows as each future completes.

    First Ctrl-C: set STOP, cancel repos not yet started, and keep draining the
    running ones (they stop before their next write, restore Issues and return
    an 'aborted' row) so every touched repo reaches the CSV. Returns normally;
    callers check STOP. A second Ctrl-C aborts the drain."""
    stats = OrgStats(org=org)
    total = len(repos)
    completed = 0

    def consume(future, repo_name: str) -> None:
        nonlocal completed
        completed += 1
        try:
            result: RepoResult = future.result()
        except Exception as exc:  # noqa: BLE001
            print(
                f"\n[{completed}/{total}] {repo_name}\n"
                f"  WORKER CRASHED: {exc}",
                file=sys.stderr,
            )
            stats.total_repos += 1
            stats.crashed += 1
            _write_row(
                csv_writer, csv_file, csv_lock,
                {"org": org, "repo": repo_name, "action": "worker_crashed"},
            )
            return

        block = [f"\n[{completed}/{total}] {result.repo_name}"]
        block.extend(f"  {line}" for line in result.logs)
        print("\n".join(block))

        _apply_deltas(stats, result.stat_deltas)
        _write_row(csv_writer, csv_file, csv_lock, result.row)

    executor = ThreadPoolExecutor(max_workers=workers)
    hard_abort = False
    try:
        future_to_repo = {executor.submit(worker_fn, repo): repo for repo in repos}
        pending = set(future_to_repo)
        consumed: set = set()
        while pending:
            try:
                # Short timeout keeps Ctrl-C responsive on Windows.
                done, pending = wait_futures(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    if not future.cancelled() and future not in consumed:
                        consume(future, future_to_repo[future]["nameWithOwner"])
                        consumed.add(future)
            except KeyboardInterrupt:
                if STOP.is_set():
                    raise
                STOP.set()
                print(
                    "\nInterrupted. Stopping workers before their next write and "
                    "restoring Issues settings. Press Ctrl-C again to abort now.",
                    file=sys.stderr,
                )
                for future in future_to_repo:
                    future.cancel()
                pending = {
                    f for f in future_to_repo if not f.cancelled() and f not in consumed
                }
    except KeyboardInterrupt:
        hard_abort = True
        raise
    finally:
        executor.shutdown(wait=not hard_abort, cancel_futures=True)

    return stats


def run_delete_mode(
    org: str,
    repos: list[dict],
    csv_writer: csv.DictWriter,
    csv_file: io.TextIOWrapper,
    csv_lock: threading.Lock,
    ctx: GhContext,
    veracode_profiles: set[str] | None,
    workers: int,
    dry_run: bool,
    opts: DeleteOptions | None = None,
) -> OrgStats:
    check_veracode = veracode_profiles is not None

    def worker(repo: dict) -> RepoResult:
        return _process_delete_repo(org, repo, ctx, veracode_profiles, dry_run, opts)

    stats = _drive_workers(org, repos, csv_writer, csv_file, csv_lock, workers, worker)

    print(f"\nFinished closing issues for org: {org}\n")
    print(f"Delete Stats for Organization: {org}")
    print("----------------------------------------------------------------")
    print(f"Total Repositories:            {stats.total_repos}")
    print(f"Archived Repositories:         {stats.archived}")
    if check_veracode:
        print(f"Skipped VC Profile Hits:       {stats.skipped_veracode_profile}")
    print(f"Repositories with No Issues:   {stats.skipped_no_issues}")
    if dry_run:
        print(f"Would Close (issues):          {stats.would_close}")
        print(f"Would Enable Issues (repos):   {stats.needs_enable}")
    else:
        print(f"Issues Closed:                 {stats.deleted}")
    print(f"Failed Closes:                 {stats.close_failed}")
    print(f"Failed Enables:                {stats.enable_failed}")
    print(f"Failed Queries:                {stats.query_failed}")
    print(f"Failed Restores:               {stats.restore_failed}")
    print(f"Crashed:                       {stats.crashed}")
    if stats.aborted:
        print(f"Aborted (Ctrl-C):              {stats.aborted}")
    print("----------------------------------------------------------------")

    return stats


def run_create_mode(
    org: str,
    repos: list[dict],
    csv_writer: csv.DictWriter,
    csv_file: io.TextIOWrapper,
    csv_lock: threading.Lock,
    ctx: GhContext,
    stale_days: int | None,
    veracode_profiles: set[str] | None,
    workers: int,
    gate: CreateGate,
    inflight: InflightTracker,
    dry_run: bool,
    rescan_existing: bool = False,
) -> OrgStats:
    check_veracode = veracode_profiles is not None

    def worker(repo: dict) -> RepoResult:
        return _process_create_repo(
            org, repo, ctx, stale_days, veracode_profiles, gate, inflight, dry_run,
            rescan_existing,
        )

    stats = _drive_workers(org, repos, csv_writer, csv_file, csv_lock, workers, worker)

    print(f"\nFinished processing all repositories for org: {org}\n")
    print(f"Repository Stats for Organization: {org}")
    print("----------------------------------------------------------------")
    print(f"Total Repositories:       {stats.total_repos}")
    print(f"Archived Repositories:    {stats.archived}")
    print(f"IaC Repositories:         {stats.iac} (Primary language: HCL/Bicep)")
    print(f"Issues Permission Skips:  {stats.skipped_perm}")
    print(f"Skipped Existing Issues:  {stats.skipped_existing}")
    if check_veracode:
        print(f"Skipped VC Profile Hits:  {stats.skipped_veracode_profile}")
    if stale_days:
        print(f"Skipped Recent Checks:    {stats.skipped_recent} (scanned within {stale_days} days)")
    print(f"Created Issues:           {stats.created}")
    print(f"Commented Existing:       {stats.commented}")
    print(f"Failed Creates/Comments:  {stats.failed}")
    print(f"Failed Queries:           {stats.query_failed}")
    if stale_days:
        print(f"Failed Stale Checks:      {stats.stale_check_failed}")
    print(f"Failed Restores:          {stats.restore_failed}")
    print(f"Crashed:                  {stats.crashed}")
    if dry_run:
        print(f"Would Enable Issues:      {stats.needs_enable}")
    if stats.aborted:
        print(f"Aborted (Ctrl-C):         {stats.aborted}")
    print("----------------------------------------------------------------")

    return stats


def print_org_header(org: str, org_index: int, total_orgs: int) -> None:
    if total_orgs > 1:
        print(f"\n{'#' * 64}")
        print(f"# Organization {org_index}/{total_orgs}: {org}")
        print(f"{'#' * 64}")


def print_multi_org_summary(
    all_stats: list[OrgStats],
    delete_mode: bool,
    stale_days: int | None,
    check_veracode: bool,
    output_file: str,
    dry_run: bool = False,
) -> None:
    print(f"\n{'=' * 64}")
    print("RUN SUMMARY")
    print(f"{'=' * 64}")

    if delete_mode:
        cols = [
            ("Org", "<30", lambda s: s.org),
            ("Repos", ">6", lambda s: s.total_repos),
        ]
        if dry_run:
            cols += [
                ("WouldClose", ">10", lambda s: s.would_close),
                ("NeedEnable", ">10", lambda s: s.needs_enable),
            ]
        else:
            cols.append(("Closed", ">8", lambda s: s.deleted))
        cols += [
            ("Archived", ">9", lambda s: s.archived),
            ("No Issues", ">10", lambda s: s.skipped_no_issues),
            ("CloseFail", ">9", lambda s: s.close_failed),
            ("EnableFail", ">10", lambda s: s.enable_failed),
        ]
        if check_veracode:
            cols.append(("VCProf", ">7", lambda s: s.skipped_veracode_profile))
    else:
        cols = [
            ("Org", "<30", lambda s: s.org),
            ("Repos", ">6", lambda s: s.total_repos),
            ("Created", ">8", lambda s: s.created),
            ("Comment", ">8", lambda s: s.commented),
            ("Failed", ">8", lambda s: s.failed),
            ("Archived", ">9", lambda s: s.archived),
            ("Skipped", ">8", lambda s: s.skipped_existing),
            ("PermSkip", ">9", lambda s: s.skipped_perm),
        ]
        if dry_run:
            cols.append(("NeedEnable", ">10", lambda s: s.needs_enable))
        if check_veracode:
            cols.append(("VCProf", ">7", lambda s: s.skipped_veracode_profile))
        if stale_days:
            cols.append(("Recent", ">7", lambda s: s.skipped_recent))
            cols.append(("StaleFail", ">9", lambda s: s.stale_check_failed))
        cols.append(("IaC", ">5", lambda s: s.iac))
    cols += [
        ("QueryFail", ">9", lambda s: s.query_failed),
        ("NotFound", ">8", lambda s: s.not_found),
        ("RestoreFail", ">11", lambda s: s.restore_failed),
        ("Crashed", ">7", lambda s: s.crashed),
        ("OrgFail", ">7", lambda s: s.org_failed),
    ]

    header = " ".join(f"{name:{fmt}}" for name, fmt, _ in cols)
    sep = "-" * len(header)
    print(header)
    print(sep)
    for s in all_stats:
        print(" ".join(f"{getter(s):{fmt}}" for _, fmt, getter in cols))
    print(sep)

    totals = ["TOTAL"] + [
        sum(getter(s) for s in all_stats) for _, _, getter in cols[1:]
    ]
    print(" ".join(f"{val:{fmt}}" for (_, fmt, _), val in zip(cols, totals)))

    failed = sum(s.total_failed for s in all_stats)
    aborted = sum(s.aborted for s in all_stats)
    print(f"\nFailures (incl. restore/org): {failed}" + (f"   Aborted: {aborted}" if aborted else ""))
    print(f"CSV Output: {output_file}")
    print(f"{'=' * 64}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create or close Veracode baseline scan trigger issues across one or more GitHub orgs."
        )
    )

    org_group = parser.add_mutually_exclusive_group(required=True)
    org_group.add_argument("org", nargs="?", help="Single GitHub organization name")
    org_group.add_argument(
        "--org-file",
        metavar="FILE",
        help="Text file with one org name per line",
    )
    org_group.add_argument(
        "--org-repo-file",
        metavar="FILE",
        help="CSV file with 2 columns (org, repo) to trigger scans for specific org/repo combinations",
    )

    repo_filter_group = parser.add_mutually_exclusive_group()
    repo_filter_group.add_argument(
        "--repo-file",
        metavar="FILE",
        help="Text file with one repo name per line",
    )
    repo_filter_group.add_argument(
        "--repo-wildcard",
        metavar="PATTERN",
        help="Wildcard pattern to filter repos (case-insensitive)",
    )

    parser.add_argument("--delete", action="store_true", help="Close previously created trigger issues")
    parser.add_argument(
        "--rescan-existing",
        action="store_true",
        help=(
            "If an open trigger issue already exists, comment on it to "
            "re-trigger the scan instead of skipping the repo. A new issue is "
            "only created when none is open. Default: skip repos that already "
            "have an open trigger issue."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen. Makes no writes of any kind.",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        metavar="N",
        default=None,
        help=f"Only create issues for repos not scanned in last N days (suggested: {DEFAULT_STALE_DAYS})",
    )
    parser.add_argument(
        "--veracode-skip-existing",
        action="store_true",
        help=(
            "Query the Veracode platform and skip repos that already have an "
            "application profile (named '<org>/<repo>'). In create mode, skips "
            "issue creation for repos with a profile. In delete mode, skips "
            "closing issues for repos that have a profile (preserves them). "
            "Requires VERACODE_API_KEY_ID/VERACODE_API_KEY_SECRET env vars or "
            "~/.veracode/credentials."
        ),
    )
    parser.add_argument(
        "--veracode-region",
        choices=sorted(VERACODE_REGIONS.keys()),
        default=None,
        help=(
            "Veracode region for the API base URL. Default: inferred from the "
            "VERACODE_API_KEY_ID prefix, else commercial."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=(
            f"Parallel worker threads per org (default: {DEFAULT_WORKERS}, "
            f"max: {MAX_WORKERS}). All workers share one rate-limit throttle, "
            f"so higher values speed up reads without risking secondary limits. "
            f"Issue creation stays serial regardless (see --create-interval)."
        ),
    )
    parser.add_argument(
        "--create-interval",
        type=float,
        default=DEFAULT_CREATE_INTERVAL,
        metavar="SECONDS",
        help=(
            f"Minimum seconds between issue creations, global across all workers "
            f"and orgs (default: {DEFAULT_CREATE_INTERVAL:.0f}). GitHub's "
            f"issue-creation limit is undisclosed and fails silently."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        metavar="N",
        help="Pause after every N issues created (0 = off)",
    )
    parser.add_argument(
        "--batch-pause",
        type=float,
        default=DEFAULT_BATCH_PAUSE,
        metavar="SECONDS",
        help=(
            f"Seconds to pause between batches so the Veracode queue drains "
            f"(default: {DEFAULT_BATCH_PAUSE:.0f})"
        ),
    )
    parser.add_argument(
        "--max-inflight",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Do not create a new issue while N scans are queued or running in "
            "the org's central repo (see --central-repo; 0 = off). Stops Actions "
            "jobs aging out past the 6h job / GITHUB_TOKEN lifetime."
        ),
    )
    parser.add_argument(
        "--central-repo",
        metavar="NAME",
        default=_DEFAULT_CENTRAL_REPO,
        help=(
            "Repo in each org where the Workflow App runs scans; --max-inflight "
            f"counts its queued and in-progress Actions runs (default: {_DEFAULT_CENTRAL_REPO})"
        ),
    )
    parser.add_argument("--hostname", metavar="HOSTNAME", default=None, help="GitHub hostname for GHES/GHEC")
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help=(
            "Output CSV path (default: vcbaseline-<create|delete>[-dryrun]-"
            "<YYYYmmdd-HHMMSS>.csv). An existing file is never overwritten "
            "unless --overwrite is given."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow --output to replace an existing file",
    )
    parser.add_argument(
        "--resume",
        metavar="CSV",
        default=None,
        help=(
            "Continue a previous run: skip repos whose row in CSV has a terminal "
            "action (deleted, no_issues_found, created, skipped_*, ...) and "
            "append new rows to that CSV"
        ),
    )
    parser.add_argument(
        "--repo-limit",
        type=int,
        default=DEFAULT_REPO_LIST_LIMIT,
        help=f"Max repos per org (default: {DEFAULT_REPO_LIST_LIMIT})",
    )
    parser.add_argument(
        "--min-remaining",
        type=int,
        default=CORE_MIN_REMAINING,
        metavar="N",
        help=f"Pause when core or graphql remaining <= N (default: {CORE_MIN_REMAINING})",
    )
    parser.add_argument(
        "--rl-check-every",
        type=int,
        default=CORE_CHECK_EVERY,
        metavar="N",
        help=f"Query the rate limit every N gh calls (default: {CORE_CHECK_EVERY})",
    )
    parser.add_argument(
        "--close-comment",
        metavar="TEXT",
        default=None,
        help=(
            "Delete mode: post TEXT as a comment before closing each issue. Off by "
            "default; each comment is a content write (slow: ~140/hour budget)."
        ),
    )
    parser.add_argument(
        "--close-per-minute",
        type=int,
        default=DEFAULT_CLOSE_PER_MINUTE,
        metavar="N",
        help=(
            f"Delete mode: max issue closes per minute, global (default: "
            f"{DEFAULT_CLOSE_PER_MINUTE}). All mutations also keep a 1s gap."
        ),
    )
    parser.add_argument(
        "--issue-author",
        metavar="LOGIN",
        default=None,
        help="Delete mode: only close trigger issues opened by LOGIN",
    )
    parser.add_argument(
        "--match-body",
        action="store_true",
        help=f"Delete mode: only close trigger issues whose body is exactly '{ISSUE_BODY}'",
    )
    parser.add_argument(
        "--strict-orgs",
        action="store_true",
        help=(
            "Check access to every org before any work and abort if one fails "
            "(old behaviour). Default: inaccessible orgs are recorded and skipped."
        ),
    )
    return parser


def _warn_mode_flags(args: argparse.Namespace) -> None:
    """Flags that do nothing in the chosen mode are almost always a mistake
    (e.g. pacing flags passed to --delete), so say so instead of ignoring them."""
    if args.delete:
        used = [
            flag for flag, on in (
                ("--rescan-existing", args.rescan_existing),
                ("--create-interval", args.create_interval != DEFAULT_CREATE_INTERVAL),
                ("--batch-size", args.batch_size != 0),
                ("--batch-pause", args.batch_pause != DEFAULT_BATCH_PAUSE),
                ("--max-inflight", args.max_inflight != 0),
                ("--central-repo", args.central_repo != _DEFAULT_CENTRAL_REPO),
            ) if on
        ]
        if used:
            print(
                f"Warning: {', '.join(used)} ignored in delete mode "
                "(delete pacing: --close-per-minute).",
                file=sys.stderr,
            )
    else:
        used = [
            flag for flag, on in (
                ("--close-comment", args.close_comment is not None),
                ("--close-per-minute", args.close_per_minute != DEFAULT_CLOSE_PER_MINUTE),
                ("--issue-author", args.issue_author is not None),
                ("--match-body", args.match_body),
            ) if on
        ]
        if used:
            print(f"Warning: {', '.join(used)} only apply to --delete; ignored.", file=sys.stderr)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.stale_days is not None and args.stale_days < 0:
        sys.exit("Error: --stale-days must be >= 0")
    if args.workers < 1:
        sys.exit("Error: --workers must be >= 1")
    if args.workers > MAX_WORKERS:
        sys.exit(f"Error: --workers must be <= {MAX_WORKERS}")
    if args.create_interval < 0:
        sys.exit("Error: --create-interval must be >= 0")
    if args.batch_size < 0:
        sys.exit("Error: --batch-size must be >= 0")
    if args.max_inflight < 0:
        sys.exit("Error: --max-inflight must be >= 0")
    if args.min_remaining < 0:
        sys.exit("Error: --min-remaining must be >= 0")
    if args.rl_check_every < 1:
        sys.exit("Error: --rl-check-every must be >= 1")
    if args.close_per_minute < 0:
        sys.exit("Error: --close-per-minute must be >= 0")
    if not validate_repo_name(args.central_repo) or _has_glob(args.central_repo):
        sys.exit(f"Error: Invalid --central-repo: {args.central_repo!r}")
    if args.org_repo_file and (args.repo_file or args.repo_wildcard):
        sys.exit(
            "Error: --org-repo-file cannot be combined with --repo-file or --repo-wildcard"
        )
    if args.delete and args.stale_days is not None:
        print("Warning: --stale-days is ignored in delete mode.", file=sys.stderr)
    _warn_mode_flags(args)

    # Settle the audit CSV before any GitHub call: never clobber a previous run.
    fieldnames = DELETE_FIELDNAMES if args.delete else CREATE_FIELDNAMES
    resume_done: set[str] = set()
    if args.resume:
        if args.dry_run:
            sys.exit("Error: --resume cannot be combined with --dry-run")
        if args.output and os.path.abspath(args.output) != os.path.abspath(args.resume):
            sys.exit("Error: --resume appends to its own CSV; drop --output")
        try:
            resume_done = load_resume(args.resume, fieldnames)
        except ResumeError as exc:
            sys.exit(f"Error: {exc}")
        output_file, open_mode = args.resume, "a"
        print(f"Resuming '{output_file}': {len(resume_done)} repo(s) already done will be skipped.")
    else:
        output_file = args.output or default_output_name(args.delete, args.dry_run)
        if os.path.exists(output_file) and not args.overwrite:
            sys.exit(
                f"Error: '{output_file}' already exists. Use --resume to continue "
                "that run, --overwrite to replace it, or another --output."
            )
        open_mode = "w" if args.overwrite else "x"

    if shutil.which("gh") is None:
        sys.exit("Error: GitHub CLI (gh) is not installed.")

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(
            "Warning: running inside GitHub Actions. Issues created with GITHUB_TOKEN "
            "do NOT trigger workflow runs, so no scans will start. Use a PAT or a "
            "GitHub App token in GH_TOKEN.",
            file=sys.stderr,
        )

    gh_hostname = args.hostname
    if gh_hostname and "ghe.com" not in gh_hostname and not os.environ.get("GH_ENTERPRISE_TOKEN"):
        print(
            f"Warning: --hostname is '{gh_hostname}' but GH_ENTERPRISE_TOKEN is not set.",
            file=sys.stderr,
        )

    if gh_hostname:
        print(f"Targeting GitHub host: {gh_hostname}")

    throttle = Throttle(
        points_per_min=GH_POINTS_PER_MIN,
        content_per_min=GH_CONTENT_PER_MIN,
        content_per_hour=GH_CONTENT_PER_HOUR,
        search_per_min=GH_SEARCH_PER_MIN,
        close_per_min=args.close_per_minute,
    )

    ctx = GhContext(
        env=build_gh_env(gh_hostname),
        state=RateLimitState(),
        throttle=throttle,
        min_remaining=args.min_remaining,
        check_every=args.rl_check_every,
    )

    gate = CreateGate(
        interval=args.create_interval,
        batch_size=args.batch_size,
        batch_pause=args.batch_pause,
    )
    inflight = InflightTracker(cap=args.max_inflight, ctx=ctx, central_repo=args.central_repo)
    delete_opts = DeleteOptions(
        close_comment=args.close_comment,
        issue_author=args.issue_author,
        match_body=args.match_body,
    )

    repo_filter_names: list[str] | None = None
    repo_filter_pattern: str | None = None

    if args.repo_file:
        try:
            repo_filter_names = load_repos_from_file(args.repo_file)
        except RepoFileError as exc:
            sys.exit(f"Error: {exc}")
        print(f"Loaded {len(repo_filter_names)} repo name(s) from '{args.repo_file}'.")

    if args.repo_wildcard:
        repo_filter_pattern = args.repo_wildcard
        print(f"Using repo wildcard filter: '{repo_filter_pattern}'")

    org_repo_pairs: list[tuple[str, str]] | None = None
    if args.org_repo_file:
        try:
            org_repo_pairs = load_org_repos_from_file(args.org_repo_file)
        except OrgRepoFileError as exc:
            sys.exit(f"Error: {exc}")
        print(f"Loaded {len(org_repo_pairs)} org-repo pair(s) from '{args.org_repo_file}'.")
        orgs = []
        seen_orgs: set[str] = set()
        for org, _ in org_repo_pairs:
            if org.lower() not in seen_orgs:
                seen_orgs.add(org.lower())
                orgs.append(org)
        print(f"Targeting {len(orgs)} org(s) from '{args.org_repo_file}'.")
    elif args.org_file:
        try:
            orgs = load_orgs_from_file(args.org_file)
        except OrgFileError as exc:
            sys.exit(f"Error: {exc}")
        print(f"Loaded {len(orgs)} org(s) from '{args.org_file}'.")
    else:
        if not validate_org_name(args.org):
            sys.exit(f"Error: Invalid org name: {args.org!r}")
        orgs = [args.org]

    total_orgs = len(orgs)

    veracode_profiles: set[str] | None = None
    use_veracode = args.veracode_skip_existing
    if use_veracode:
        region, region_warning = resolve_veracode_region(
            args.veracode_region, os.environ.get("VERACODE_API_KEY_ID"),
        )
        if region_warning:
            print(f"Warning: {region_warning}", file=sys.stderr)
        args.veracode_region = region
        try:
            veracode_profiles = fetch_veracode_profiles(region)
        except VeracodeError as exc:
            sys.exit(f"Error: Veracode profile fetch failed: {exc}")

    if args.strict_orgs:
        print("\nChecking access to all organizations (--strict-orgs)...")
        inaccessible_orgs = [org for org in orgs if not _check_and_report(org, ctx)]
        if inaccessible_orgs:
            sys.exit(
                f"\nError: Cannot access org(s): {', '.join(inaccessible_orgs)}. "
                "Fix access or remove them and retry."
            )
    print()

    if args.dry_run:
        print("================================================================")
        print("DRY RUN: no issues will be created, closed, or enabled")
        print("================================================================\n")

    print(f"Using {args.workers} parallel worker(s) per org.")
    print(
        f"Rate-limit throttle: <= {GH_POINTS_PER_MIN} points/min, "
        f"<= {GH_CONTENT_PER_MIN} content writes/min, "
        f"<= {GH_CONTENT_PER_HOUR} content writes/hour, "
        f"<= {GH_SEARCH_PER_MIN} searches/min (shared across all workers)."
    )
    if not args.delete:
        print(
            f"Creation pacing: 1 issue every {args.create_interval:.0f}s"
            + (
                f", pause {args.batch_pause:.0f}s every {args.batch_size}"
                if args.batch_size else ""
            )
            + (f", max {args.max_inflight} scans in flight" if args.max_inflight else "")
            + "."
        )

    if args.delete:
        print("================================================================")
        print(f"DELETE MODE: Closing issues with title '{ISSUE_TITLE}'")
        print("================================================================\n")
        fieldnames = DELETE_FIELDNAMES
        if use_veracode:
            print("================================================================")
            print(f"VERACODE SKIP MODE: Preserving issues for repos with existing profiles ({args.veracode_region})")
            print("================================================================\n")
    else:
        fieldnames = CREATE_FIELDNAMES
        if args.rescan_existing:
            print("================================================================")
            print(f"RESCAN MODE: Commenting on existing open '{ISSUE_TITLE}' issues")
            print("================================================================\n")
        if args.stale_days:
            print("================================================================")
            print(f"STALE CHECK MODE: Creating issues for repos not scanned in {args.stale_days} days")
            print("================================================================\n")
        if use_veracode:
            print("================================================================")
            print(f"VERACODE SKIP MODE: Skipping repos with existing profiles ({args.veracode_region})")
            print("================================================================\n")

    all_stats: list[OrgStats] = []
    csv_lock = threading.Lock()

    def run_org(org: str, csv_writer: csv.DictWriter, csv_file) -> OrgStats:
        targets: list[str] | None = None
        if org_repo_pairs:
            targets = [repo for o, repo in org_repo_pairs if o.lower() == org.lower()]
        elif repo_filter_names:
            targets = repo_filter_names

        missing: list[tuple[str, str]] = []
        org_action: str | None = None
        if targets is not None:
            repos, missing = resolve_targets(org, targets, args.repo_limit, ctx, resume_done)
            if repos is not None and not repos and missing and not check_org_access(org, ctx):
                # Every target 404'd because the org itself is out of reach.
                repos, missing, org_action = None, [], "org_inaccessible"
        else:
            repos = fetch_repos(org, args.repo_limit, ctx)
        if repos is None:
            # Only now spend a call to tell "no access" from "list failed".
            action = org_action or ("org_list_failed" if check_org_access(org, ctx) else "org_inaccessible")
            print(f"  Error: {action} for '{org}'. Continuing with the next org.", file=sys.stderr)
            _write_row(csv_writer, csv_file, csv_lock, _org_row(org, action))
            return OrgStats(org=org, org_failed=1)

        for full, action in missing:
            print(f"  Error: {full}: {action}.", file=sys.stderr)
            _write_row(csv_writer, csv_file, csv_lock, {"org": org, "repo": full, "action": action})

        def with_missing(stats: OrgStats) -> OrgStats:
            stats.not_found += sum(1 for _, a in missing if a == "not_found")
            stats.query_failed += sum(1 for _, a in missing if a != "not_found")
            if missing:
                print(f"Targets not found or not readable in '{org}': {len(missing)}")
            return stats

        if targets is None and not repos:
            print(f"  No repos returned for '{org}' - skipping.", file=sys.stderr)
            return OrgStats(org=org)

        if targets is not None:
            print(f"Resolved {len(repos)} target repo(s) for '{org}'")
        elif repo_filter_pattern:
            original_count = len(repos)
            repos = filter_repos_by_wildcard(repos, repo_filter_pattern)
            print(f"Filtered to {len(repos)}/{original_count} repos matching '{repo_filter_pattern}'")

        if resume_done:
            before = len(repos)
            repos = [r for r in repos if r["nameWithOwner"].lower() not in resume_done]
            if before != len(repos):
                print(f"Resume: skipping {before - len(repos)} repo(s) already done in '{org}'")

        if not repos:
            print(f"  No repos match filter for '{org}' - skipping.", file=sys.stderr)
            return with_missing(OrgStats(org=org))

        if args.delete:
            return with_missing(run_delete_mode(
                org, repos, csv_writer, csv_file, csv_lock, ctx,
                veracode_profiles, args.workers, args.dry_run, delete_opts,
            ))
        return with_missing(run_create_mode(
            org, repos, csv_writer, csv_file, csv_lock, ctx,
            args.stale_days, veracode_profiles, args.workers,
            gate, inflight, args.dry_run, args.rescan_existing,
        ))

    interrupted = False
    try:
        with open(output_file, open_mode, newline="", encoding="utf-8") as csv_file:
            csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            if open_mode != "a":
                csv_writer.writeheader()
                csv_file.flush()

            for org_index, org in enumerate(orgs, start=1):
                print_org_header(org, org_index, total_orgs)
                try:
                    stats = run_org(org, csv_writer, csv_file)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001 - one org must not kill the run
                    print(f"  Error: org '{org}' crashed: {exc!r}. Continuing.", file=sys.stderr)
                    _write_row(csv_writer, csv_file, csv_lock, _org_row(org, "org_crashed"))
                    stats = OrgStats(org=org, org_failed=1)
                all_stats.append(stats)
                if STOP.is_set():
                    interrupted = True
                    print(f"\nInterrupted. Remaining {total_orgs - org_index} org(s) not started.", file=sys.stderr)
                    break

    except KeyboardInterrupt:
        STOP.set()
        interrupted = True
        print("\nInterrupted. The CSV has a row for every repo that was touched.", file=sys.stderr)

    if all_stats:
        print_multi_org_summary(
            all_stats, args.delete, args.stale_days, use_veracode, output_file, args.dry_run,
        )
    else:
        print(f"\nCSV Output: {output_file}")

    code = exit_code(all_stats, interrupted)
    if code:
        sys.exit(code)


EXIT_OK = 0
EXIT_FAILURES = 2
EXIT_INTERRUPTED = 130


def exit_code(all_stats: list[OrgStats], interrupted: bool) -> int:
    if interrupted:
        return EXIT_INTERRUPTED
    if any(s.total_failed for s in all_stats):
        return EXIT_FAILURES
    return EXIT_OK


if __name__ == "__main__":
    main()
