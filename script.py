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
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ISSUE_TITLE = "Veracode Baseline Scans"
ISSUE_BODY = "Veracode All Scans"
IAC_LANGUAGES = frozenset({"HCL", "Bicep"})
SUBPROCESS_TIMEOUT = 60
DEFAULT_OUTPUT_FILE = "vcbaseline.csv"
DEFAULT_STALE_DAYS = 30
WRITE_THROTTLE_SECONDS = 1.0
SECONDARY_RATE_LIMIT_SLEEP = 60
SECONDARY_BACKOFF_CAP = 600
MAX_RATE_LIMIT_RETRIES = 3
PARTIAL_STDERR_LIMIT = 500

_VALID_ORG_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,37}[a-zA-Z0-9])?$")
_VALID_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

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
    """Raised for unrecoverable org-file loading failures."""


class RepoFileError(Exception):
    """Raised for unrecoverable repo-file loading failures."""


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

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


DEFAULT_RL_MIN_REMAINING = _env_int("GH_RL_MIN_REMAINING", 100)
DEFAULT_RL_CHECK_EVERY   = _env_int("GH_RL_CHECK_EVERY", 50)
DEFAULT_REPO_LIST_LIMIT  = _env_int("REPO_LIST_LIMIT", 1000)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RateLimitState:
    call_count: int = 0
    remaining: int = 9999
    reset_epoch: int = 0


@dataclass
class GhContext:
    env: dict[str, str]
    state: RateLimitState
    min_remaining: int
    check_every: int


@dataclass
class OrgStats:
    org: str
    total_repos: int = 0
    archived: int = 0
    iac: int = 0
    skipped_perm: int = 0
    skipped_existing: int = 0
    skipped_recent: int = 0
    created: int = 0
    skipped_no_issues: int = 0
    deleted: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

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
    """Shared loader for org/repo files."""
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


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def query_rate_limit(ctx: GhContext) -> bool:
    """Query GitHub core REST rate limit. Updates ctx.state."""
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit",
             "--jq", ".resources.core.remaining, .resources.core.reset"],
            capture_output=True, text=True, check=True, env=ctx.env,
            timeout=SUBPROCESS_TIMEOUT,
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return False
        ctx.state.remaining = int(lines[0])
        ctx.state.reset_epoch = int(lines[1])
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return False


def maybe_pause_for_rate_limit(ctx: GhContext) -> None:
    ctx.state.call_count += 1
    if ctx.state.call_count != 1 and ctx.state.call_count % ctx.check_every != 0:
        return
    if not query_rate_limit(ctx):
        return
    if ctx.state.remaining <= ctx.min_remaining:
        sleep_for = ctx.state.reset_epoch - int(time.time()) + 1
        if sleep_for > 0:
            reset_human = datetime.fromtimestamp(
                ctx.state.reset_epoch, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(
                f"Core rate limit low (remaining={ctx.state.remaining}). "
                f"Sleeping ~{sleep_for}s until reset ({reset_human}).",
                file=sys.stderr,
            )
            time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------

def _run_subprocess(args: list[str], env: dict[str, str]) -> tuple[bool, str, str]:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, env=env,
            timeout=SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        partial_stderr = ""
        if exc.stderr:
            raw = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
            partial_stderr = raw[:PARTIAL_STDERR_LIMIT]
        print(f"Timeout ({SUBPROCESS_TIMEOUT}s) running: {' '.join(args)}", file=sys.stderr)
        if partial_stderr:
            print(f"Partial stderr: {partial_stderr}", file=sys.stderr)
        return False, "", "timeout"


def gh_call(args: list[str], ctx: GhContext) -> tuple[bool, str, str]:
    """Run gh CLI with proactive + reactive rate limit handling and exponential backoff."""
    delay = SECONDARY_RATE_LIMIT_SLEEP

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        maybe_pause_for_rate_limit(ctx)
        success, stdout, stderr = _run_subprocess(args, ctx.env)

        if success:
            return True, stdout, stderr

        combined = (stderr + stdout).lower()

        # Auth/permission failures are not retryable.
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

        is_secondary = any(pat in combined for pat in _SECONDARY_RATE_LIMIT_PATTERNS)
        if is_secondary:
            print(
                f"Secondary rate limit (attempt {attempt + 1}/{MAX_RATE_LIMIT_RETRIES}). "
                f"Sleeping {delay}s...",
                file=sys.stderr,
            )
            time.sleep(delay)
            delay = min(delay * 2, SECONDARY_BACKOFF_CAP)
        elif query_rate_limit(ctx):
            sleep_for = max(ctx.state.reset_epoch - int(time.time()) + 1, 30)
            print(f"Core rate limit. Sleeping {sleep_for}s until reset...", file=sys.stderr)
            time.sleep(sleep_for)
        else:
            print("Rate limited (could not query reset). Sleeping 30s...", file=sys.stderr)
            time.sleep(30)

    return False, "", ""


# ---------------------------------------------------------------------------
# JSON helper
# ---------------------------------------------------------------------------

def parse_json_safe(stdout: str, context: str = "") -> object | None:
    """Parse JSON from subprocess stdout. Returns None on empty input or parse error."""
    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        print(f"Error: Failed to parse JSON ({context}): {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# GitHub API calls
# ---------------------------------------------------------------------------

def check_org_access(org: str, ctx: GhContext) -> bool:
    success, _, _ = gh_call(["gh", "api", f"orgs/{org}", "--silent"], ctx)
    return success


def _check_and_report(org: str, ctx: GhContext) -> bool:
    print(f"  Checking {org}...", end=" ", flush=True)
    ok = check_org_access(org, ctx)
    print("OK" if ok else "FAILED")
    return ok


def fetch_repos(org: str, limit: int, ctx: GhContext) -> list[dict]:
    """Fetch repos with default branch included to avoid extra calls later."""
    success, stdout, _ = gh_call(
        [
            "gh", "repo", "list", org,
            "--limit", str(limit),
            "--json", "nameWithOwner,hasIssuesEnabled,primaryLanguage,isArchived,defaultBranchRef",
            "--jq", _REPO_JQ,
        ],
        ctx,
    )
    if not success:
        print(f"Error: Failed to fetch repos for org '{org}'.", file=sys.stderr)
        return []
    result = parse_json_safe(stdout, f"fetch_repos org={org}")
    return result if isinstance(result, list) else []


def find_open_issues(repo: str, title: str, ctx: GhContext) -> list[int] | None:
    """Find open issues by exact title match.

    Uses `gh issue list` (GraphQL-backed) without --search, then filters
    by exact title in Python. This avoids:
      - the search API rate limit (30/min) that --search would hit
      - the gh api --paginate + --jq output concatenation problem
        (concatenated arrays produce invalid JSON for json.loads)
      - any jq filter injection concerns
    """
    success, stdout, _ = gh_call(
        [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--limit", "1000",
            "--json", "number,title",
        ],
        ctx,
    )
    if not success:
        return None
    issues = parse_json_safe(stdout, f"find_open_issues repo={repo}")
    if not isinstance(issues, list):
        return None
    return [issue["number"] for issue in issues if issue.get("title") == title]


def get_last_veracode_check(repo: str, branch: str, ctx: GhContext) -> datetime | None:
    """Get most recent Veracode check completion. Branch is passed in to avoid extra API call.

    Note: We don't use --paginate here because:
      1. gh api --paginate + --jq produces concatenated JSON output that
         is invalid for json.loads.
      2. The check-runs endpoint with per_page=100 returns up to 100 runs
         for a single commit, which is far more than any single commit will
         realistically have for Veracode-prefixed checks.
    Filtering and the "max completed_at" logic are done in Python.
    """
    if not branch:
        return None

    success, stdout, _ = gh_call(
        [
            "gh", "api", f"repos/{repo}/commits/{branch}/check-runs",
            "-X", "GET",
            "-f", "per_page=100",
        ],
        ctx,
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


# ---------------------------------------------------------------------------
# CSV row builders
# ---------------------------------------------------------------------------

CREATE_FIELDNAMES = [
    "org", "repo", "primary_language", "issues_enabled", "is_archived",
    "last_check_date", "days_since_check", "action",
]
DELETE_FIELDNAMES = [
    "org", "repo", "primary_language", "is_archived", "issues_deleted", "action",
]


def _row_create(org: str, repo: dict, action: str,
                last_check_date: str = "", days_since_check: str = "") -> dict:
    return {
        "org": org,
        "repo": repo["nameWithOwner"],
        "primary_language": repo["primaryLanguage"],
        "issues_enabled": repo["hasIssuesEnabled"],
        "is_archived": repo["isArchived"],
        "last_check_date": last_check_date,
        "days_since_check": days_since_check,
        "action": action,
    }


def _row_delete(org: str, repo: dict, issues_deleted: int, action: str) -> dict:
    return {
        "org": org,
        "repo": repo["nameWithOwner"],
        "primary_language": repo["primaryLanguage"],
        "is_archived": repo["isArchived"],
        "issues_deleted": issues_deleted,
        "action": action,
    }


def _write_row(csv_writer: csv.DictWriter, csv_file: io.TextIOWrapper,
               row: dict, flush: bool = True) -> None:
    csv_writer.writerow(row)
    if flush:
        csv_file.flush()


# ---------------------------------------------------------------------------
# Mode: delete
# ---------------------------------------------------------------------------

def run_delete_mode(org: str, repos: list[dict],
                    csv_writer: csv.DictWriter, csv_file: io.TextIOWrapper,
                    ctx: GhContext) -> OrgStats:
    stats = OrgStats(org=org)

    for repo in repos:
        name = repo["nameWithOwner"]
        is_archived = repo["isArchived"]
        print("-------------------------------------------")
        print(f"Processing {name}")
        stats.total_repos += 1

        if is_archived:
            stats.archived += 1
            print("Repository is archived. Skipping.")
            _write_row(csv_writer, csv_file, _row_delete(org, repo, 0, "skipped_archived"))
            continue

        issue_numbers = find_open_issues(name, ISSUE_TITLE, ctx)
        if issue_numbers is None:
            print("Could not query issues (API error). Skipping.")
            stats.failed += 1
            _write_row(csv_writer, csv_file, _row_delete(org, repo, 0, "failed_query_issues"))
            continue

        if not issue_numbers:
            print("No matching issues found.")
            stats.skipped_no_issues += 1
            _write_row(csv_writer, csv_file, _row_delete(org, repo, 0, "no_issues_found"))
            continue

        issues_deleted = issues_failed = 0
        for issue_num in issue_numbers:
            print(f"Closing issue #{issue_num}...")
            success, _, _ = gh_call(
                ["gh", "issue", "close", str(issue_num), "--repo", name,
                 "--comment", "Closed by cleanup script"],
                ctx,
            )
            if success:
                issues_deleted += 1
                stats.deleted += 1
                time.sleep(WRITE_THROTTLE_SECONDS)
            else:
                print(f"Failed to close issue #{issue_num}")
                issues_failed += 1
                stats.failed += 1

        action = "partial_delete" if issues_failed > 0 else "deleted"
        _write_row(csv_writer, csv_file, _row_delete(org, repo, issues_deleted, action))

    print(f"\nFinished closing issues for org: {org}\n")
    print(f"Delete Stats for Organization: {org}")
    print("----------------------------------------------------------------")
    print(f"Total Repositories:            {stats.total_repos}")
    print(f"Archived Repositories:         {stats.archived}")
    print(f"Repositories with No Issues:   {stats.skipped_no_issues}")
    print(f"Issues Closed:                 {stats.deleted}")
    print(f"Failed Closes:                 {stats.failed}")
    print("----------------------------------------------------------------")

    return stats


# ---------------------------------------------------------------------------
# Mode: create
# ---------------------------------------------------------------------------

def _restore_issues_disabled(name: str, ctx: GhContext) -> bool:
    """Re-disable issues. Returns True on success."""
    print("Restoring state: Disabling issues...")
    success, _, _ = gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], ctx)
    if not success:
        print(f"Warning: failed to restore issues-disabled state on {name}", file=sys.stderr)
    return success


def run_create_mode(org: str, repos: list[dict],
                    csv_writer: csv.DictWriter, csv_file: io.TextIOWrapper,
                    ctx: GhContext, stale_days: int | None) -> OrgStats:
    stats = OrgStats(org=org)
    check_stale = stale_days is not None and stale_days > 0

    for repo in repos:
        name = repo["nameWithOwner"]
        issues_enabled = repo["hasIssuesEnabled"]
        primary_lang = repo["primaryLanguage"]
        is_archived = repo["isArchived"]
        default_branch = repo.get("defaultBranch", "")

        print("-------------------------------------------")
        print(f"Processing {name}")
        stats.total_repos += 1

        last_check_date = ""
        days_since_check = ""

        if is_archived:
            stats.archived += 1
            print("Repository is archived. Skipping.")
            _write_row(csv_writer, csv_file, _row_create(org, repo, "skipped_archived"))
            continue

        if primary_lang in IAC_LANGUAGES:
            stats.iac += 1

        if check_stale:
            print(f"Checking for Veracode checks in the last {stale_days} days...")
            last_check = get_last_veracode_check(name, default_branch, ctx)
            days_since = calculate_days_since(last_check)

            if last_check:
                last_check_date = last_check.strftime("%Y-%m-%d %H:%M:%S UTC")
            if days_since is not None:
                days_since_check = str(days_since)

            if last_check and days_since is not None and days_since < stale_days:
                print(f"Recent Veracode check found ({days_since} days ago). Skipping.")
                stats.skipped_recent += 1
                _write_row(csv_writer, csv_file,
                           _row_create(org, repo, "skipped_recent_check",
                                       last_check_date, days_since_check))
                continue

            if last_check:
                print(f"Last Veracode check was {days_since} days ago. Proceeding.")
            else:
                print("No Veracode checks found. Proceeding.")

        was_disabled = False
        if not issues_enabled:
            print("Issues are disabled. Temporarily enabling...")
            success, _, _ = gh_call(["gh", "repo", "edit", name, "--enable-issues"], ctx)
            if not success:
                print("Could not enable issues. Skipping issue creation.")
                stats.skipped_perm += 1
                _write_row(csv_writer, csv_file,
                           _row_create(org, repo, "skipped_cant_enable_issues",
                                       last_check_date, days_since_check))
                continue
            was_disabled = True

        existing_issues = find_open_issues(name, ISSUE_TITLE, ctx)

        if existing_issues is None:
            print("Could not query existing issues (API error). Skipping to avoid duplicates.")
            stats.failed += 1
            if was_disabled:
                _restore_issues_disabled(name, ctx)
            _write_row(csv_writer, csv_file,
                       _row_create(org, repo, "failed_query_issues",
                                   last_check_date, days_since_check))
            continue

        if existing_issues:
            print("Open issue with same title already exists. Skipping.")
            stats.skipped_existing += 1
            if was_disabled:
                _restore_issues_disabled(name, ctx)
            _write_row(csv_writer, csv_file,
                       _row_create(org, repo, "skipped_existing_issue",
                                   last_check_date, days_since_check))
            continue

        print("Creating issue...")
        success, _, _ = gh_call(
            ["gh", "issue", "create", "--repo", name,
             "--title", ISSUE_TITLE, "--body", ISSUE_BODY],
            ctx,
        )

        if success:
            stats.created += 1
            action = "created"
            time.sleep(WRITE_THROTTLE_SECONDS)
        else:
            print("Failed to create issue.")
            stats.failed += 1
            action = "failed_create"

        if was_disabled:
            if not _restore_issues_disabled(name, ctx):
                action = f"{action}_restore_failed"

        _write_row(csv_writer, csv_file,
                   _row_create(org, repo, action, last_check_date, days_since_check))

    print(f"\nFinished processing all repositories for org: {org}\n")
    print(f"Repository Stats for Organization: {org}")
    print("----------------------------------------------------------------")
    print(f"Total Repositories:       {stats.total_repos}")
    print(f"Archived Repositories:    {stats.archived}")
    print(f"IaC Repositories:         {stats.iac} (Primary language: HCL/Bicep)")
    print(f"Issues Permission Skips:  {stats.skipped_perm}")
    print(f"Skipped Existing Issues:  {stats.skipped_existing}")
    if stale_days:
        print(f"Skipped Recent Checks:    {stats.skipped_recent} (scanned within {stale_days} days)")
    print(f"Created Issues:           {stats.created}")
    print(f"Failed Creates:           {stats.failed}")
    print("----------------------------------------------------------------")

    return stats


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_org_header(org: str, org_index: int, total_orgs: int) -> None:
    if total_orgs > 1:
        print(f"\n{'#' * 64}")
        print(f"# Organization {org_index}/{total_orgs}: {org}")
        print(f"{'#' * 64}")


def print_multi_org_summary(all_stats: list[OrgStats], delete_mode: bool,
                            stale_days: int | None, output_file: str) -> None:
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

    # Totals row
    totals = ["TOTAL"] + [
        sum(getter(s) for s in all_stats) for _, _, getter in cols[1:]
    ]
    print(" ".join(f"{val:{fmt}}" for (_, fmt, _), val in zip(cols, totals)))

    print(f"\nCSV Output: {output_file}")
    print(f"{'=' * 64}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create or close Veracode baseline scan trigger issues across one or more GitHub orgs."
        )
    )

    org_group = parser.add_mutually_exclusive_group(required=True)
    org_group.add_argument("org", nargs="?", help="Single GitHub organization name")
    org_group.add_argument("--org-file", metavar="FILE",
                           help="Text file with one org name per line")

    repo_filter_group = parser.add_mutually_exclusive_group()
    repo_filter_group.add_argument("--repo-file", metavar="FILE",
                                   help="Text file with one repo name per line")
    repo_filter_group.add_argument("--repo-wildcard", metavar="PATTERN",
                                   help="Wildcard pattern to filter repos (case-insensitive)")

    parser.add_argument("--delete", action="store_true",
                        help="Close previously created trigger issues")
    parser.add_argument("--stale-days", type=int, metavar="N", default=None,
                        help=f"Only create issues for repos not scanned in last N days "
                             f"(suggested: {DEFAULT_STALE_DAYS})")
    parser.add_argument("--hostname", metavar="HOSTNAME", default=None,
                        help="GitHub hostname for GHES/GHEC")
    parser.add_argument("--output", metavar="FILE", default=DEFAULT_OUTPUT_FILE,
                        help=f"Output CSV path (default: {DEFAULT_OUTPUT_FILE})")
    parser.add_argument("--repo-limit", type=int, default=DEFAULT_REPO_LIST_LIMIT,
                        help=f"Max repos per org (default: {DEFAULT_REPO_LIST_LIMIT})")
    parser.add_argument("--min-remaining", type=int, default=DEFAULT_RL_MIN_REMAINING,
                        help=f"Pause when core remaining <= N (default: {DEFAULT_RL_MIN_REMAINING})")
    parser.add_argument("--rl-check-every", type=int, default=DEFAULT_RL_CHECK_EVERY,
                        help=f"Check rate limit every N gh calls (default: {DEFAULT_RL_CHECK_EVERY})")
    args = parser.parse_args()

    if args.rl_check_every < 1:
        sys.exit("Error: --rl-check-every must be >= 1")
    if args.min_remaining < 0:
        sys.exit("Error: --min-remaining must be >= 0")
    if args.stale_days is not None and args.stale_days < 0:
        sys.exit("Error: --stale-days must be >= 0")
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

    ctx = GhContext(
        env=build_gh_env(gh_hostname),
        state=RateLimitState(),
        min_remaining=args.min_remaining,
        check_every=args.rl_check_every,
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

    if args.org_file:
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

    print("\nChecking access to all organizations...")
    inaccessible_orgs = [org for org in orgs if not _check_and_report(org, ctx)]
    if inaccessible_orgs:
        sys.exit(
            f"\nError: Cannot access org(s): {', '.join(inaccessible_orgs)}. "
            "Fix access or remove them and retry."
        )
    print()

    if args.delete:
        print("================================================================")
        print(f"DELETE MODE: Removing issues with title '{ISSUE_TITLE}'")
        print("================================================================\n")
        fieldnames = DELETE_FIELDNAMES
    else:
        fieldnames = CREATE_FIELDNAMES
        if args.stale_days:
            print("================================================================")
            print(f"STALE CHECK MODE: Creating issues for repos not scanned in {args.stale_days} days")
            print("================================================================\n")

    output_file = args.output
    all_stats: list[OrgStats] = []

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
                if repo_filter_names:
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
                    stats = run_delete_mode(org, repos, csv_writer, csv_file, ctx)
                else:
                    stats = run_create_mode(org, repos, csv_writer, csv_file, ctx, args.stale_days)

                all_stats.append(stats)

    except KeyboardInterrupt:
        print("\nInterrupted. Partial CSV output may exist.", file=sys.stderr)
        sys.exit(130)

    if total_orgs > 1 and all_stats:
        print_multi_org_summary(all_stats, args.delete, args.stale_days, output_file)

    print(f"\nCSV Output: {output_file}")


if __name__ == "__main__":
    main()
