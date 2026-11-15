# Veracode Workflow App - Issue Scan Trigger Script

Creates or closes GitHub issues across repositories in one or more GitHub organizations to trigger Veracode scans via the Veracode Workflow App. Handles archived repos, temporarily disabled Issues, duplicate prevention, rate limiting, scan pacing, stale scan detection, repo filtering, Veracode profile existence check, parallel processing, and multi-org runs with a CSV audit trail.

---

## How It Works

For each repository in the target organization, the script:

1. Skips archived repositories
2. Optionally filters repos by org/repo combination (`--org-repo-file`), name list (`--repo-file`), or wildcard pattern (`--repo-wildcard`)
3. Optionally skips repos that already have a Veracode application profile on the platform (`--veracode-skip-existing`)
4. Optionally checks for recent Veracode scans and skips repos scanned within N days (`--stale-days`)
5. Temporarily enables Issues if disabled and restores the original state on every path, including errors and Ctrl-C. If an enable call errors (timeout, rate limit), the setting is read back and restored if it landed. A failed restore is recorded as a `*_restore_failed` action and counted in the summary. In delete mode, repos with Issues disabled are always toggled (GitHub's issue list returns an empty 200 on them, see [Delete Mode](#delete-mode))
6. Checks for an existing open trigger issue (direct read of the repo's open issues, not search). If one is open, the repo is skipped, unless `--rescan-existing` is set
7. Waits for a creation slot (`--max-inflight`, then `--create-interval` / `--batch-size`)
8. Re-checks for an open trigger issue before and inside the creation gate (the slot wait can be long)
9. If none exists, creates the trigger issue, then reads it back to confirm it exists. If one exists, skips the repo, or with `--rescan-existing` comments `Veracode All Scans` on it to re-trigger the scan
10. Writes a per-repo result row to the CSV audit trail
11. Proactively checks GitHub Core **and GraphQL** rate limits and sleeps until reset if either is low

Repos within an org are processed in parallel by a configurable thread pool (`--workers`). Each repo is one atomic work unit so the enable-issues / create-issue / restore-issues sequence stays consistent per repo while different repos run concurrently. Issue creation itself is serialized globally regardless of worker count.

> **Scans are not triggered directly.** The issue acts as a signal to the Veracode Workflow App, which initiates scans based on the `veracode.yml` configuration in each repository. If `issues.trigger` is not set to `true` in `veracode.yml`, no scan will start.

---

## Quickstart

Always dry run first.

```bash
# 1. See what would happen. No writes.
python script.py --dry-run --stale-days 30 --veracode-skip-existing my-github-org

# 2. Pilot on a slice, confirm scans complete end to end.
python script.py --repo-wildcard "api-*" --create-interval 30 --max-inflight 10 my-github-org

# 3. Bulk run, paced.
python script.py --org-file orgs.txt --stale-days 30 --veracode-skip-existing \
  --workers 5 --create-interval 25 --max-inflight 15 --batch-size 50 --batch-pause 1200
```

### Other common runs

```bash
# Single org
python script.py my-github-org

# Multiple orgs
python script.py --org-file orgs.txt

# Specific org/repo combinations
python script.py --org-repo-file org_repos.csv

# Specific repos
python script.py --repo-file repos.txt my-github-org

# Wildcard
python script.py --repo-wildcard "*-service" my-github-org

# Rescan: comment on existing open trigger issues instead of skipping them
python script.py --rescan-existing --stale-days 30 my-github-org

# Clean up
python script.py --delete --org-file orgs.txt
```

---

## Requirements

```bash
gh --version      # GitHub CLI v2+
python --version  # Python 3.9+
```

Stdlib only by default. `--veracode-skip-existing` additionally requires:

```bash
pip install veracode-api-signing requests
```

---

## Credentials

### GitHub.com and GHEC

```bash
gh auth login
# or
export GH_TOKEN=ghp_yourtoken
```

Use a token dedicated to this script. Do not reuse the Workflow App's token, and never use a workflow's `GITHUB_TOKEN`.

### GitHub Enterprise Server (GHES)

```bash
gh auth login --hostname github.mycompany.com
# or
export GH_ENTERPRISE_TOKEN=ghp_yourtoken
```

When using `--hostname`, the script injects `GH_HOST` into every `gh` call automatically. The script warns if `--hostname` points to a GHES instance but `GH_ENTERPRISE_TOKEN` is not set.

### Veracode (only required with `--veracode-skip-existing`)

```bash
export VERACODE_API_KEY_ID=your_api_id
export VERACODE_API_KEY_SECRET=your_api_secret
```

Or `~/.veracode/credentials`:

```ini
[default]
veracode_api_key_id = your_api_id
veracode_api_key_secret = your_api_secret
```

The API account needs read access to the Applications API (Security Lead, Reviewer, or Creator role).

Without `--veracode-region`, the region is read from the `VERACODE_API_KEY_ID` prefix (`vera01ei-...` = eu, `vera01fi-...` = federal, `vera01gi-...` or no prefix = commercial). An explicit `--veracode-region` that contradicts the key prints a warning. Keys read only from `~/.veracode/credentials` are not inspected, so pass `--veracode-region` for non-commercial accounts in that case.

### Required Permissions

Use a classic personal access token with these scopes:

| Scope | Why |
|-------|-----|
| `repo` | List repos, create and close issues, enable Issues, read check runs (`--stale-days`) and Actions runs in the central `veracode` repo (`--max-inflight`) |
| `read:org` | Required by the `gh` CLI |

The token owner also needs these repository roles:

| Operation | Minimum role |
|-----------|--------------|
| Create issues | Read |
| Close issues opened by another account (`--delete`) | Triage |
| Enable Issues on repos where disabled | Admin |

If the org uses SAML SSO, authorize the token for it: **Settings > Developer settings > Personal access tokens (classic) > Configure SSO**.

---

## Veracode Workflow App Configuration (Required)

Each target repository must have `issues.trigger: true` in `veracode.yml` and include `"Veracode All Scans"` as a command under each scan type:

```yaml
issues:
  trigger: true
  commands:
    - "Veracode **** Scan"
    - "Veracode All Scans"
```

The command value must exactly match the Workflow App command name. This mismatch is the most common reason scans do not trigger after an issue is created.

---

## Command-Line Reference

| Flag | Description |
|------|-------------|
| `org` | Single org name (positional argument) |
| `--org-file FILE` | Text file with one org per line. `#` and blank lines ignored. Duplicate orgs (case-insensitive) are processed once. Mutually exclusive with the positional org and `--org-repo-file`. |
| `--org-repo-file FILE` | CSV file with 2 columns: `org` and `repo`. Supports quoted values. Targets only the listed org/repo combinations. `#` and blank lines ignored. Mutually exclusive with the positional org, `--org-file`, `--repo-file` and `--repo-wildcard`. See [Org-Repo Filtering](#org-repo-filtering). |
| `--repo-file FILE` | Text file with one repo name per line (no org prefix). Entries may be glob patterns (`api-*`, `svc-v[12]`). Exact names are looked up directly (not limited by `--repo-limit`); a name that does not exist gets a `not_found` row. Mutually exclusive with `--repo-wildcard` and `--org-repo-file`. |
| `--repo-wildcard PATTERN` | Wildcard filter, case-insensitive. Mutually exclusive with `--repo-file` and `--org-repo-file`. |
| `--delete` | Close previously created trigger issues instead of creating new ones. See [Delete Mode](#delete-mode). |
| `--close-comment TEXT` | Delete mode: comment TEXT on each issue before closing it. Off by default (comments are content writes and slow the run to ~140/hour). |
| `--close-per-minute N` | Delete mode: max closes per minute, global (default: `40`, `0` = only the 1s mutation gap) |
| `--issue-author LOGIN` | Delete mode: only close trigger issues opened by LOGIN (server-side `creator` filter) |
| `--match-body` | Delete mode: also require the issue body to be exactly `Veracode All Scans`. Off by default because bodies may have been edited. |
| `--rescan-existing` | If an open trigger issue already exists, comment `Veracode All Scans` on it to re-trigger the scan instead of skipping the repo. A new issue is only created when none is open. Off by default. See [Duplicate Protection and Rescans](#duplicate-protection-and-rescans). |
| `--dry-run` | Report what would happen. Makes no writes of any kind. Works in create and delete mode. |
| `--stale-days N` | Only create issues for repos not scanned in the last N days. Disabled by default. `0` disables. Ignored in delete mode. |
| `--veracode-skip-existing` | Query the Veracode platform once at startup and skip repos that already have a profile named `<org>/<repo>` (case-insensitive). |
| `--veracode-region {commercial,eu,federal}` | Veracode region. Default: from the `VERACODE_API_KEY_ID` prefix, else `commercial` (see [Credentials](#veracode-only-required-with---veracode-skip-existing)) |
| `--workers N` | Parallel worker threads per org (default: `5`, max: `50`). Affects reads and skip paths only. Issue creation is always serial. |
| `--create-interval SECONDS` | **Minimum seconds between issue creations, global across all workers and orgs (default: `25`).** |
| `--batch-size N` | Pause after every N issues successfully created or commented (`0` = off) |
| `--batch-pause SECONDS` | Seconds to pause between batches so the Veracode queue drains (default: `900`) |
| `--max-inflight N` | Do not create a new issue while N scans are queued or running in the org's central repo (`0` = off). See [Pacing](#pacing-and-why-it-matters). |
| `--central-repo NAME` | Repo in each org where the Workflow App runs scans; `--max-inflight` counts its Actions runs (default: `veracode`) |
| `--hostname HOSTNAME` | GitHub hostname. Omit for github.com. GHES: `github.mycompany.com`. GHEC: `myorg.ghe.com`. |
| `--output FILE` | CSV output path (default: `vcbaseline-<create\|delete>[-dryrun]-<YYYYmmdd-HHMMSS>.csv`). Refuses to replace an existing file. |
| `--overwrite` | Allow `--output` to replace an existing file |
| `--resume CSV` | Continue a previous run: skip repos already done in CSV and append to it. See [Resuming](#resuming-after-a-failure). |
| `--repo-limit N` | Max repos to fetch per org (default: `1000`). The script warns if a result hits this number, as the list may be truncated. |
| `--min-remaining N` | Pause when Core or GraphQL remaining <= N (default: `100`) |
| `--rl-check-every N` | Query the rate limit every N gh calls (default: `50`) |
| `--strict-orgs` | Check access to every org before any work and abort if one is inaccessible. Default: record and skip it. |

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `GH_RL_MIN_REMAINING` | `100` | Same as `--min-remaining` |
| `GH_RL_CHECK_EVERY` | `50` | Same as `--rl-check-every` |
| `REPO_LIST_LIMIT` | `1000` | Same as `--repo-limit` |
| `GH_POINTS_PER_MIN` | `700` | Secondary-limit point budget (GitHub allows 900 REST / 2000 GraphQL) |
| `GH_CONTENT_PER_MIN` | `20` | Content writes per minute (GitHub allows 80) |
| `GH_CONTENT_PER_HOUR` | `140` | Content writes per hour (GitHub allows 500) |
| `GH_SEARCH_PER_MIN` | `25` | Search API calls per minute (GitHub allows 30). The existing-issue check no longer uses search, so this rarely applies. |

Flags take precedence over the env vars they share a default with. Flags that do nothing in the chosen mode (for example `--create-interval` with `--delete`, or `--close-comment` without it) produce a warning.

---

## Pacing and Why It Matters

Creating hundreds of issues at once queues hundreds of Actions jobs. Those jobs sit waiting on the Veracode scan queue and on your Actions concurrency entitlement, and the `GITHUB_TOKEN` inside each job expires when the job finishes or at its maximum lifetime: 6 hours on GitHub-hosted runners, up to 24 hours on self-hosted. Jobs that queue too long die with a 401 or get cancelled. That is a pacing problem on this side, not a Workflow App bug.

Three controls, use them together:

| Control | Protects against |
|---------|------------------|
| `--create-interval` | GitHub's undisclosed issue-creation limit (fails silently, see below) |
| `--batch-size` / `--batch-pause` | Bulk floods that outrun the Veracode queue |
| `--max-inflight` | Actions jobs aging out past the 6h job / token lifetime |

Sizing `--max-inflight`: set it to roughly your Veracode concurrent-scan capacity, and never above your Actions concurrent-job entitlement. If your median scan is 30 minutes and the cap is 15, you drain about 30 scans/hour, so `--create-interval 120` matches. If a scan can approach 6 hours, `--max-inflight` is not optional.

`--max-inflight` counts queued and in-progress Actions runs in the org's central repo (`--central-repo`, default `veracode`), where the Workflow App runs every scan, plus slots this run has taken whose run is not visible yet (up to 5 minutes; a run whose title contains `org/repo` is matched to its slot sooner). Each poll is two Actions API reads per org (60s interval). Slots are reserved atomically before the create and given back if nothing was written, so parallel workers cannot overshoot the cap. If the Actions API errors, the queue is treated as full (fail closed); after 30 minutes of errors, or 7 hours waiting on a full queue, that repo is recorded as `failed_inflight_wait`.

---

## Rate Limit Handling

GitHub's published limits: 900 points/min for REST, 2,000 points/min for GraphQL, 100 concurrent requests shared across both, 80 content-generating requests/min and 500/hour, and at least 1 second between mutative requests. Some endpoints, including issue creation, have lower undisclosed content limits.

The script handles these automatically:

- **Primary limits**: checks Core **and** GraphQL buckets (`gh repo list`, `gh issue create` and `gh repo edit` are GraphQL, so Core alone never fires) and sleeps until whichever is closer to empty resets. State is shared across all worker threads.
- **Secondary limits**: a shared throttle enforces the point, content/min, content/hour and search/min budgets, plus a 1 second minimum gap between mutations, across every worker. A secondary-limit hit pauses **all** workers, not just the one that hit it, with exponential backoff, retried up to 3 times, honouring `Retry-After` where present.
- **Silent failures**: GitHub returns HTTP 200 with an error body when the GraphQL primary limit is hit, and 200 or 403 on secondary limits, so exit code alone is not proof. For GraphQL calls (`gh repo`/`gh issue`) the script parses the body and treats a top-level `errors` (or bare `message`) as a failure, and as a rate limit only if that error says so. REST reads are not body-scanned (check-run output or run titles can mention rate limits harmlessly) and use `--jq` so only the needed fields come back. After every issue creation the script reads the issue back (up to 3 times) to confirm it exists. A create that reports success but produces no visible issue is recorded as `failed_silent_block`.
- **Non-idempotent writes are never blindly retried**: if an issue create or comment fails with a rate limit or timeout, the script backs off, then checks whether the write landed anyway. It only retries if it did not, so one repo cannot end up with several issues or several trigger comments. A create that fails any other way (5xx, crash, Ctrl-C) is also read back before being reported as failed.
- **Auth failures** (bad credentials, 404, insufficient permissions): not retried, reported immediately, recorded in the CSV.
- **Veracode 429 / 5xx**: retried with backoff at startup, honouring `Retry-After`. Aborts the run after retries are exhausted, and also aborts if the platform returns zero profiles.

Rate limit pauses are logged to stderr so the CSV output stays clean.

---

## Delete Mode

`--delete` closes open issues titled exactly `Veracode Baseline Scans` (pull requests excluded) with a REST `PATCH state=closed, state_reason=not_planned`.

- **Throughput.** A close is a mutation but not a content write, so it is not held to the 140/hour content budget. Closes are paced by `--close-per-minute` (default 40, about 2,400/hour) and the global 1 second gap between all mutations (Issues toggles included), so the hard ceiling is 60 mutations/minute.
- **Retries.** The PATCH is idempotent and is retried on rate limits. On a timeout the issue state is read back: closed counts as closed, still open is retried once, unknown is `failed_close`.
- **Comment.** No comment by default. `--close-comment TEXT` posts one comment per issue, sent once and never retried, charged to the content budget. A failed comment does not block the close.
- **Filters.** `--issue-author LOGIN` and `--match-body` narrow what gets closed.
- **Issues disabled.** On a repo with Issues disabled GitHub's issue *list* answers HTTP 200 with `[]` even when issues exist (only single-issue reads return 410), so an empty list proves nothing. Repos the org listing reports as Issues-disabled are therefore always enabled, listed, closed and restored, even when nothing turns out to match. Repos reported as enabled are listed first and only toggled if the list answers 410 (Issues switched off since the listing).

Sizing example, 2,000 orgs x 50 repos, one trigger issue each, 30% with Issues disabled: about 100k reads and 160k mutations (100k closes, 30k enables, 30k restores), no content writes. Every disabled repo costs an enable and a restore whether or not it has an issue to close. The 1 second mutation gap makes that roughly 45 hours; a token limited to 5,000 requests/hour is the tighter bound at about 52 hours (15,000/hour tokens stay at ~45). Charging closes as content writes (140/hour, as earlier versions did) would take about 30 days for the same run. `--close-comment` puts every close back on the content budget.

---

## Parallel Processing

`--workers` controls reads and skip paths. Issue creation is serialized by `--create-interval` regardless, so raising workers does not increase creation risk.

| Workers | Use case |
|---------|----------|
| `1` | Sequential, easiest to debug |
| `3-5` | Conservative default |
| `8-15` | Typical runs of a few hundred repos |
| `20-30` | Read-heavy runs where most repos hit a fast skip path (`--stale-days`, `--veracode-skip-existing`) |
| `30-50` | Very large orgs, mostly skips |

Per-repo log lines are buffered and printed as one contiguous block prefixed `[N/total] org/repo`, in completion order. Use `--workers 1` for strict sequential output.

Wall-clock time on a create-heavy run is set by `--create-interval`, not by `--workers`. At the default 25s that is about 140 issues/hour.

---

## Repo Filtering

### By name list (`--repo-file`)

```text
# repos.txt
my-api
frontend-app
shared-lib
```

```bash
python script.py --repo-file repos.txt my-github-org
```

Case-insensitive. `#` and blank lines ignored. Lines containing `*`, `?` or `[` are treated as glob patterns, the same as `--repo-wildcard`; the same applies to the repo column of `--org-repo-file`.

### By wildcard (`--repo-wildcard`)

| Pattern | Matches |
|---------|---------|
| `example*` | Starts with "example" |
| `*example` | Ends with "example" |
| `*example*` | Contains "example" |
| `ex?mple` | Single character wildcard |
| `api-v[12]` | Character set |

Case-insensitive, applied to the repo name only, not the org prefix.

Both filters combine with `--stale-days`, `--veracode-skip-existing`, `--workers` and `--delete`.

---

## Org-Repo Filtering

Use `--org-repo-file` to target specific org/repo combinations without maintaining a separate `--repo-file` list per org. Useful when you want to run across multiple orgs but only on a subset of repositories in each.

### Filter by org/repo combination (`--org-repo-file`)

Create a CSV file with 2 columns, `org` and `repo`:

```csv
# org_repos.csv
# Comments start with #
my-org,api-service
my-org,frontend-app
other-org,backend-api
other-org,shared-lib
third-org,core-lib
```

Quoted values are supported:

```csv
"my-org","api-service"
"my-org","frontend-app"
"other-org","backend-api"
```

```bash
python script.py --org-repo-file org_repos.csv
```

The org list is derived from the file, so no positional org or `--org-file` is needed (and neither is allowed alongside it). Matching is case-insensitive. Lines starting with `#` and blank lines are ignored. The file is read with Python's CSV reader with quote support, so values containing special characters can be quoted safely.

Exact repo names (here and in `--repo-file`) are fetched one by one (`GET /repos/{org}/{repo}`), so they are found however large the org is, and a name that does not exist is recorded as `not_found` (lookup errors as `failed_repo_lookup`); both count as failures. The org is only listed in full when the list contains glob entries. If every target in an org is missing and the org itself is unreachable, one `org_inaccessible` row is written instead.

Combines with `--stale-days`, `--veracode-skip-existing`, `--workers` and `--delete`:

```bash
# Only listed org/repo combinations not scanned in 30 days, 10 workers
python script.py --org-repo-file org_repos.csv --stale-days 30 --workers 10

# Skip repos with existing Veracode profiles
python script.py --org-repo-file org_repos.csv --veracode-skip-existing

# Close issues only on the listed org/repo combinations
python script.py --delete --org-repo-file org_repos.csv
```

---

## Veracode Profile Skip

`--veracode-skip-existing` fetches all application profiles from `appsec/v1/applications` once at startup, builds an in-memory set of lowercased names, then checks whether `<org>/<repo>` is present. The Workflow App always names profiles `<org>/<repo>`.

The check runs before any GitHub mutation, so repos with existing profiles are recorded as `skipped_veracode_profile_exists` without toggling Issues on.

**Fail-safe:** if the API is unreachable, errors after retries, or returns zero profiles, the script aborts before making any GitHub changes.

> **API cost:** one Veracode call per 500 profiles at startup, then in-memory. Pairs well with high `--workers`.

---

## Stale Scan Detection

`--stale-days N` queries check runs on each repo's default branch (paginated, filtered with `--jq` to runs whose name starts with "veracode", case-insensitive, matching SAST, SCA and IaC). If the most recent completed check is within the threshold, the repo is skipped. If the lookup fails (API error, no default branch, unexpected output) the repo is **not** triggered: it is recorded as `failed_stale_check` and counted as a failure.

```bash
python script.py --stale-days 30 my-github-org
```

> **Caveat:** check runs only exist for the **head commit of the default branch**. A repo that has been committed to since its last scan reports no checks and is treated as stale. Repos with no Veracode check history are also treated as stale.

> **API cost:** one additional API call per repo (more for repos with over 100 check runs on the head commit), parallelized under `--workers`.

---

## Large Deployments

Each org is processed independently. If an org's repo list fails, the script checks whether the org is reachable and records an org-level CSV row (empty `repo` column) with action `org_inaccessible` or `org_list_failed`, counts it, and moves on. An unexpected error inside one org is recorded as `org_crashed`. Pass `--strict-orgs` to restore the old behaviour: check every org up front and abort if any is inaccessible.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Every repo and org succeeded |
| `2` | At least one repo or org failed (enable, query, close, create, stale check, in-flight wait, target not found, crash, org access) or an Issues restore failed. Check the CSV. |
| `130` | Interrupted (Ctrl-C) |

### Interrupting (Ctrl-C)

The first Ctrl-C stops the run safely: repos not yet started are cancelled, running workers stop before their next write (enable, close, create, comment), restore any Issues setting they changed, and record an `aborted` row. Pacing waits wake up immediately. The CSV keeps a row for every repo touched, the summary is printed, and the exit code is `130`. Continue later with `--resume`. A second Ctrl-C aborts the drain.

### Large-run example

```bash
# Orgs over 1,000 repos
python script.py --repo-limit 5000 my-github-org

# Very large, paced bulk run
GH_POINTS_PER_MIN=500 GH_CONTENT_PER_MIN=15 GH_CONTENT_PER_HOUR=140 \
python script.py --org-file orgs.txt \
  --repo-limit 10000 --stale-days 30 --veracode-skip-existing \
  --workers 15 --min-remaining 500 --rl-check-every 25 \
  --create-interval 25 --max-inflight 15 --batch-size 50 --batch-pause 1200 \
  2>&1 | tee vcbaseline.log
```

Run under `tmux` or `nohup`. Bulk runs are long by design.

### Resuming after a failure

Every run writes a new timestamped CSV, so earlier audits are never overwritten. To continue an interrupted or partly failed run, pass its CSV:

```bash
python script.py --delete --org-file orgs.txt --resume vcbaseline-delete-20261115-090807.csv
```

Repos whose row has a terminal action (`deleted`, `no_issues_found`, `created`, `created_unverified`, `commented_existing`, `skipped_*`, including their `_restore_failed` variants) are skipped. Everything else (`failed_*`, `partial_delete`, `aborted`, `worker_crashed`, org-level rows) is retried, and new rows are appended to the same file. The CSV must come from the same mode. `--resume` cannot be combined with `--dry-run`. Fix `_restore_failed` rows by hand: a re-run does not retry the restore.

---

## Output

A new `vcbaseline-<mode>[-dryrun]-<timestamp>.csv` is written to the working directory for each run (override with `--output`; existing files are never replaced without `--overwrite`). One row per repository, all orgs in one file, filter by the `org` column. Org-level failures get a row with an empty `repo` column. Each row is flushed as soon as the repo finishes, so an interrupted run keeps everything done so far. Cells that start with `=`, `+`, `-`, `@`, tab or carriage return are prefixed with `'` so spreadsheets do not run them as formulas. Row order reflects completion order.

#### Create Mode

| Field | Description |
|-------|-------------|
| `org` | GitHub organization name |
| `repo` | Full `org/repo` name |
| `primary_language` | Repository primary language, or `N/A` |
| `issues_enabled` | Whether Issues were enabled before the run |
| `is_archived` | Whether the repository is archived |
| `veracode_profile_exists` | `true` / `false` with `--veracode-skip-existing`, empty otherwise |
| `last_check_date` | Timestamp of most recent Veracode check run |
| `days_since_check` | Days since the last Veracode check |
| `action` | Outcome, see below |

| `action` value | Meaning |
|----------------|---------|
| `created` | Issue created and verified present |
| `created_unverified` | Issue created but the read-back check itself errored. Verify manually. |
| `failed_silent_block` | Create returned success but no issue is visible after 3 read-backs. GitHub silently dropped it. Increase `--create-interval` and retry. A retry run re-checks first, so it will not duplicate an issue that did land. |
| `created_restore_failed` | Created, but the repo's original issues-disabled state could not be restored. Manual cleanup may be required. |
| `commented_existing` | `--rescan-existing` only. Commented on the existing trigger issue to re-trigger the scan. |
| `failed_comment` | `--rescan-existing` only. Comment on the existing trigger issue failed |
| `failed_comment_unknown` | `--rescan-existing` only. Comment errored and the follow-up check also failed. Not retried, to avoid a duplicate scan. Check the issue manually. |
| `dry_run_would_create` | `--dry-run` only. Would have created an issue. |
| `dry_run_would_comment` | `--dry-run --rescan-existing` only. Would have commented on the existing issue. |
| `dry_run_needs_issues_enabled` | `--dry-run` only. Would have enabled Issues first. |
| `skipped_veracode_profile_exists` | Profile already exists on the Veracode platform |
| `skipped_recent_check` | Veracode check found within `--stale-days` |
| `skipped_existing_issue` | Open trigger issue already exists (default, without `--rescan-existing`) |
| `skipped_archived` | Repository is archived |
| `skipped_cant_enable_issues` | Issues could not be enabled (insufficient permissions) |
| `failed_create` | Issue creation API call failed |
| `failed_create_unknown` | Create errored and the follow-up check also failed. Not retried, to avoid a duplicate. Check the repo manually. |
| `failed_create_restore_failed` | Creation failed AND the issues-disabled state could not be restored |
| `failed_query_issues` | Could not check for existing issues; creation skipped to prevent duplicates |
| `failed_stale_check` | `--stale-days` only. The last-scan lookup failed; the repo was not triggered. |
| `failed_inflight_wait` | `--max-inflight` only. Gave up waiting for an in-flight slot (Actions API errors for 30 min, or a full queue for 7 h). |

#### Delete Mode

| Field | Description |
|-------|-------------|
| `org` | GitHub organization name |
| `repo` | Full `org/repo` name |
| `primary_language` | Repository primary language, or `N/A` |
| `is_archived` | Whether the repository is archived |
| `veracode_profile_exists` | `true` / `false` with `--veracode-skip-existing`, empty otherwise |
| `issues_deleted` | Number of issues closed |
| `action` | Outcome, see below |

| `action` value | Meaning |
|----------------|---------|
| `deleted` | All matching issues closed |
| `partial_delete` | Some issues closed, some failed |
| `failed_close` | No matching issue could be closed |
| `dry_run_would_close` | `--dry-run` only. Would have closed the listed issues. |
| `no_issues_found` | No open trigger issues found |
| `skipped_archived` | Repository is archived |
| `skipped_veracode_profile_exists` | Profile exists on the platform, issues preserved |
| `failed_enable_issues` | Issues could not be enabled to reach the existing issue |
| `failed_query_issues` | Could not query issues; repository skipped |
| `dry_run_needs_issues_enabled` | `--dry-run` only. Issues are disabled; a real run would enable them, close any trigger issues, and restore. Their issues cannot be counted without enabling. |

Both modes can also record:

| `action` value | Meaning |
|----------------|---------|
| `<action>_restore_failed` | The original issues-disabled state could not be restored. Disable Issues manually. |
| `worker_crashed` | Unexpected error on this repo. Issues were restored; re-run it. |
| `aborted` | Run interrupted (Ctrl-C) before this repo's next write. Re-run or `--resume`. |
| `not_found` | `--repo-file` / `--org-repo-file` only. The listed repo does not exist or is not visible to the token. |
| `failed_repo_lookup` | `--repo-file` / `--org-repo-file` only. Looking up the listed repo failed (not a 404). |

---

## Duplicate Protection and Rescans

The script looks for an open issue titled `Veracode Baseline Scans` by reading the repo's open issues directly through the REST API (`GET /repos/{owner}/{repo}/issues?state=open`, paginated) and matching the title exactly. Pull requests returned by that endpoint are ignored.

Earlier versions used `gh issue list --search`, which goes through GitHub's search index. The index lags behind writes, so a just-created issue could be invisible to the check and to the read-back. That caused duplicates on re-runs and false `failed_silent_block` results that were then retried. The direct read does not depend on the index.

Other safeguards:

- **Re-check before writing.** Repos can wait a long time for a slot (`--batch-pause`, `--max-inflight`). The script checks again before queueing for the creation gate and once more inside it, right before creating, so an issue opened meanwhile is not duplicated and the repo gives its turn back.
- **No blind retries.** A create or comment that fails with a rate limit or timeout is only retried after confirming it did not land.
- **Lookup failures skip the repo.** If the check fails, nothing is written and the row is `failed_query_issues`.
- **Orgs are de-duplicated.** The same org listed twice in `--org-file` or `--org-repo-file` (any case) is processed once.
- **Existing duplicates are flagged.** If a repo already has more than one open trigger issue, a `[WARN]` line lists them. Clean them up with `--delete`.

### What happens when a trigger issue is already open

| Mode | Behaviour |
|------|-----------|
| Default | Skip the repo (`skipped_existing_issue`). Nothing is written. |
| `--rescan-existing` | Comment `Veracode All Scans` on the oldest open trigger issue (`commented_existing`). No new issue. |

Repos with no open trigger issue get a new issue in both modes.

The Workflow App starts a scan when a comment containing the `issues.commands` value is added to an issue, the same as on issue creation. Comments use the same `--create-interval`, `--batch-size` and `--max-inflight` pacing as creations.

With `--rescan-existing`, also pass `--stale-days` so repos with a scan running or recently completed are not commented on again.

Other ways to re-trigger:

- Use `--delete` to close existing issues, then run again in create mode
- Close the existing issue manually

---

## Troubleshooting

- **Scans fail with 401 / expired token, or jobs are cancelled**
  - Too many scans queued at once. The job's `GITHUB_TOKEN` dies with the job (6h hosted, up to 24h self-hosted). Add `--max-inflight` and raise `--create-interval`.

- **Issues created but no scan ever starts**
  - Check `issues.trigger: true` and the exact command string in `veracode.yml`. If the script ran inside Actions with `GITHUB_TOKEN`, the issue event never triggers a workflow. Use a PAT or App token.

- **`failed_silent_block` rows in the CSV**
  - GitHub's undisclosed issue-creation limit dropped the issue while returning success. Raise `--create-interval`, lower `GH_CONTENT_PER_MIN` / `GH_CONTENT_PER_HOUR`, and retry those repos with `--repo-file`.

- **"API rate limit exceeded / HTTP 403 / secondary rate limit / submitted too quickly"**
  - Handled automatically, all workers pause. If frequent, tighten:
    ```bash
    python script.py --workers 3 --min-remaining 200 --rl-check-every 25 --org-file orgs.txt
    ```

- **Run feels slow**
  - Create-heavy runs are paced by `--create-interval`, by design. Read-heavy runs (`--delete`, `--stale-days`, `--veracode-skip-existing`) scale with `--workers`.

- **A repo was scanned recently but `--stale-days` still triggered it**
  - Check runs only exist for the default branch head. A commit since the last scan clears them.

- **"WORKER CRASHED" in stderr**
  - Unhandled exception on one repo. It gets a `worker_crashed` CSV row and counts as failed; the run continues. Re-run it with `--resume`.

- **`org_inaccessible` / `org_list_failed` rows, or "Cannot access org(s)" with `--strict-orgs`**
  - Typo, missing SSO authorization, or the token lacks access. The rest of the run continues unless `--strict-orgs` is set.

- **"Expected 2 columns (org, repo)" when using --org-repo-file**
  - A line has the wrong number of comma-separated columns. Each line must have exactly 2.

- **"org and repo cannot be empty" when using --org-repo-file**
  - One column is empty or whitespace only. Both values must be non-empty.

- **"No org-repo pairs found" when using --org-repo-file**
  - Every line is blank or starts with `#`. Add at least one uncommented pair.

- **"Invalid org name(s)" / "Invalid repo name(s)"**
  - Org names: alphanumeric and hyphens, must start and end alphanumeric, max 39 chars. Repo names: alphanumeric, hyphen, underscore, period, plus glob characters.

- **`created_restore_failed` or `failed_create_restore_failed` rows**
  - The script enabled Issues on a repo and could not re-disable them. Filter the CSV for these and disable Issues manually.

- **"Veracode profile fetch failed" or "returned 0 application profiles"**
  - Check credentials, that the account has read access to the Applications API, and that `--veracode-region` matches your account. Zero profiles aborts on purpose: the skip would match nothing and every repo would be triggered.

- **"SyntaxError" or unexpected failures**
  - Requires Python 3.9 or newer.

- **GHES: "authentication failed" or "Could not resolve host"**
  - Ensure `GH_ENTERPRISE_TOKEN` is set, `gh auth login --hostname your.ghes.host` has been run, and the host is reachable.

- **GHEC: targeting the wrong instance**
  - Use `--hostname myorg.ghe.com`. GHEC uses `GH_TOKEN`, not `GH_ENTERPRISE_TOKEN`.
