#!/usr/bin/env python3
"""
vcbaseline.py - Create or close Veracode baseline scan trigger issues across all repos in a GitHub org.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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


def query_rate_limit() -> bool:
    """Query GitHub core REST rate limit. Updates global state. Returns True on success."""
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit", "--jq", ".resources.core.remaining, .resources.core.reset"],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return False
        rate_limit_state.remaining = int(lines[0])
        rate_limit_state.reset_epoch = int(lines[1])
        return True
    except (subprocess.CalledProcessError, ValueError):
        return False


def maybe_pause_for_rate_limit(min_remaining: int, check_every: int) -> None:
    """Proactively pause if remaining API calls are low."""
    rate_limit_state.call_count += 1

    if rate_limit_state.call_count % check_every != 0:
        return

    if not query_rate_limit():
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


def gh_call(args: list[str], min_remaining: int, check_every: int) -> tuple[bool, str, str]:
    """
    Run a gh CLI command with proactive and reactive rate limit handling.
    Returns (success, stdout, stderr).
    Retries once after sleeping if rate limited.
    """
    maybe_pause_for_rate_limit(min_remaining, check_every)

    def run_command() -> tuple[bool, str, str]:
        result = subprocess.run(args, capture_output=True, text=True)
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
        if query_rate_limit():
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


def fetch_repos(org: str, limit: int, min_remaining: int, check_every: int) -> list[dict]:
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
        min_remaining,
        check_every,
    )
    if not success:
        print(f"Error: Failed to fetch repos for org '{org}'.", file=sys.stderr)
        sys.exit(1)
    return json.loads(stdout.strip()) if stdout.strip() else []


def find_open_issues(repo: str, title: str, min_remaining: int, check_every: int) -> list[int]:
    """Return list of open issue numbers matching the given title."""
    success, stdout, _ = gh_call(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         "--search", f"{title} in:title", "--json", "number", "--jq", "[.[].number]"],
        min_remaining,
        check_every,
    )
    if not success or not stdout.strip():
        return []
    return json.loads(stdout.strip())


def run_delete_mode(
    org: str,
    repos: list[dict],
    csv_writer: csv.DictWriter,
    min_remaining: int,
    check_every: int,
) -> None:
    """Close all open trigger issues across org repos."""
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
                "repo": name, "primary_language": primary_lang,
                "is_archived": is_archived, "issues_deleted": 0, "action": "skipped_archived",
            })
            continue

        issue_numbers = find_open_issues(name, ISSUE_TITLE, min_remaining, check_every)

        if not issue_numbers:
            print("No matching issues found.")
            skipped_no_issues += 1
            csv_writer.writerow({
                "repo": name, "primary_language": primary_lang,
                "is_archived": is_archived, "issues_deleted": 0, "action": "no_issues_found",
            })
            continue

        issues_deleted = issues_failed = 0

        for issue_num in issue_numbers:
            print(f"Closing issue #{issue_num}...")
            success, _, _ = gh_call(
                ["gh", "issue", "close", str(issue_num), "--repo", name,
                 "--comment", "Closed by cleanup script"],
                min_remaining,
                check_every,
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
            "repo": name, "primary_language": primary_lang,
            "is_archived": is_archived, "issues_deleted": issues_deleted, "action": action,
        })

    print("\nFinished closing issues.\n")
    print(f"Delete Stats for Organization: {org}")
    print("----------------------------------------------------------------")
    print(f"Total Repositories:            {total_repos}")
    print(f"Archived Repositories:         {archived_count}")
    print(f"Repositories with No Issues:   {skipped_no_issues}")
    print(f"Issues Closed:                 {deleted_count}")
    print(f"Failed Closes:                 {failed_delete_count}")
    print(f"CSV Output:                    {OUTPUT_FILE}")
    print("----------------------------------------------------------------")


def run_create_mode(
    org: str,
    repos: list[dict],
    csv_writer: csv.DictWriter,
    min_remaining: int,
    check_every: int,
) -> None:
    """Create baseline scan trigger issues across org repos."""
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
                "repo": name, "primary_language": primary_lang,
                "issues_enabled": issues_enabled, "is_archived": is_archived, "action": "skipped_archived",
            })
            continue

        was_disabled = False
        if not issues_enabled:
            print("Issues are disabled. Temporarily enabling...")
            success, _, _ = gh_call(
                ["gh", "repo", "edit", name, "--enable-issues"],
                min_remaining, check_every,
            )
            if not success:
                print("Could not enable issues. Skipping issue creation.")
                skipped_issues_perm_count += 1
                csv_writer.writerow({
                    "repo": name, "primary_language": primary_lang,
                    "issues_enabled": issues_enabled, "is_archived": is_archived,
                    "action": "skipped_cant_enable_issues",
                })
                continue
            was_disabled = True

        existing_issues = find_open_issues(name, ISSUE_TITLE, min_remaining, check_every)

        if existing_issues:
            print("Open issue with same title already exists. Skipping.")
            skipped_existing_count += 1
            if was_disabled:
                print("Restoring state: Disabling issues...")
                gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], min_remaining, check_every)
            csv_writer.writerow({
                "repo": name, "primary_language": primary_lang,
                "issues_enabled": issues_enabled, "is_archived": is_archived,
                "action": "skipped_existing_issue",
            })
            continue

        print("Creating issue...")
        success, _, _ = gh_call(
            ["gh", "issue", "create", "--repo", name, "--title", ISSUE_TITLE, "--body", ISSUE_BODY],
            min_remaining, check_every,
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
            gh_call(["gh", "repo", "edit", name, "--enable-issues=false"], min_remaining, check_every)

        csv_writer.writerow({
            "repo": name, "primary_language": primary_lang,
            "issues_enabled": issues_enabled, "is_archived": is_archived, "action": action,
        })

    print("\nFinished processing all repositories.\n")
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
    print(f"CSV Output:               {OUTPUT_FILE}")
    print("----------------------------------------------------------------")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create or close Veracode baseline scan trigger issues across a GitHub org."
    )
    parser.add_argument("org", help="GitHub organization name")
    parser.add_argument("--delete", action="store_true", help="Close previously created trigger issues")
    parser.add_argument("--repo-limit", type=int, default=DEFAULT_REPO_LIST_LIMIT,
                        help=f"Max repos to fetch (default: {DEFAULT_REPO_LIST_LIMIT})")
    parser.add_argument("--min-remaining", type=int, default=DEFAULT_RL_MIN_REMAINING,
                        help=f"Pause when core API remaining <= N (default: {DEFAULT_RL_MIN_REMAINING})")
    parser.add_argument("--rl-check-every", type=int, default=DEFAULT_RL_CHECK_EVERY,
                        help=f"Check rate limit every N gh calls (default: {DEFAULT_RL_CHECK_EVERY})")
    args = parser.parse_args()

    # Validate gh CLI is available
    if subprocess.run(["which", "gh"], capture_output=True).returncode != 0:
        print("Error: GitHub CLI (gh) is not installed.", file=sys.stderr)
        sys.exit(1)

    # Validate org access
    print(f"Checking access to organization: {args.org}...")
    success, _, _ = gh_call(
        ["gh", "api", f"orgs/{args.org}", "--silent"],
        args.min_remaining, args.rl_check_every,
    )
    if not success:
        print(f"Error: You do not have access to '{args.org}' or it does not exist.", file=sys.stderr)
        sys.exit(1)

    repos = fetch_repos(args.org, args.repo_limit, args.min_remaining, args.rl_check_every)

    with open(OUTPUT_FILE, "w", newline="") as csv_file:
        if args.delete:
            fieldnames = ["repo", "primary_language", "is_archived", "issues_deleted", "action"]
        else:
            fieldnames = ["repo", "primary_language", "issues_enabled", "is_archived", "action"]

        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        csv_writer.writeheader()

        if args.delete:
            run_delete_mode(args.org, repos, csv_writer, args.min_remaining, args.rl_check_every)
        else:
            run_create_mode(args.org, repos, csv_writer, args.min_remaining, args.rl_check_every)


if __name__ == "__main__":
    main()
