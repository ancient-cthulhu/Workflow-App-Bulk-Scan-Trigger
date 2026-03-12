#!/usr/bin/env python3
"""
script.py - Create or close Veracode baseline scan trigger issues across all repos in a GitHub org.
Supports a single org (positional arg) or multiple orgs via --org-file.
Supports GitHub.com, GitHub Enterprise Cloud (GHEC), and GitHub Enterprise Server (GHES) via --hostname.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime

OUTPUT_FILE = "vcbaseline.csv"
ISSUE_TITLE = "Veracode Baseline Scans"
ISSUE_BODY = "Veracode All Scans"
IAC_LANGUAGES = {"HCL", "Bicep"}

# Rate limit defaults (overridable via env or CLI flags)
DEFAULT_RL_MIN_REMAINING = int(os.environ.get("GH_RL_MIN_REMAINING", 100))
DEFAULT_RL_CHECK_EVERY = int(os.environ.get("GH_RL_CHECK_EVERY", 50))
DEFAULT_REPO_LIST_LIMIT = int(os.environ.get("REPO_LIST_LIMIT", 1000))


@dataclass
class RateLimitState:
    call_count: int = 0
    remaining: int = 9999
    reset_epoch: int = 0


rate_limit_state = RateLimitState()


def build_gh_env(gh_hostname: str | None) -> dict:
    """
    Build the subprocess environment for gh calls.
    If a hostname is provided, sets GH_HOST so all gh commands target that host.
    Inherits the full current environment so existing GH_TOKEN / GH_ENTERPRISE_TOKEN
    credentials are picked up automatically.
    """
    env = os.environ.copy()
    if gh_hostname:
        env["GH_HOST"] = gh_hostname
    return env


def query_rate_limit(gh_hostname: str | None) -> bool:
    """Query GitHub core REST rate limit. Updates global state. Returns True on success."""
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit", "--jq", ".resources.core.remaining, .resources.core.reset"],
            capture_output=True,
            text=True,
            check=True,
            env=build_gh_env(gh_hostname),
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return False
        rate_limit_state.remaining = int(lines[0])
        rate_limit_state.reset_epoch = int(lines[1])
        return True
    except (subprocess.CalledProcessError, ValueError):
        return False


def maybe_pause_for_rate_limit(min_remaining: int, check_every: int, gh_hostname: str | None) -> None:
    """Proactively pause if remaining API calls are low."""
    rate_limit_state.call_count += 1

    if rate_limit_state.call_count % check_every != 0:
        return

    if not query_rate_limit(gh_hostname):
        return

    if rate_limit_state.remaining <= min_remaining:
        now = int(time.time())
        sleep_for = rate_limit_state.reset_epoch - now + 1
        if sleep_for > 0:
            reset_human = datetime.fromtimestamp(rate_limit_state.reset_epoch).strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"Core rate limit low (remaining={rate_limit_state.remaining}). "
                f"Sleeping ~{sleep_for}s until reset ({reset_human}).",
                file=sys.stderr,
            )
            time.sleep(sleep_for)


def gh_call(
    args: list[str],
    min_remaining: int,
    check_every: int,
    gh_hostname: str | None = None,
) -> tuple[bool, str, str]:
    """
    Run a gh CLI command with proactive and reactive rate limit handling.
    Injects GH_HOST into the subprocess environment if gh_hostname is provided,
    so all calls target the correct GitHub instance without modifying the parent shell.
    Returns (success, stdout, stderr). Retries once after sleeping if rate limited.
    """
    maybe_pause_for_rate_limit(min_remaining, check_every, gh_hostname)

    env = build_gh_env(gh_hostname)

    def run_command() -> tuple[bool, str, str]:
        result = subprocess.run(args, capture_output=True, text=True, env=env)
        return result.returncode == 0, result.stdout, result.stderr

    success, stdout, stderr = run_command()

    if success:
        return True, stdout, stderr

    rate_limit_indicators = [
        "rate limit", "secondary rate", "abuse detection", "HTTP 403", "API rate limit exceeded"
    ]
    combined_output = (stderr + stdout).lower()
    is_rate_limited = any(indicator.lower() in combined_output for indicator in rate_limit_indicators)

    if is_rate_limited:
        if query_rate_limit(gh_hostname):
            now = int(time.time())
            sleep_for = rate_limit_state.reset_epoch - now + 1
            if sleep_for > 0:
                print(f"Rate limited. Sleeping {sleep_for}s until reset, then retrying once...", file=sys.stderr)
                time.sleep(sleep_for)
        else:
            print("Rate limited. Sleeping 30s (could not query reset)...", file=sys.stderr)
            time.sleep(30)

        success, stdout, stderr = run_command()
        if success:
            return True, stdout, stderr

    print(stderr, file=sys.stderr)
    return False, stdout, stderr


def load_orgs_from_file(file_path: str) -> list[str]:
    """
    Parse an org list file. Returns a list of org names.
    Skips blank lines and lines starting with #.
    """
    if not os.path.isfile(file_path):
        print(f"Error: Org file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    orgs = []
    with open(file_path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            orgs.append(line)

    if not orgs:
        print(f"Error: No org names found in '{file_path}' (all lines are blank or comments).", file=sys.stderr)
        sys.exit(1)

    return orgs


def check_org_access(org: str, min_remaining: int, check_every: int, gh_hostname: str | None) -> bool:
    """Returns True if the authenticated user can access the org."""
    success, _, _ = gh_call(
        ["gh", "api", f"orgs/{org}", "--silent"],
        min_remaining, check_every, gh_hostname,
    )
    return success


def fetch_repos(
    org: str, limit: int, min_remaining: int, check_every: int, gh_hostname: str | None
) -> list[dict]:
    """Fetch all repos for the org as a list of dicts."""
    jq_query = (
        "[.[] | {"
        "nameWithOwner: .nameWithOwner, "
        "hasIssuesEnabled: .hasIssuesEnabled, "
        "primaryLanguage: (.primaryLanguage.name // \"N/A\"), "
        "isArchived: .isArchived"
        "}]"
    )
    success, stdout, _ = gh_call(
        ["gh", "repo", "list", org, "--limit", str(limit), "--json",
         "nameWithOwner,hasIssuesEnabled,primaryLanguage,isArchived", "--jq", jq_query],
        min_remaining, check_every, gh_hostname,
    )
    if not success:
        print(f"Error: Failed to fetch repos for org '{org}'.", file=sys.stderr)
        return []
    return json.loads(stdout.strip()) if stdout.strip() else []


def find_open_issues(
    repo: str, title: str, min_remaining: int, check_every: int, gh_hostname: str | None
) -> list[int]:
    """Return list of open issue numbers matching the given title."""
    success, stdout, _ = gh_call(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         "--search", f"{title} in:title", "--json", "number", "--jq", "[.[].number]"],
        min_remaining, check_every, gh_hostname,
    )
    if not success or not stdout.strip():
        return []
    return json.loads(stdout.strip())


def print_org_header(org: str, org_index: int, total_orgs: int) -> None:
    """Print a visible header when processing multiple orgs."""
    if total_orgs > 1:
        print(f"\n{'#' * 64}")
        print(f"# Organization {org_index}/{total_orgs}: {org}")
        print(f"{'#' * 64}")


def run_delete_mode(
    org: str,
    repos: list[dict],
    csv_writer: csv.DictWriter,
    min_remaining: int,
    check_every: int,
    gh_hostname: str | None,
) -> dict:
    """
    Close all open trigger issues across org repos.
    Returns a stats dict for use in multi-org summaries.
    """
    print("================================================================")
    print(f"DELETE MODE: Removing issues with title '{ISSUE_TITLE}'")
    print("================================================================\n")

    total_repos = archived_count = deleted_count = skipped_no_issues = failed_delete_count = 0

    for repo in repos:
        name = repo["nameWithOwner"]
        primary_lang = repo["primaryLanguage"]
        is_archived = repo["isArchived"]

        print("-------------------------------------------")
        print(f"Processing {name}")
        total_repos += 1

        if is_archived:
            archived_count += 1
            print("Repository is archived. Skipping.")
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "is_archived": is_archived, "issues_deleted": 0, "action": "skipped_archived",
            })
            continue

        issue_numbers = find_open_issues(name, ISSUE_TITLE, min_remaining, check_every, gh_hostname)

        if not issue_numbers:
            print("No matching issues found.")
            skipped_no_issues += 1
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "is_archived": is_archived, "issues_deleted": 0, "action": "no_issues_found",
            })
            continue

        issues_deleted = issues_failed = 0

        for issue_num in issue_numbers:
            print(f"Closing issue #{issue_num}...")
            success, _, _ = gh_call(
                ["gh", "issue", "close", str(issue_num), "--repo", name,
                 "--comment", "Closed by cleanup script"],
                min_remaining, check_every, gh_hostname,
            )
            if success:
                issues_deleted += 1
                deleted_count += 1
            else:
                print(f"Failed to close issue #{issue_num}")
                issues_failed += 1
                failed_delete_count += 1

        action = "partial_delete" if issues_failed > 0 else "deleted"
        csv_writer.writerow({
            "org": org, "repo": name, "primary_language": primary_lang,
            "is_archived": is_archived, "issues_deleted": issues_deleted, "action": action,
        })

    print(f"\nFinished closing issues for org: {org}\n")
    print(f"Delete Stats for Organization: {org}")
    print("----------------------------------------------------------------")
    print(f"Total Repositories:            {total_repos}")
    print(f"Archived Repositories:         {archived_count}")
    print(f"Repositories with No Issues:   {skipped_no_issues}")
    print(f"Issues Closed:                 {deleted_count}")
    print(f"Failed Closes:                 {failed_delete_count}")
    print("----------------------------------------------------------------")

    return {
        "org": org,
        "total_repos": total_repos,
        "archived": archived_count,
        "skipped_no_issues": skipped_no_issues,
        "deleted": deleted_count,
        "failed": failed_delete_count,
    }


def run_create_mode(
    org: str,
    repos: list[dict],
    csv_writer: csv.DictWriter,
    min_remaining: int,
    check_every: int,
    gh_hostname: str | None,
) -> dict:
    """
    Create baseline scan trigger issues across org repos.
    Returns a stats dict for use in multi-org summaries.
    """
    total_count = iac_count = archived_count = created_count = 0
    skipped_existing_count = skipped_archived_count = skipped_issues_perm_count = failed_count = 0

    for repo in repos:
        name = repo["nameWithOwner"]
        issues_enabled = repo["hasIssuesEnabled"]
        primary_lang = repo["primaryLanguage"]
        is_archived = repo["isArchived"]

        print("-------------------------------------------")
        print(f"Processing {name}")
        total_count += 1

        if primary_lang in IAC_LANGUAGES:
            iac_count += 1

        if is_archived:
            archived_count += 1
            skipped_archived_count += 1
            print("Repository is archived. Skipping.")
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "issues_enabled": issues_enabled, "is_archived": is_archived, "action": "skipped_archived",
            })
            continue

        was_disabled = False
        if not issues_enabled:
            print("Issues are disabled. Temporarily enabling...")
            success, _, _ = gh_call(
                ["gh", "repo", "edit", name, "--enable-issues"],
                min_remaining, check_every, gh_hostname,
            )
            if not success:
                print("Could not enable issues. Skipping issue creation.")
                skipped_issues_perm_count += 1
                csv_writer.writerow({
                    "org": org, "repo": name, "primary_language": primary_lang,
                    "issues_enabled": issues_enabled, "is_archived": is_archived,
                    "action": "skipped_cant_enable_issues",
                })
                continue
            was_disabled = True

        existing_issues = find_open_issues(name, ISSUE_TITLE, min_remaining, check_every, gh_hostname)

        if existing_issues:
            print("Open issue with same title already exists. Skipping.")
            skipped_existing_count += 1
            if was_disabled:
                print("Restoring state: Disabling issues...")
                gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], min_remaining, check_every, gh_hostname)
            csv_writer.writerow({
                "org": org, "repo": name, "primary_language": primary_lang,
                "issues_enabled": issues_enabled, "is_archived": is_archived,
                "action": "skipped_existing_issue",
            })
            continue

        print("Creating issue...")
        success, _, _ = gh_call(
            ["gh", "issue", "create", "--repo", name, "--title", ISSUE_TITLE, "--body", ISSUE_BODY],
            min_remaining, check_every, gh_hostname,
        )

        if success:
            created_count += 1
            action = "created"
        else:
            print("Failed to create issue.")
            failed_count += 1
            action = "failed_create"

        if was_disabled:
            print("Restoring state: Disabling issues...")
            gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], min_remaining, check_every, gh_hostname)

        csv_writer.writerow({
            "org": org, "repo": name, "primary_language": primary_lang,
            "issues_enabled": issues_enabled, "is_archived": is_archived, "action": action,
        })

    print(f"\nFinished processing all repositories for org: {org}\n")
    print(f"Repository Stats for Organization: {org}")
    print("----------------------------------------------------------------")
    print(f"Total Repositories:       {total_count}")
    print(f"Archived Repositories:    {archived_count}")
    print(f"Skipped Archived:         {skipped_archived_count}")
    print(f"IaC Repositories:         {iac_count} (Primary language: HCL/Bicep)")
    print(f"Issues Permission Skips:  {skipped_issues_perm_count}")
    print(f"Skipped Existing Issues:  {skipped_existing_count}")
    print(f"Created Issues:           {created_count}")
    print(f"Failed Creates:           {failed_count}")
    print("----------------------------------------------------------------")

    return {
        "org": org,
        "total_repos": total_count,
        "archived": archived_count,
        "iac": iac_count,
        "skipped_perm": skipped_issues_perm_count,
        "skipped_existing": skipped_existing_count,
        "created": created_count,
        "failed": failed_count,
    }


def print_multi_org_summary(all_stats: list[dict], delete_mode: bool) -> None:
    """Print a rolled-up summary across all orgs when running in multi-org mode."""
    print(f"\n{'=' * 64}")
    print("MULTI-ORG SUMMARY")
    print(f"{'=' * 64}")

    if delete_mode:
        total_repos = sum(s["total_repos"] for s in all_stats)
        total_deleted = sum(s["deleted"] for s in all_stats)
        total_failed = sum(s["failed"] for s in all_stats)
        total_archived = sum(s["archived"] for s in all_stats)
        total_no_issues = sum(s["skipped_no_issues"] for s in all_stats)

        print(f"{'Org':<30} {'Repos':>6} {'Closed':>8} {'Failed':>8} {'Archived':>9} {'No Issues':>10}")
        print("-" * 75)
        for s in all_stats:
            print(f"{s['org']:<30} {s['total_repos']:>6} {s['deleted']:>8} {s['failed']:>8} {s['archived']:>9} {s['skipped_no_issues']:>10}")
        print("-" * 75)
        print(f"{'TOTAL':<30} {total_repos:>6} {total_deleted:>8} {total_failed:>8} {total_archived:>9} {total_no_issues:>10}")
    else:
        total_repos = sum(s["total_repos"] for s in all_stats)
        total_created = sum(s["created"] for s in all_stats)
        total_failed = sum(s["failed"] for s in all_stats)
        total_archived = sum(s["archived"] for s in all_stats)
        total_skipped = sum(s["skipped_existing"] for s in all_stats)
        total_perm = sum(s["skipped_perm"] for s in all_stats)
        total_iac = sum(s["iac"] for s in all_stats)

        print(f"{'Org':<30} {'Repos':>6} {'Created':>8} {'Failed':>8} {'Archived':>9} {'Skipped':>8} {'PermSkip':>9} {'IaC':>5}")
        print("-" * 90)
        for s in all_stats:
            print(f"{s['org']:<30} {s['total_repos']:>6} {s['created']:>8} {s['failed']:>8} {s['archived']:>9} {s['skipped_existing']:>8} {s['skipped_perm']:>9} {s['iac']:>5}")
        print("-" * 90)
        print(f"{'TOTAL':<30} {total_repos:>6} {total_created:>8} {total_failed:>8} {total_archived:>9} {total_skipped:>8} {total_perm:>9} {total_iac:>5}")

    print(f"\nCSV Output: {OUTPUT_FILE}")
    print(f"{'=' * 64}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create or close Veracode baseline scan trigger issues across one or more GitHub orgs. "
            "Supports GitHub.com, GitHub Enterprise Cloud (GHEC), and GitHub Enterprise Server (GHES)."
        )
    )

    org_group = parser.add_mutually_exclusive_group(required=True)
    org_group.add_argument("org", nargs="?", help="Single GitHub organization name")
    org_group.add_argument("--org-file", metavar="FILE",
                           help="Path to a text file with one org name per line (# lines are comments)")

    parser.add_argument("--delete", action="store_true", help="Close previously created trigger issues")
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
    parser.add_argument("--repo-limit", type=int, default=DEFAULT_REPO_LIST_LIMIT,
                        help=f"Max repos to fetch per org (default: {DEFAULT_REPO_LIST_LIMIT})")
    parser.add_argument("--min-remaining", type=int, default=DEFAULT_RL_MIN_REMAINING,
                        help=f"Pause when core API remaining <= N (default: {DEFAULT_RL_MIN_REMAINING})")
    parser.add_argument("--rl-check-every", type=int, default=DEFAULT_RL_CHECK_EVERY,
                        help=f"Check rate limit every N gh calls (default: {DEFAULT_RL_CHECK_EVERY})")
    args = parser.parse_args()

    # Validate gh CLI is available
    if shutil.which("gh") is None:
        print("Error: GitHub CLI (gh) is not installed.", file=sys.stderr)
        sys.exit(1)

    gh_hostname = args.hostname

    # Warn if targeting GHES without GH_ENTERPRISE_TOKEN set
    if gh_hostname and "ghe.com" not in gh_hostname and not os.environ.get("GH_ENTERPRISE_TOKEN"):
        print(
            f"Warning: --hostname is set to '{gh_hostname}' but GH_ENTERPRISE_TOKEN is not set. "
            "Authentication may fail for GHES. Set GH_ENTERPRISE_TOKEN before running.",
            file=sys.stderr,
        )

    if gh_hostname:
        print(f"Targeting GitHub host: {gh_hostname}")

    # Resolve org list
    if args.org_file:
        orgs = load_orgs_from_file(args.org_file)
        print(f"Loaded {len(orgs)} org(s) from '{args.org_file}'.")
    else:
        orgs = [args.org]

    total_orgs = len(orgs)
    multi_org_mode = total_orgs > 1

    # Pre-flight: validate access to all orgs before starting work
    print("\nChecking access to all organizations...")
    inaccessible_orgs = []
    for org in orgs:
        print(f"  Checking {org}...", end=" ")
        if check_org_access(org, args.min_remaining, args.rl_check_every, gh_hostname):
            print("OK")
        else:
            print("FAILED")
            inaccessible_orgs.append(org)

    if inaccessible_orgs:
        print(f"\nError: Cannot access the following org(s): {', '.join(inaccessible_orgs)}", file=sys.stderr)
        print("Aborting. Fix access or remove them from the org file and retry.", file=sys.stderr)
        sys.exit(1)

    print()

    if args.delete:
        fieldnames = ["org", "repo", "primary_language", "is_archived", "issues_deleted", "action"]
    else:
        fieldnames = ["org", "repo", "primary_language", "issues_enabled", "is_archived", "action"]

    all_stats = []

    with open(OUTPUT_FILE, "w", newline="") as csv_file:
        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        csv_writer.writeheader()

        for org_index, org in enumerate(orgs, start=1):
            print_org_header(org, org_index, total_orgs)

            repos = fetch_repos(org, args.repo_limit, args.min_remaining, args.rl_check_every, gh_hostname)
            if not repos:
                continue

            if args.delete:
                stats = run_delete_mode(org, repos, csv_writer, args.min_remaining, args.rl_check_every, gh_hostname)
            else:
                stats = run_create_mode(org, repos, csv_writer, args.min_remaining, args.rl_check_every, gh_hostname)

            all_stats.append(stats)

    if multi_org_mode and all_stats:
        print_multi_org_summary(all_stats, args.delete)

    print(f"\nCSV Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
