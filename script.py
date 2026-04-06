#!/usr/bin/env python3
"""
script.py - Create or close Veracode baseline scan trigger issues across all repos in a GitHub org.
Supports a single org (positional arg) or multiple orgs via --org-file.
Supports GitHub.com, GitHub Enterprise Cloud (GHEC), and GitHub Enterprise Server (GHES) via --hostname.
Supports --stale-days to only create issues for repos not scanned within N days.
Supports --repo-file to target specific repos and --repo-wildcard for pattern matching.
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

_VALID_ORG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,38}$")
_VALID_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

_REPO_JQ = (
    "[.[] | {"
    "nameWithOwner: .nameWithOwner, "
    "hasIssuesEnabled: .hasIssuesEnabled, "
    "primaryLanguage: (.primaryLanguage.name // \"N/A\"), "
    "isArchived: .isArchived"
    "}]"
)

_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "secondary rate",
    "abuse detection",
    "http 403",
    "api rate limit exceeded",
)


class OrgFileError(Exception):
    """Raised for unrecoverable org-file loading failures. Caught in main()."""


class RepoFileError(Exception):
    """Raised for unrecoverable repo-file loading failures. Caught in main()."""


# ---------------------------------------------------------------------------
# Environment helpers (module-level reads, safe against bad env values)
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    """Read an integer from the environment, falling back to default on bad values."""
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
    """
    Tracks GitHub Core REST rate-limit state across gh calls.
    reset_epoch=0 is the uninitialised sentinel; it is only meaningful
    after a successful query_rate_limit() call.
    """
    call_count: int = 0
    remaining: int = 9999
    reset_epoch: int = 0


@dataclass
class GhContext:
    """
    Bundles the four values that travel together through every gh call.
    Pass one GhContext instead of four separate parameters.
    """
    env: dict[str, str]
    state: RateLimitState
    min_remaining: int
    check_every: int


@dataclass
class OrgStats:
    org: str
    total_repos: int = 0
    archived: int = 0
    # create mode
    iac: int = 0
    skipped_perm: int = 0
    skipped_existing: int = 0
    skipped_recent: int = 0
    created: int = 0
    # delete mode
    skipped_no_issues: int = 0
    deleted: int = 0
    # shared
    failed: int = 0


# ---------------------------------------------------------------------------
# Environment / org helpers
# ---------------------------------------------------------------------------

def build_gh_env(gh_hostname: str | None) -> dict[str, str]:
    """
    Return a copy of the current environment, optionally with GH_HOST injected.
    Build once per run in main() and store in GhContext.
    """
    env = os.environ.copy()
    if gh_hostname:
        env["GH_HOST"] = gh_hostname
    return env


def validate_org_name(org: str) -> bool:
    """Return True if org is a plausible GitHub org slug."""
    return bool(_VALID_ORG_RE.match(org))


def validate_repo_name(repo: str) -> bool:
    """Return True if repo is a plausible GitHub repo name (without org prefix)."""
    return bool(_VALID_REPO_RE.match(repo))


def load_orgs_from_file(file_path: str) -> list[str]:
    """Parse an org list file. Validates all names before returning.

    Raises:
        OrgFileError: if the file is missing, empty, or contains invalid org names.
    """
    if not os.path.isfile(file_path):
        raise OrgFileError(f"Org file not found: {file_path}")

    with open(file_path, encoding="utf-8") as fh:
        orgs = [
            stripped for line in fh
            if (stripped := line.strip()) and not stripped.startswith("#")
        ]

    if not orgs:
        raise OrgFileError(
            f"No org names found in '{file_path}' (all lines are blank or comments)."
        )

    invalid = [o for o in orgs if not validate_org_name(o)]
    if invalid:
        raise OrgFileError(
            f"Invalid org name(s) in '{file_path}': {', '.join(invalid)}"
        )

    return orgs


def load_repos_from_file(file_path: str) -> list[str]:
    """Parse a repo list file. Returns repo names (without org prefix).

    Raises:
        RepoFileError: if the file is missing, empty, or contains invalid repo names.
    """
    if not os.path.isfile(file_path):
        raise RepoFileError(f"Repo file not found: {file_path}")

    with open(file_path, encoding="utf-8") as fh:
        repos = [
            stripped for line in fh
            if (stripped := line.strip()) and not stripped.startswith("#")
        ]

    if not repos:
        raise RepoFileError(
            f"No repo names found in '{file_path}' (all lines are blank or comments)."
        )

    invalid = [r for r in repos if not validate_repo_name(r)]
    if invalid:
        raise RepoFileError(
            f"Invalid repo name(s) in '{file_path}': {', '.join(invalid)}"
        )

    return repos


def filter_repos_by_names(repos: list[dict], target_names: list[str]) -> list[dict]:
    """Filter repos to only those matching target names (case-insensitive)."""
    target_set = {name.lower() for name in target_names}
    filtered = []
    for repo in repos:
        repo_name = repo["nameWithOwner"].split("/", 1)[-1].lower()
        if repo_name in target_set:
            filtered.append(repo)
    return filtered


def filter_repos_by_wildcard(repos: list[dict], pattern: str) -> list[dict]:
    """Filter repos using fnmatch-style wildcard pattern (case-insensitive).
    
    Supports:
      - "example*" matches repos starting with "example"
      - "*example" matches repos ending with "example"
      - "*example*" matches repos containing "example"
      - "ex?mple" matches single character wildcard
    """
    pattern_lower = pattern.lower()
    filtered = []
    for repo in repos:
        repo_name = repo["nameWithOwner"].split("/", 1)[-1].lower()
        if fnmatch.fnmatch(repo_name, pattern_lower):
            filtered.append(repo)
    return filtered


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def query_rate_limit(ctx: GhContext) -> bool:
    """Query GitHub core REST rate limit. Updates ctx.state. Returns True on success."""
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
        ctx.state.remaining = int(lines[0])
        ctx.state.reset_epoch = int(lines[1])
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return False


def maybe_pause_for_rate_limit(ctx: GhContext) -> None:
    """Proactively pause if remaining API calls are low."""
    ctx.state.call_count += 1

    # Check on the first call and then every N calls
    if ctx.state.call_count != 1 and ctx.state.call_count % ctx.check_every != 0:
        return

    if not query_rate_limit(ctx):
        return

    if ctx.state.remaining <= ctx.min_remaining:
        now = int(time.time())
        sleep_for = ctx.state.reset_epoch - now + 1
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
    """Execute a subprocess. Returns (success, stdout, stderr). Handles timeout."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=env,
            timeout=SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        partial_stderr = ""
        if exc.stderr:
            raw = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
            partial_stderr = raw[:200]
        print(f"Timeout ({SUBPROCESS_TIMEOUT}s) running: {' '.join(args)}", file=sys.stderr)
        if partial_stderr:
            print(f"Partial stderr: {partial_stderr}", file=sys.stderr)
        return False, "", "timeout"


def gh_call(args: list[str], ctx: GhContext) -> tuple[bool, str, str]:
    """
    Run a gh CLI command with proactive and reactive rate limit handling.
    Returns (success, stdout, stderr). Retries once after sleeping if rate limited.
    """
    maybe_pause_for_rate_limit(ctx)

    success, stdout, stderr = _run_subprocess(args, ctx.env)

    if success:
        return True, stdout, stderr

    combined = (stderr + stdout).lower()
    is_rate_limited = any(pat in combined for pat in _RATE_LIMIT_PATTERNS)

    if is_rate_limited:
        if query_rate_limit(ctx):
            now = int(time.time())
            sleep_for = ctx.state.reset_epoch - now + 1
            if sleep_for > 0:
                print(
                    f"Rate limited. Sleeping {sleep_for}s until reset, then retrying once...",
                    file=sys.stderr,
                )
                time.sleep(sleep_for)
        else:
            print("Rate limited. Sleeping 30s (could not query reset)...", file=sys.stderr)
            time.sleep(30)

        success, stdout, stderr = _run_subprocess(args, ctx.env)
        if success:
            return True, stdout, stderr

    print(stderr, file=sys.stderr)
    return False, stdout, stderr


# ---------------------------------------------------------------------------
# JSON helper
# ---------------------------------------------------------------------------

def parse_json_safe(stdout: str, context: str) -> list | dict | None:
    """Parse JSON from subprocess stdout. Returns None and logs on parse failure."""
    stripped = stdout.strip()
    if not stripped:
        return []
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        print(f"Error: Failed to parse JSON ({context}): {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# GitHub API calls
# ---------------------------------------------------------------------------

def check_org_access(org: str, ctx: GhContext) -> bool:
    """Returns True if the authenticated user can access the org."""
    success, _, _ = gh_call(["gh", "api", f"orgs/{org}", "--silent"], ctx)
    return success


def _check_and_report(org: str, ctx: GhContext) -> bool:
    """Check org access and print result inline."""
    print(f"  Checking {org}...", end=" ", flush=True)
    ok = check_org_access(org, ctx)
    print("OK" if ok else "FAILED")
    return ok


def fetch_repos(org: str, limit: int, ctx: GhContext) -> list[dict]:
    """Fetch all repos for the org as a list of dicts."""
    success, stdout, _ = gh_call(
        [
            "gh", "repo", "list", org,
            "--limit", str(limit),
            "--json", "nameWithOwner,hasIssuesEnabled,primaryLanguage,isArchived",
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
    """Return open issue numbers matching title, or None if the API call failed.

    Callers must distinguish None (call failed) from [] (no issues found):
    - None   -> API call failed; treat as unknown, do not proceed
    - []     -> no matching open issues exist
    - [...]  -> matching issues were found
    """
    success, stdout, _ = gh_call(
        [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--search", f"{title} in:title",
            "--json", "number",
            "--jq", "[.[].number]",
        ],
        ctx,
    )
    if not success:
        return None
    result = parse_json_safe(stdout, f"find_open_issues repo={repo}")
    return result if isinstance(result, list) else None


def get_last_veracode_check(repo: str, ctx: GhContext) -> datetime | None:
    """Return the most recent Veracode check run completion time, or None if not found."""
    # Get default branch first
    success, stdout, _ = gh_call(
        ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
        ctx,
    )
    if not success or not stdout.strip():
        return None
    branch = stdout.strip()

    # Query check runs on the default branch head
    success, stdout, _ = gh_call(
        [
            "gh", "api", f"repos/{repo}/commits/{branch}/check-runs",
            "--jq",
            '[.check_runs[] | select(.name | startswith("Veracode")) | .completed_at] | map(select(. != null)) | max',
        ],
        ctx,
    )
    if not success:
        return None

    stripped = stdout.strip()
    if not stripped or stripped == "null":
        return None

    try:
        return datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        return None


def calculate_days_since(dt: datetime | None) -> int | None:
    """Return number of days since the given datetime, or None if dt is None."""
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    delta = now - dt
    return delta.days


# ---------------------------------------------------------------------------
# Mode: delete
# ---------------------------------------------------------------------------

def run_delete_mode(
    org: str,
    repos: list[dict],
    csv_writer: csv.DictWriter[str],
    csv_file: io.TextIOWrapper,
    ctx: GhContext,
) -> OrgStats:
    """Close all open trigger issues across org repos."""
    stats = OrgStats(org=org)

    for repo in repos:
        name = repo["nameWithOwner"]
        primary_lang = repo["primaryLanguage"]
        is_archived = repo["isArchived"]

        print("-------------------------------------------")
        print(f"Processing {name}")
        stats.total_repos += 1

        if is_archived:
            stats.archived += 1
            print("Repository is archived. Skipping.")
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "is_archived": is_archived, "issues_deleted": 0, "action": "skipped_archived",
            })
            csv_file.flush()
            continue

        issue_numbers = find_open_issues(name, ISSUE_TITLE, ctx)

        if issue_numbers is None:
            print("Could not query issues (API error). Skipping.")
            stats.failed += 1
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "is_archived": is_archived, "issues_deleted": 0, "action": "failed_query_issues",
            })
            csv_file.flush()
            continue

        if not issue_numbers:
            print("No matching issues found.")
            stats.skipped_no_issues += 1
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "is_archived": is_archived, "issues_deleted": 0, "action": "no_issues_found",
            })
            csv_file.flush()
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
            else:
                print(f"Failed to close issue #{issue_num}")
                issues_failed += 1
                stats.failed += 1

        action = "partial_delete" if issues_failed > 0 else "deleted"
        csv_writer.writerow({
            "org": org, "repo": name, "primary_language": primary_lang,
            "is_archived": is_archived, "issues_deleted": issues_deleted, "action": action,
        })
        csv_file.flush()

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

def run_create_mode(
    org: str,
    repos: list[dict],
    csv_writer: csv.DictWriter[str],
    csv_file: io.TextIOWrapper,
    ctx: GhContext,
    stale_days: int | None,
) -> OrgStats:
    """Create baseline scan trigger issues across org repos."""
    stats = OrgStats(org=org)
    check_stale = stale_days is not None and stale_days > 0

    for repo in repos:
        name = repo["nameWithOwner"]
        issues_enabled = repo["hasIssuesEnabled"]
        primary_lang = repo["primaryLanguage"]
        is_archived = repo["isArchived"]

        print("-------------------------------------------")
        print(f"Processing {name}")
        stats.total_repos += 1

        # Initialize stale check fields
        last_check_date: str = ""
        days_since_check: str = ""

        if is_archived:
            stats.archived += 1
            print("Repository is archived. Skipping.")
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "issues_enabled": issues_enabled, "is_archived": is_archived,
                "last_check_date": last_check_date, "days_since_check": days_since_check,
                "action": "skipped_archived",
            })
            csv_file.flush()
            continue

        # IaC count after archived guard: archived IaC repos are not included
        if primary_lang in IAC_LANGUAGES:
            stats.iac += 1

        # Check for recent Veracode scans if --stale-days is set
        if check_stale:
            print(f"Checking for Veracode checks in the last {stale_days} days...")
            last_check = get_last_veracode_check(name, ctx)
            days_since = calculate_days_since(last_check)

            if last_check:
                last_check_date = last_check.strftime("%Y-%m-%d %H:%M:%S UTC")
            if days_since is not None:
                days_since_check = str(days_since)

            if last_check and days_since is not None and days_since < stale_days:
                print(f"Recent Veracode check found ({days_since} days ago). Skipping.")
                stats.skipped_recent += 1
                csv_writer.writerow({
                    "org": org, "repo": name, "primary_language": primary_lang,
                    "issues_enabled": issues_enabled, "is_archived": is_archived,
                    "last_check_date": last_check_date, "days_since_check": days_since_check,
                    "action": "skipped_recent_check",
                })
                csv_file.flush()
                continue

            if last_check:
                print(f"Last Veracode check was {days_since} days ago. Proceeding.")
            else:
                print("No Veracode checks found. Proceeding.")

        was_disabled = False
        if not issues_enabled:
            print("Issues are disabled. Temporarily enabling...")
            success, _, _ = gh_call(
                ["gh", "repo", "edit", name, "--enable-issues"], ctx
            )
            if not success:
                print("Could not enable issues. Skipping issue creation.")
                stats.skipped_perm += 1
                csv_writer.writerow({
                    "org": org, "repo": name, "primary_language": primary_lang,
                    "issues_enabled": issues_enabled, "is_archived": is_archived,
                    "last_check_date": last_check_date, "days_since_check": days_since_check,
                    "action": "skipped_cant_enable_issues",
                })
                csv_file.flush()
                continue
            was_disabled = True

        existing_issues = find_open_issues(name, ISSUE_TITLE, ctx)

        if existing_issues is None:
            print("Could not query existing issues (API error). Skipping to avoid duplicates.")
            stats.failed += 1
            if was_disabled:
                print("Restoring state: Disabling issues...")
                gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], ctx)
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "issues_enabled": issues_enabled, "is_archived": is_archived,
                "last_check_date": last_check_date, "days_since_check": days_since_check,
                "action": "failed_query_issues",
            })
            csv_file.flush()
            continue

        if existing_issues:
            print("Open issue with same title already exists. Skipping.")
            stats.skipped_existing += 1
            if was_disabled:
                print("Restoring state: Disabling issues...")
                gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], ctx)
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "issues_enabled": issues_enabled, "is_archived": is_archived,
                "last_check_date": last_check_date, "days_since_check": days_since_check,
                "action": "skipped_existing_issue",
            })
            csv_file.flush()
            continue

        print("Creating issue...")
        success, _, _ = gh_call(
            ["gh", "issue", "create", "--repo", name, "--title", ISSUE_TITLE, "--body", ISSUE_BODY],
            ctx,
        )

        if success:
            stats.created += 1
            action = "created"
        else:
            print("Failed to create issue.")
            stats.failed += 1
            action = "failed_create"

        if was_disabled:
            print("Restoring state: Disabling issues...")
            gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], ctx)

        csv_writer.writerow({
            "org": org, "repo": name, "primary_language": primary_lang,
            "issues_enabled": issues_enabled, "is_archived": is_archived,
            "last_check_date": last_check_date, "days_since_check": days_since_check,
            "action": action,
        })
        csv_file.flush()

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
# Summary + header
# ---------------------------------------------------------------------------

def print_org_header(org: str, org_index: int, total_orgs: int) -> None:
    """Print a visible org header in multi-org mode."""
    if total_orgs > 1:
        print(f"\n{'#' * 64}")
        print(f"# Organization {org_index}/{total_orgs}: {org}")
        print(f"{'#' * 64}")


def print_multi_org_summary(
    all_stats: list[OrgStats],
    delete_mode: bool,
    stale_days: int | None,
    output_file: str,
) -> None:
    """Print a rolled-up summary across all orgs."""
    print(f"\n{'=' * 64}")
    print("MULTI-ORG SUMMARY")
    print(f"{'=' * 64}")

    if delete_mode:
        total_repos     = sum(s.total_repos        for s in all_stats)
        total_deleted   = sum(s.deleted            for s in all_stats)
        total_failed    = sum(s.failed             for s in all_stats)
        total_archived  = sum(s.archived           for s in all_stats)
        total_no_issues = sum(s.skipped_no_issues  for s in all_stats)

        print(f"{'Org':<30} {'Repos':>6} {'Closed':>8} {'Failed':>8} {'Archived':>9} {'No Issues':>10}")
        print("-" * 75)
        for s in all_stats:
            print(
                f"{s.org:<30} {s.total_repos:>6} {s.deleted:>8} {s.failed:>8} "
                f"{s.archived:>9} {s.skipped_no_issues:>10}"
            )
        print("-" * 75)
        print(
            f"{'TOTAL':<30} {total_repos:>6} {total_deleted:>8} {total_failed:>8} "
            f"{total_archived:>9} {total_no_issues:>10}"
        )
    else:
        total_repos    = sum(s.total_repos       for s in all_stats)
        total_created  = sum(s.created           for s in all_stats)
        total_failed   = sum(s.failed            for s in all_stats)
        total_archived = sum(s.archived          for s in all_stats)
        total_skipped  = sum(s.skipped_existing  for s in all_stats)
        total_perm     = sum(s.skipped_perm      for s in all_stats)
        total_iac      = sum(s.iac               for s in all_stats)
        total_recent   = sum(s.skipped_recent    for s in all_stats)

        if stale_days:
            print(
                f"{'Org':<30} {'Repos':>6} {'Created':>8} {'Failed':>8} "
                f"{'Archived':>9} {'Skipped':>8} {'PermSkip':>9} {'Recent':>7} {'IaC':>5}"
            )
            print("-" * 97)
            for s in all_stats:
                print(
                    f"{s.org:<30} {s.total_repos:>6} {s.created:>8} {s.failed:>8} "
                    f"{s.archived:>9} {s.skipped_existing:>8} {s.skipped_perm:>9} {s.skipped_recent:>7} {s.iac:>5}"
                )
            print("-" * 97)
            print(
                f"{'TOTAL':<30} {total_repos:>6} {total_created:>8} {total_failed:>8} "
                f"{total_archived:>9} {total_skipped:>8} {total_perm:>9} {total_recent:>7} {total_iac:>5}"
            )
        else:
            print(
                f"{'Org':<30} {'Repos':>6} {'Created':>8} {'Failed':>8} "
                f"{'Archived':>9} {'Skipped':>8} {'PermSkip':>9} {'IaC':>5}"
            )
            print("-" * 90)
            for s in all_stats:
                print(
                    f"{s.org:<30} {s.total_repos:>6} {s.created:>8} {s.failed:>8} "
                    f"{s.archived:>9} {s.skipped_existing:>8} {s.skipped_perm:>9} {s.iac:>5}"
                )
            print("-" * 90)
            print(
                f"{'TOTAL':<30} {total_repos:>6} {total_created:>8} {total_failed:>8} "
                f"{total_archived:>9} {total_skipped:>8} {total_perm:>9} {total_iac:>5}"
            )

    print(f"\nCSV Output: {output_file}")
    print(f"{'=' * 64}")


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create or close Veracode baseline scan trigger issues across one or more GitHub orgs. "
            "Supports GitHub.com, GitHub Enterprise Cloud (GHEC), and GitHub Enterprise Server (GHES)."
        )
    )

    org_group = parser.add_mutually_exclusive_group(required=True)
    org_group.add_argument("org", nargs="?", help="Single GitHub organization name")
    org_group.add_argument(
        "--org-file", metavar="FILE",
        help="Path to a text file with one org name per line (# lines are comments)",
    )

    repo_filter_group = parser.add_mutually_exclusive_group()
    repo_filter_group.add_argument(
        "--repo-file", metavar="FILE",
        help="Path to a text file with one repo name per line (without org prefix, # lines are comments)",
    )
    repo_filter_group.add_argument(
        "--repo-wildcard", metavar="PATTERN",
        help=(
            "Filter repos using wildcard pattern. Examples: "
            "'example*' (starts with), '*example' (ends with), '*example*' (contains), "
            "'ex?mple' (single char wildcard). Case-insensitive."
        ),
    )

    parser.add_argument("--delete", action="store_true", help="Close previously created trigger issues")
    parser.add_argument(
        "--stale-days", type=int, metavar="N", default=None,
        help=(
            f"Only create issues for repos not scanned in the last N days. "
            f"Checks for Veracode check runs (SAST, SCA, IaC). "
            f"Default: disabled. Use --stale-days {DEFAULT_STALE_DAYS} for 30-day threshold. "
            f"Set to 0 to disable."
        ),
    )
    parser.add_argument(
        "--hostname", metavar="HOSTNAME", default=None,
        help=(
            "GitHub hostname to target. Omit for github.com. "
            "For GHES: your instance hostname e.g. github.mycompany.com. "
            "For GHEC: your enterprise subdomain e.g. myorg.ghe.com. "
            "Sets GH_HOST for all gh calls without modifying your shell environment. "
            "Ensure GH_ENTERPRISE_TOKEN (GHES) or GH_TOKEN (GHEC/github.com) is set before running."
        ),
    )
    parser.add_argument(
        "--output", metavar="FILE", default=DEFAULT_OUTPUT_FILE,
        help=f"Output CSV file path (default: {DEFAULT_OUTPUT_FILE})",
    )
    parser.add_argument(
        "--repo-limit", type=int, default=DEFAULT_REPO_LIST_LIMIT,
        help=f"Max repos to fetch per org (default: {DEFAULT_REPO_LIST_LIMIT})",
    )
    parser.add_argument(
        "--min-remaining", type=int, default=DEFAULT_RL_MIN_REMAINING,
        help=f"Pause when core API remaining <= N (default: {DEFAULT_RL_MIN_REMAINING})",
    )
    parser.add_argument(
        "--rl-check-every", type=int, default=DEFAULT_RL_CHECK_EVERY,
        help=f"Check rate limit every N gh calls (default: {DEFAULT_RL_CHECK_EVERY})",
    )
    args = parser.parse_args()

    # --- CLI argument validation ---
    if args.rl_check_every < 1:
        print("Error: --rl-check-every must be >= 1", file=sys.stderr)
        sys.exit(1)

    if args.min_remaining < 0:
        print("Error: --min-remaining must be >= 0", file=sys.stderr)
        sys.exit(1)

    if args.stale_days is not None and args.stale_days < 0:
        print("Error: --stale-days must be >= 0", file=sys.stderr)
        sys.exit(1)

    if args.delete and args.stale_days is not None:
        print("Warning: --stale-days is ignored in delete mode.", file=sys.stderr)

    if shutil.which("gh") is None:
        print("Error: GitHub CLI (gh) is not installed.", file=sys.stderr)
        sys.exit(1)

    gh_hostname = args.hostname

    if gh_hostname and "ghe.com" not in gh_hostname and not os.environ.get("GH_ENTERPRISE_TOKEN"):
        print(
            f"Warning: --hostname is set to '{gh_hostname}' but GH_ENTERPRISE_TOKEN is not set. "
            "Authentication may fail for GHES. Set GH_ENTERPRISE_TOKEN before running.",
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

    # --- Load repo filter if specified ---
    repo_filter_names: list[str] | None = None
    repo_filter_pattern: str | None = None

    if args.repo_file:
        try:
            repo_filter_names = load_repos_from_file(args.repo_file)
        except RepoFileError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Loaded {len(repo_filter_names)} repo name(s) from '{args.repo_file}'.")

    if args.repo_wildcard:
        repo_filter_pattern = args.repo_wildcard
        print(f"Using repo wildcard filter: '{repo_filter_pattern}'")

    # --- Resolve org list ---
    if args.org_file:
        try:
            orgs = load_orgs_from_file(args.org_file)
        except OrgFileError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Loaded {len(orgs)} org(s) from '{args.org_file}'.")
    else:
        if not validate_org_name(args.org):
            print(f"Error: Invalid org name: {args.org!r}", file=sys.stderr)
            sys.exit(1)
        orgs = [args.org]

    total_orgs = len(orgs)

    # --- Pre-flight access check ---
    print("\nChecking access to all organizations...")
    inaccessible_orgs = [org for org in orgs if not _check_and_report(org, ctx)]

    if inaccessible_orgs:
        print(
            f"\nError: Cannot access the following org(s): {', '.join(inaccessible_orgs)}",
            file=sys.stderr,
        )
        print("Aborting. Fix access or remove them from the org file and retry.", file=sys.stderr)
        sys.exit(1)

    print()

    if args.delete:
        print("================================================================")
        print(f"DELETE MODE: Removing issues with title '{ISSUE_TITLE}'")
        print("================================================================\n")
        fieldnames = ["org", "repo", "primary_language", "is_archived", "issues_deleted", "action"]
    else:
        fieldnames = ["org", "repo", "primary_language", "issues_enabled", "is_archived",
                      "last_check_date", "days_since_check", "action"]
        if args.stale_days:
            print("================================================================")
            print(f"STALE CHECK MODE: Creating issues for repos not scanned in {args.stale_days} days")
            print("================================================================\n")

    output_file = args.output
    all_stats: list[OrgStats] = []

    if os.path.exists(output_file):
        print(f"Warning: '{output_file}' already exists and will be overwritten.", file=sys.stderr)

    try:
        with open(output_file, "w", newline="") as csv_file:
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

                # Apply repo filtering
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
