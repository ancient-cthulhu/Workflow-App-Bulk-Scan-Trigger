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
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

ISSUE_TITLE = "Veracode Baseline Scans"
ISSUE_BODY = "Veracode All Scans"
IAC_LANGUAGES = frozenset({"HCL", "Bicep"})
SUBPROCESS_TIMEOUT = 60
REPO_LIST_TIMEOUT = 300
DEFAULT_OUTPUT_FILE = "vcbaseline.csv"
DEFAULT_STALE_DAYS = 30
SECONDARY_RATE_LIMIT_SLEEP = 60
SECONDARY_BACKOFF_CAP = 600
MAX_RATE_LIMIT_RETRIES = 3
PARTIAL_STDERR_LIMIT = 500
DEFAULT_WORKERS = 5
MAX_WORKERS = 50

VERACODE_REGIONS = {
    "commercial": "https://api.veracode.com/appsec/v1",
    "eu": "https://api.veracode.eu/appsec/v1",
    "federal": "https://api.veracode.us/appsec/v1",
}
VERACODE_PAGE_SIZE = 500
VERACODE_TIMEOUT = 60
VERACODE_MAX_RETRIES = 3
VERACODE_RETRY_SLEEP = 5

_VALID_ORG_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,37}[a-zA-Z0-9])?$")
_VALID_REPO_RE = re.compile(r"^[a-zA-Z0-9.*-]+$")
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
GH_CONTENT_PER_MIN = _env_int("GH_CONTENT_PER_MIN", 60)
GH_CONTENT_PER_HOUR = _env_int("GH_CONTENT_PER_HOUR", 450)


@dataclass(frozen=True)
class CallCost:
    points: int
    content: bool


COST_READ = CallCost(points=1, content=False)
COST_WRITE = CallCost(points=5, content=False)
COST_CONTENT = CallCost(points=5, content=True)


class Throttle:
    """Shared across all worker threads. Paces every gh call against GitHub's
    secondary rate limits: combined points/minute, content/minute, content/hour,
    plus a global back-off window so a single secondary-limit hit stalls every
    thread instead of all of them retrying at once."""

    def __init__(
        self,
        points_per_min: int,
        content_per_min: int,
        content_per_hour: int,
    ) -> None:
        self._lock = threading.Lock()
        self._points: deque[tuple[float, int]] = deque()
        self._content_min: deque[float] = deque()
        self._content_hour: deque[float] = deque()
        self._pause_until = 0.0
        self.points_per_min = points_per_min
        self.content_per_min = content_per_min
        self.content_per_hour = content_per_hour

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

                    if cost.content:
                        self._prune(self._content_min, now, 60.0)
                        self._prune(self._content_hour, now, 3600.0)
                        if len(self._content_min) + 1 > self.content_per_min and self._content_min:
                            wait = max(wait, self._content_min[0] + 60.0 - now)
                        if len(self._content_hour) + 1 > self.content_per_hour and self._content_hour:
                            wait = max(wait, self._content_hour[0] + 3600.0 - now)

                    if wait <= 0:
                        self._points.append((now, cost.points))
                        if cost.content:
                            self._content_min.append(now)
                            self._content_hour.append(now)
                        return

            time.sleep(min(wait, 5.0) + random.uniform(0.0, 0.15))


@dataclass
class RateLimitState:
    call_count: int = 0
    remaining: int = 9999
    reset_epoch: int = 0
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
    skipped_no_issues: int = 0
    deleted: int = 0
    failed: int = 0


@dataclass
class RepoResult:
    repo_name: str
    row: dict
    logs: list[str] = field(default_factory=list)
    stat_deltas: dict[str, int] = field(default_factory=dict)


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


def load_orgs_from_file(file_path: str) -> list[str]:
    orgs = _load_lines(file_path, OrgFileError, "org")
    invalid = [o for o in orgs if not validate_org_name(o)]
    if invalid:
        raise OrgFileError(f"Invalid org name(s) in '{file_path}': {', '.join(invalid)}")
    return orgs


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
            if row[0].startswith("#"):
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
    target_set = {name.lower() for name in target_names}
    return [
        r for r in repos
        if r["nameWithOwner"].split("/", 1)[-1].lower() in target_set
    ]


def filter_repos_by_wildcard(repos: list[dict], pattern: str) -> list[dict]:
    pattern_lower = pattern.lower()
    return [
        r for r in repos
        if fnmatch.fnmatch(r["nameWithOwner"].split("/", 1)[-1].lower(), pattern_lower)
    ]


def query_rate_limit(ctx: GhContext) -> bool:
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit", "--jq", ".resources.core.remaining, .resources.core.reset"],
            capture_output=True,
            text=True,
            check=True,
            env=ctx.env,
            timeout=SUBPROCESS_TIMEOUT,
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return False
        with ctx.state.lock:
            ctx.state.remaining = int(lines[0])
            ctx.state.reset_epoch = int(lines[1])
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

    if remaining <= ctx.min_remaining:
        sleep_for = reset_epoch - int(time.time()) + 1
        if sleep_for > 0:
            reset_human = datetime.fromtimestamp(
                reset_epoch, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(
                f"Core rate limit low (remaining={remaining}). "
                f"Sleeping ~{sleep_for}s until reset ({reset_human}).",
                file=sys.stderr,
            )
            time.sleep(sleep_for)


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
            env=env,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout, result.stderr
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


def gh_call(
    args: list[str],
    ctx: GhContext,
    cost: CallCost = COST_READ,
    timeout: int = SUBPROCESS_TIMEOUT,
) -> tuple[bool, str, str]:
    delay = SECONDARY_RATE_LIMIT_SLEEP

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        maybe_pause_for_rate_limit(ctx)
        ctx.throttle.acquire(cost)
        success, stdout, stderr = _run_subprocess(args, ctx.env, timeout=timeout)

        if success:
            return True, stdout, stderr

        combined = (stderr + stdout).lower()

        if any(pat in combined for pat in _AUTH_FAILURE_PATTERNS):
            print(stderr, file=sys.stderr)
            return False, stdout, stderr

        is_rate_limited = any(pat in combined for pat in _RATE_LIMIT_PATTERNS)
        if not is_rate_limited:
            print(stderr, file=sys.stderr)
            return False, stdout, stderr

        if attempt == MAX_RATE_LIMIT_RETRIES:
            print(
                f"Rate limit retries exhausted ({MAX_RATE_LIMIT_RETRIES}). Giving up.",
                file=sys.stderr,
            )
            print(stderr, file=sys.stderr)
            return False, stdout, stderr

        retry_after = _parse_retry_after(combined)
        is_secondary = any(pat in combined for pat in _SECONDARY_RATE_LIMIT_PATTERNS)

        if is_secondary or retry_after is not None:
            block_for = retry_after if retry_after is not None else delay
            print(
                f"Secondary rate limit (attempt {attempt + 1}/{MAX_RATE_LIMIT_RETRIES}). "
                f"Pausing all workers {block_for}s...",
                file=sys.stderr,
            )
            ctx.throttle.register_block(block_for)
            time.sleep(block_for)
            delay = min(delay * 2, SECONDARY_BACKOFF_CAP)
        elif query_rate_limit(ctx):
            with ctx.state.lock:
                reset_epoch = ctx.state.reset_epoch
            sleep_for = max(reset_epoch - int(time.time()) + 1, 30)
            print(f"Core rate limit. Pausing all workers {sleep_for}s until reset...", file=sys.stderr)
            ctx.throttle.register_block(sleep_for)
            time.sleep(sleep_for)
        else:
            print("Rate limited (could not query reset). Pausing all workers 30s...", file=sys.stderr)
            ctx.throttle.register_block(30)
            time.sleep(30)

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
                    print(
                        f"  Veracode HTTP {response.status_code} on page {page} "
                        f"(attempt {attempt}/{VERACODE_MAX_RETRIES}). Retrying...",
                        file=sys.stderr,
                    )
                    time.sleep(VERACODE_RETRY_SLEEP * attempt)
                    continue
                break
            except requests.RequestException as exc:
                last_exc = exc
                print(
                    f"  Veracode request error on page {page} "
                    f"(attempt {attempt}/{VERACODE_MAX_RETRIES}): {exc}",
                    file=sys.stderr,
                )
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

    print(f"  Loaded {len(profile_names)} unique Veracode profile name(s).")
    return profile_names


def build_veracode_profile_name(repo_full_name: str) -> str:
    return repo_full_name.strip().lower()


def check_org_access(org: str, ctx: GhContext) -> bool:
    success, _, _ = gh_call(["gh", "api", f"orgs/{org}", "--silent"], ctx, cost=COST_READ)
    return success


def _check_and_report(org: str, ctx: GhContext) -> bool:
    print(f"  Checking {org}...", end=" ", flush=True)
    ok = check_org_access(org, ctx)
    print("OK" if ok else "FAILED")
    return ok


def fetch_repos(org: str, limit: int, ctx: GhContext) -> list[dict]:
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
        return []
    result = parse_json_safe(stdout, f"fetch_repos org={org}")
    return result if isinstance(result, list) else []


def find_open_issues(repo: str, title: str, ctx: GhContext) -> list[int] | None:
    success, stdout, _ = gh_call(
        [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--limit", "1000",
            "--json", "number,title",
        ],
        ctx,
        cost=COST_READ,
    )
    if not success:
        return None
    issues = parse_json_safe(stdout, f"find_open_issues repo={repo}")
    if not isinstance(issues, list):
        return None
    return [issue["number"] for issue in issues if issue.get("title") == title]


def get_last_veracode_check(repo: str, branch: str, ctx: GhContext) -> datetime | None:
    if not branch:
        return None

    success, stdout, _ = gh_call(
        [
            "gh", "api", f"repos/{repo}/commits/{branch}/check-runs",
            "-X", "GET",
            "-f", "per_page=100",
        ],
        ctx,
        cost=COST_READ,
    )
    if not success:
        return None

    parsed = parse_json_safe(stdout, f"get_last_veracode_check repo={repo}")
    if not isinstance(parsed, dict):
        return None

    check_runs = parsed.get("check_runs") or []
    completed_times: list[datetime] = []
    for run in check_runs:
        name = run.get("name") or ""
        completed_at = run.get("completed_at")
        if not name.startswith("Veracode") or not completed_at:
            continue
        try:
            completed_times.append(
                datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            )
        except (ValueError, AttributeError):
            continue

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


def _restore_issues_disabled(name: str, ctx: GhContext, log: RepoLogger) -> bool:
    log.log("Restoring state: Disabling issues...")
    success, _, _ = gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], ctx, cost=COST_WRITE)
    if not success:
        log.warn(f"Failed to restore issues-disabled state on {name}")
    return success


def _process_delete_repo(
    org: str,
    repo: dict,
    ctx: GhContext,
    veracode_profiles: set[str] | None,
) -> RepoResult:
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

    was_disabled = False
    if not issues_enabled:
        log.log("Issues are disabled. Temporarily enabling to close existing issue...")
        success, _, _ = gh_call(["gh", "repo", "edit", name, "--enable-issues"], ctx, cost=COST_WRITE)
        if not success:
            log.log("Could not enable issues. Skipping.")
            deltas["failed"] = 1
            return RepoResult(
                name,
                _row_delete(org, repo, 0, "failed_enable_issues", vc_exists_str),
                log.lines, deltas,
            )
        was_disabled = True

    issue_numbers = find_open_issues(name, ISSUE_TITLE, ctx)
    if issue_numbers is None:
        log.log("Could not query issues (API error). Skipping.")
        deltas["failed"] = 1
        if was_disabled:
            _restore_issues_disabled(name, ctx, log)
        return RepoResult(
            name,
            _row_delete(org, repo, 0, "failed_query_issues", vc_exists_str),
            log.lines, deltas,
        )

    if not issue_numbers:
        log.log("No matching issues found.")
        deltas["skipped_no_issues"] = 1
        if was_disabled:
            _restore_issues_disabled(name, ctx, log)
        return RepoResult(
            name,
            _row_delete(org, repo, 0, "no_issues_found", vc_exists_str),
            log.lines, deltas,
        )

    issues_deleted = issues_failed = 0
    for issue_num in issue_numbers:
        log.log(f"Closing issue #{issue_num}...")
        success, _, _ = gh_call(
            [
                "gh", "issue", "close", str(issue_num), "--repo", name,
                "--comment", "Closed by cleanup script",
            ],
            ctx,
            cost=COST_CONTENT,
        )
        if success:
            issues_deleted += 1
            deltas["deleted"] = deltas.get("deleted", 0) + 1
        else:
            log.log(f"Failed to close issue #{issue_num}")
            issues_failed += 1
            deltas["failed"] = deltas.get("failed", 0) + 1

    action = "partial_delete" if issues_failed > 0 else "deleted"

    if was_disabled:
        if not _restore_issues_disabled(name, ctx, log):
            action = f"{action}_restore_failed"

    return RepoResult(
        name,
        _row_delete(org, repo, issues_deleted, action, vc_exists_str),
        log.lines, deltas,
    )


def _process_create_repo(
    org: str,
    repo: dict,
    ctx: GhContext,
    stale_days: int | None,
    veracode_profiles: set[str] | None,
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
        last_check = get_last_veracode_check(name, default_branch, ctx)
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
            log.log("No Veracode checks found. Proceeding.")

    was_disabled = False
    if not issues_enabled:
        log.log("Issues are disabled. Temporarily enabling...")
        success, _, _ = gh_call(["gh", "repo", "edit", name, "--enable-issues"], ctx, cost=COST_WRITE)
        if not success:
            log.log("Could not enable issues. Skipping issue creation.")
            deltas["skipped_perm"] = 1
            return RepoResult(
                name,
                _row_create(
                    org, repo, "skipped_cant_enable_issues",
                    vc_exists_str, last_check_date, days_since_check,
                ),
                log.lines, deltas,
            )
        was_disabled = True

    existing_issues = find_open_issues(name, ISSUE_TITLE, ctx)

    if existing_issues is None:
        log.log("Could not query existing issues (API error). Skipping to avoid duplicates.")
        deltas["failed"] = 1
        if was_disabled:
            _restore_issues_disabled(name, ctx, log)
        return RepoResult(
            name,
            _row_create(
                org, repo, "failed_query_issues",
                vc_exists_str, last_check_date, days_since_check,
            ),
            log.lines, deltas,
        )

    if existing_issues:
        log.log("Open issue with same title already exists. Skipping.")
        deltas["skipped_existing"] = 1
        if was_disabled:
            _restore_issues_disabled(name, ctx, log)
        return RepoResult(
            name,
            _row_create(
                org, repo, "skipped_existing_issue",
                vc_exists_str, last_check_date, days_since_check,
            ),
            log.lines, deltas,
        )

    log.log("Creating issue...")
    success, _, _ = gh_call(
        [
            "gh", "issue", "create", "--repo", name,
            "--title", ISSUE_TITLE, "--body", ISSUE_BODY,
        ],
        ctx,
        cost=COST_CONTENT,
    )

    if success:
        deltas["created"] = 1
        action = "created"
    else:
        log.log("Failed to create issue.")
        deltas["failed"] = 1
        action = "failed_create"

    if was_disabled:
        if not _restore_issues_disabled(name, ctx, log):
            action = f"{action}_restore_failed"

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
    flush per-repo logs atomically as each future completes."""
    stats = OrgStats(org=org)
    total = len(repos)
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_repo = {executor.submit(worker_fn, repo): repo for repo in repos}

        for future in as_completed(future_to_repo):
            repo = future_to_repo[future]
            completed += 1
            repo_name = repo["nameWithOwner"]

            try:
                result: RepoResult = future.result()
            except Exception as exc:
                print(
                    f"\n[{completed}/{total}] {repo_name}\n"
                    f"  WORKER CRASHED: {exc}",
                    file=sys.stderr,
                )
                stats.failed += 1
                continue

            block = [f"\n[{completed}/{total}] {result.repo_name}"]
            block.extend(f"  {line}" for line in result.logs)
            print("\n".join(block))

            _apply_deltas(stats, result.stat_deltas)

            with csv_lock:
                csv_writer.writerow(result.row)
                csv_file.flush()

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
) -> OrgStats:
    check_veracode = veracode_profiles is not None

    def worker(repo: dict) -> RepoResult:
        return _process_delete_repo(org, repo, ctx, veracode_profiles)

    stats = _drive_workers(org, repos, csv_writer, csv_file, csv_lock, workers, worker)

    print(f"\nFinished closing issues for org: {org}\n")
    print(f"Delete Stats for Organization: {org}")
    print("----------------------------------------------------------------")
    print(f"Total Repositories:            {stats.total_repos}")
    print(f"Archived Repositories:         {stats.archived}")
    if check_veracode:
        print(f"Skipped VC Profile Hits:       {stats.skipped_veracode_profile}")
    print(f"Repositories with No Issues:   {stats.skipped_no_issues}")
    print(f"Issues Closed:                 {stats.deleted}")
    print(f"Failed Closes:                 {stats.failed}")
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
) -> OrgStats:
    check_veracode = veracode_profiles is not None

    def worker(repo: dict) -> RepoResult:
        return _process_create_repo(org, repo, ctx, stale_days, veracode_profiles)

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
    print(f"Failed Creates:           {stats.failed}")
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
) -> None:
    print(f"\n{'=' * 64}")
    print("MULTI-ORG SUMMARY")
    print(f"{'=' * 64}")

    if delete_mode:
        cols = [
            ("Org", "<30", lambda s: s.org),
            ("Repos", ">6", lambda s: s.total_repos),
            ("Closed", ">8", lambda s: s.deleted),
            ("Failed", ">8", lambda s: s.failed),
            ("Archived", ">9", lambda s: s.archived),
            ("No Issues", ">10", lambda s: s.skipped_no_issues),
        ]
        if check_veracode:
            cols.append(("VCProf", ">7", lambda s: s.skipped_veracode_profile))
    else:
        cols = [
            ("Org", "<30", lambda s: s.org),
            ("Repos", ">6", lambda s: s.total_repos),
            ("Created", ">8", lambda s: s.created),
            ("Failed", ">8", lambda s: s.failed),
            ("Archived", ">9", lambda s: s.archived),
            ("Skipped", ">8", lambda s: s.skipped_existing),
            ("PermSkip", ">9", lambda s: s.skipped_perm),
        ]
        if check_veracode:
            cols.append(("VCProf", ">7", lambda s: s.skipped_veracode_profile))
        if stale_days:
            cols.append(("Recent", ">7", lambda s: s.skipped_recent))
        cols.append(("IaC", ">5", lambda s: s.iac))

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

    print(f"\nCSV Output: {output_file}")
    print(f"{'=' * 64}")


def main() -> None:
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
        default="commercial",
        help="Veracode region for the API base URL (default: commercial)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=(
            f"Parallel worker threads per org (default: {DEFAULT_WORKERS}, "
            f"max: {MAX_WORKERS}). All workers share one rate-limit throttle, "
            f"so higher values speed up reads without risking secondary limits."
        ),
    )
    parser.add_argument("--hostname", metavar="HOSTNAME", default=None, help="GitHub hostname for GHES/GHEC")
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=DEFAULT_OUTPUT_FILE,
        help=f"Output CSV path (default: {DEFAULT_OUTPUT_FILE})",
    )
    parser.add_argument(
        "--repo-limit",
        type=int,
        default=DEFAULT_REPO_LIST_LIMIT,
        help=f"Max repos per org (default: {DEFAULT_REPO_LIST_LIMIT})",
    )

    args = parser.parse_args()

    if args.stale_days is not None and args.stale_days < 0:
        sys.exit("Error: --stale-days must be >= 0")
    if args.workers < 1:
        sys.exit("Error: --workers must be >= 1")
    if args.workers > MAX_WORKERS:
        sys.exit(f"Error: --workers must be <= {MAX_WORKERS}")
    if args.delete and args.stale_days is not None:
        print("Warning: --stale-days is ignored in delete mode.", file=sys.stderr)

    if shutil.which("gh") is None:
        sys.exit("Error: GitHub CLI (gh) is not installed.")

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
    )

    ctx = GhContext(
        env=build_gh_env(gh_hostname),
        state=RateLimitState(),
        throttle=throttle,
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
        orgs = list(dict.fromkeys(org for org, _ in org_repo_pairs))
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
        try:
            veracode_profiles = fetch_veracode_profiles(args.veracode_region)
        except VeracodeError as exc:
            sys.exit(f"Error: Veracode profile fetch failed: {exc}")

    print("\nChecking access to all organizations...")
    inaccessible_orgs = [org for org in orgs if not _check_and_report(org, ctx)]
    if inaccessible_orgs:
        sys.exit(
            f"\nError: Cannot access org(s): {', '.join(inaccessible_orgs)}. "
            "Fix access or remove them and retry."
        )
    print()

    print(f"Using {args.workers} parallel worker(s) per org.")
    print(
        f"Rate-limit throttle: <= {GH_POINTS_PER_MIN} points/min, "
        f"<= {GH_CONTENT_PER_MIN} content writes/min, "
        f"<= {GH_CONTENT_PER_HOUR} content writes/hour (shared across all workers)."
    )

    if args.delete:
        print("================================================================")
        print(f"DELETE MODE: Removing issues with title '{ISSUE_TITLE}'")
        print("================================================================\n")
        fieldnames = DELETE_FIELDNAMES
        if use_veracode:
            print("================================================================")
            print(f"VERACODE SKIP MODE: Preserving issues for repos with existing profiles ({args.veracode_region})")
            print("================================================================\n")
    else:
        fieldnames = CREATE_FIELDNAMES
        if args.stale_days:
            print("================================================================")
            print(f"STALE CHECK MODE: Creating issues for repos not scanned in {args.stale_days} days")
            print("================================================================\n")
        if use_veracode:
            print("================================================================")
            print(f"VERACODE SKIP MODE: Skipping repos with existing profiles ({args.veracode_region})")
            print("================================================================\n")

    output_file = args.output
    all_stats: list[OrgStats] = []
    csv_lock = threading.Lock()

    if os.path.exists(output_file):
        print(f"Warning: '{output_file}' already exists and will be overwritten.", file=sys.stderr)

    try:
        with open(output_file, "w", newline="", encoding="utf-8") as csv_file:
            csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            csv_writer.writeheader()
            csv_file.flush()

            for org_index, org in enumerate(orgs, start=1):
                print_org_header(org, org_index, total_orgs)

                repos = fetch_repos(org, args.repo_limit, ctx)
                if not repos:
                    print(f"  No repos returned for '{org}' - skipping.", file=sys.stderr)
                    all_stats.append(OrgStats(org=org))
                    continue

                original_count = len(repos)
                if org_repo_pairs:
                    target_repos = [repo for o, repo in org_repo_pairs if o == org]
                    repos = filter_repos_by_names(repos, target_repos)
                    print(f"Filtered to {len(repos)}/{original_count} repos from --org-repo-file for '{org}'")
                elif repo_filter_names:
                    repos = filter_repos_by_names(repos, repo_filter_names)
                    print(f"Filtered to {len(repos)}/{original_count} repos matching --repo-file")
                elif repo_filter_pattern:
                    repos = filter_repos_by_wildcard(repos, repo_filter_pattern)
                    print(f"Filtered to {len(repos)}/{original_count} repos matching '{repo_filter_pattern}'")

                if not repos:
                    print(f"  No repos match filter for '{org}' - skipping.", file=sys.stderr)
                    all_stats.append(OrgStats(org=org))
                    continue

                if args.delete:
                    stats = run_delete_mode(
                        org, repos, csv_writer, csv_file, csv_lock, ctx,
                        veracode_profiles, args.workers,
                    )
                else:
                    stats = run_create_mode(
                        org, repos, csv_writer, csv_file, csv_lock, ctx,
                        args.stale_days, veracode_profiles, args.workers,
                    )

                all_stats.append(stats)

    except KeyboardInterrupt:
        print("\nInterrupted. Partial CSV output may exist.", file=sys.stderr)
        sys.exit(130)

    if total_orgs > 1 and all_stats:
        print_multi_org_summary(
            all_stats, args.delete, args.stale_days, use_veracode, output_file,
        )

    print(f"\nCSV Output: {output_file}")


if __name__ == "__main__":
    main()
