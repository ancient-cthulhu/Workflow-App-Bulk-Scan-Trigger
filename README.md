# Veracode Workflow App - Issue Scan Trigger Script

Creates or closes GitHub issues across repositories in one or more GitHub organizations to trigger Veracode scans via the Veracode Workflow App. Handles archived repos, temporarily disabled Issues, duplicate prevention, rate limiting, stale scan detection, repo filtering, Veracode profile existence check, and multi-org runs with a CSV audit trail.

-----

## How It Works

For each repository in the target organization, the script:

1. Skips archived repositories
1. Optionally filters repos by name list (`--repo-file`) or wildcard pattern (`--repo-wildcard`)
1. Optionally skips repos that already have a Veracode application profile on the platform (`--veracode-skip-existing`)
1. Optionally checks for recent Veracode scans and skips repos scanned within N days (`--stale-days`)
1. Temporarily enables Issues if disabled (restores original state after)
1. Checks for an existing open trigger issue to avoid duplicates
1. Creates the trigger issue
1. Writes a per-repo result row to the CSV audit trail
1. Proactively checks GitHub API rate limits and sleeps until reset if limits are low

> **Scans are not triggered directly.** The issue acts as a signal to the Veracode Workflow App, which initiates scans based on the `veracode.yml` configuration in each repository. If `issues.trigger` is not set to `true` in `veracode.yml`, no scan will start.

-----

## Quickstart

### Create trigger issues across one org

```bash
python script.py my-github-org
```

### Create trigger issues across multiple orgs

```bash
python script.py --org-file orgs.txt
```

### Target specific repos by name

```bash
python script.py --repo-file repos.txt my-github-org
```

### Target repos matching a wildcard pattern

```bash
python script.py --repo-wildcard "api-*" my-github-org
python script.py --repo-wildcard "*-service" my-github-org
python script.py --repo-wildcard "*core*" my-github-org
```

### Only trigger repos not scanned in the last 30 days

```bash
python script.py --stale-days 30 my-github-org
python script.py --stale-days 30 --org-file orgs.txt
```

### Skip repos that already have a Veracode application profile

```bash
python script.py --veracode-skip-existing my-github-org
python script.py --veracode-skip-existing --veracode-region eu --org-file orgs.txt
```

### Clean up trigger issues

```bash
python script.py --delete my-github-org
python script.py --delete --org-file orgs.txt
```

-----

## Requirements

```bash
gh --version    # GitHub CLI v2+
python --version  # Python 3.10+
```

Stdlib only by default. `--veracode-skip-existing` additionally requires:

```bash
pip install veracode-api-signing requests
```

-----

## Credentials

### GitHub.com and GHEC

```bash
gh auth login
# or
export GH_TOKEN=ghp_yourtoken
```

### GitHub Enterprise Server (GHES)

```bash
gh auth login --hostname github.mycompany.com
# or
export GH_ENTERPRISE_TOKEN=ghp_yourtoken
```

When using `--hostname`, the script injects `GH_HOST` into every `gh` call automatically, no need to export it in your shell. The script warns if `--hostname` points to a GHES instance but `GH_ENTERPRISE_TOKEN` is not set.

### Veracode (only required with `--veracode-skip-existing`)

Set either environment variables:

```bash
export VERACODE_API_KEY_ID=your_api_id
export VERACODE_API_KEY_SECRET=your_api_secret
```

Or a credentials file at `~/.veracode/credentials`:

```ini
[default]
veracode_api_key_id = your_api_id
veracode_api_key_secret = your_api_secret
```

The API account needs read access to the Applications API (Security Lead, Reviewer, or Creator role).

### Required Permissions

|Operation                            |Required                                             |
|-------------------------------------|-----------------------------------------------------|
|Create / delete issues               |Read access + permission to create issues            |
|Enable Issues on repos where disabled|Admin permission on those repos                      |
|Query Veracode profiles              |Veracode API account with read access to Applications|

If Issues cannot be enabled on a repository, the script skips it and records the reason in the CSV.

-----

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

-----

## Command-Line Reference

|Flag                                       |Description                                                                                                                                                                                                              |
|-------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|`org`                                      |Single org name (positional argument)                                                                                                                                                                                    |
|`--org-file FILE`                          |Path to a text file with one org per line. `#` lines and blank lines are ignored. Mutually exclusive with the positional org argument.                                                                                   |
|`--repo-file FILE`                         |Path to a text file with one repo name per line (without org prefix). `#` lines and blank lines are ignored. Mutually exclusive with `--repo-wildcard`.                                                                  |
|`--repo-wildcard PATTERN`                  |Filter repos using wildcard pattern. Case-insensitive. Mutually exclusive with `--repo-file`. See [Repo Filtering](#repo-filtering) for pattern syntax.                                                                  |
|`--delete`                                 |Close previously created trigger issues instead of creating new ones                                                                                                                                                     |
|`--stale-days N`                           |Only create issues for repos not scanned in the last N days. Checks for Veracode check runs (SAST, SCA, IaC). Disabled by default. Set to 0 to disable. Ignored in delete mode.                                          |
|`--veracode-skip-existing`                 |Query the Veracode platform once at startup and skip repos that already have an application profile named `<org>/<repo>` (case-insensitive). Ignored in delete mode. See [Veracode Profile Skip](#veracode-profile-skip).|
|`--veracode-region {commercial,eu,federal}`|Veracode region for the API base URL (default: `commercial`).                                                                                                                                                            |
|`--hostname HOSTNAME`                      |GitHub hostname. Omit for github.com. GHES: `github.mycompany.com`. GHEC: `myorg.ghe.com`. Sets `GH_HOST` per-call without modifying your shell.                                                                         |
|`--output FILE`                            |CSV output path (default: `vcbaseline.csv`)                                                                                                                                                                              |
|`--repo-limit N`                           |Max repos to fetch per org (default: `1000`)                                                                                                                                                                             |
|`--min-remaining N`                        |Pause when Core API remaining <= N (default: `100`)                                                                                                                                                                      |
|`--rl-check-every N`                       |Check rate limit every N gh calls (default: `50`)                                                                                                                                                                        |

Environment variables `GH_RL_MIN_REMAINING`, `GH_RL_CHECK_EVERY`, and `REPO_LIST_LIMIT` set the same defaults as their flag equivalents. Flags take precedence.

-----

## Repo Filtering

Use `--repo-file` or `--repo-wildcard` to target a subset of repositories within an org. Both options work with single-org and multi-org (`--org-file`) modes.

### Filter by Name List (`--repo-file`)

Create a text file with one repo name per line (without the org prefix):

```text
# repos.txt
# Target these specific repos
my-api
frontend-app
shared-lib
internal-tools
```

```bash
python script.py --repo-file repos.txt my-github-org
python script.py --repo-file repos.txt --org-file orgs.txt
```

Matching is case-insensitive. Lines starting with `#` and blank lines are ignored.

### Filter by Wildcard Pattern (`--repo-wildcard`)

Use glob-style patterns to match repo names:

|Pattern    |Matches                                                                          |
|-----------|---------------------------------------------------------------------------------|
|`example*` |Repos starting with “example” (e.g., `example-api`, `example-frontend`)          |
|`*example` |Repos ending with “example” (e.g., `my-example`, `test-example`)                 |
|`*example*`|Repos containing “example” (e.g., `my-example-api`, `example`, `test-example-v2`)|
|`ex?mple`  |Single character wildcard (e.g., `example`, `ex1mple`, `ex-mple`)                |
|`api-v[12]`|Character set (e.g., `api-v1`, `api-v2`)                                         |

```bash
# All repos starting with "api-"
python script.py --repo-wildcard "api-*" my-github-org

# All repos ending with "-service"
python script.py --repo-wildcard "*-service" my-github-org

# All repos containing "core"
python script.py --repo-wildcard "*core*" my-github-org

# Combined with multi-org
python script.py --repo-wildcard "*-backend" --org-file orgs.txt
```

Matching is case-insensitive. The pattern applies to the repo name only (not the org prefix).

### Combining with Other Filters

Repo filters can be combined with `--stale-days` and `--veracode-skip-existing`:

```bash
# Only api-* repos not scanned in 30 days, skipping those with existing VC profiles
python script.py --repo-wildcard "api-*" --stale-days 30 --veracode-skip-existing my-github-org
```

Repo filters also work with `--delete`:

```bash
# Close issues only on specific repos
python script.py --delete --repo-file repos.txt my-github-org
```

-----

## Veracode Profile Skip

Use `--veracode-skip-existing` to query the Veracode platform once at startup and skip any repo that already has a matching application profile. This is useful when the Workflow App auto-creates profiles named `<org>/<repo>` and you only want to trigger scans for repos without an existing profile.

```bash
# Default (US Commercial region)
python script.py --veracode-skip-existing my-github-org

# EU region
python script.py --veracode-skip-existing --veracode-region eu my-github-org

# US Federal region
python script.py --veracode-skip-existing --veracode-region federal --org-file orgs.txt
```

The script fetches all application profiles paginated from `appsec/v1/applications`, builds an in-memory set of lowercased profile names, then for each repo checks whether `<org>/<repo>` (lowercase) is present. The check runs before any GitHub issue mutation, so repos with existing profiles are recorded in the CSV as `skipped_veracode_profile_exists` without toggling Issues on.

**Fail-safe behavior:** If the Veracode API is unreachable or returns an unrecoverable error after retries, the script aborts before making any GitHub changes. This prevents accidentally triggering scans for repos that may already have profiles.

> **API cost note:** One Veracode API call per 500 profiles at startup. The lookup itself is in-memory and adds no extra API cost per repo.

-----

## Stale Scan Detection

Use `--stale-days N` to only create trigger issues for repositories that have not had a Veracode scan in the last N days. This is useful for periodic re-scans or ensuring coverage across large orgs.

The script queries GitHub check runs on each repo and looks for checks starting with “Veracode” (matching SAST, SCA, and IaC scans). If the most recent check is within the threshold, the repo is skipped.

```bash
# Trigger scans only for repos not scanned in 30 days
python script.py --stale-days 30 my-github-org

# 14-day threshold
python script.py --stale-days 14 --org-file orgs.txt

# Disable stale check (default behavior)
python script.py --stale-days 0 my-github-org
```

Repos with no Veracode check history are treated as stale and will have issues created.

> **API cost note:** When `--stale-days` is set, the script makes one additional API call per repo to query Veracode check runs on the default branch. The default branch is fetched alongside the initial repo list, so no extra round-trip is needed to discover it.

-----

## Rate Limit Handling

The script handles three failure modes automatically:

- **Core REST limits**: sleeps until the rate-limit reset epoch and retries.
- **Auth failures** (bad credentials, 404, insufficient permissions): not retried; reported immediately and recorded in the CSV.
- **Veracode 429 / 5xx**: retried with backoff during profile fetch at startup; aborts the run after retries are exhausted.

Between content-creation calls (issue create / close), a 1 second throttle is applied to reduce secondary rate limit risk on bursty operations.

To reduce risk further on large runs, tighten the proactive thresholds:

```bash
python script.py --min-remaining 200 --rl-check-every 25 --org-file orgs.txt
```

Rate limit pauses are logged to stderr so the CSV output stays clean.

-----

## Platform Notes

### GitHub Enterprise Server (GHES)

```bash
export GH_ENTERPRISE_TOKEN=ghp_yourtoken

python script.py --hostname github.mycompany.com my-org
python script.py --hostname github.mycompany.com --org-file orgs.txt
```

### GitHub Enterprise Cloud (GHEC)

GHEC uses `GH_TOKEN` (same as GitHub.com). Use `--hostname` with your GHEC subdomain if needed:

```bash
export GH_TOKEN=ghp_yourtoken

python script.py --hostname myorg.ghe.com my-org
```

For most GHEC setups authenticated via `gh auth login`, `--hostname` is not required.

-----

## Large Deployments

Before processing begins, the script validates access to all orgs and aborts if any are inaccessible - no partial runs from a typo mid-way through.

For orgs with more than 1,000 repos, raise `--repo-limit`:

```bash
python script.py --repo-limit 5000 my-github-org
```

For very large orgs, tighten rate limit controls:

```bash
python script.py --repo-limit 10000 --min-remaining 200 --rl-check-every 25 --org-file orgs.txt
```

-----

## Output

`vcbaseline.csv` is written to the working directory after every run (override with `--output`). One row per repository. In multi-org runs all orgs land in the same file, filter by the `org` column to isolate results per organization.

#### Create Mode

|Field                    |Description                                                                                 |
|-------------------------|--------------------------------------------------------------------------------------------|
|`org`                    |GitHub organization name                                                                    |
|`repo`                   |Full `org/repo` name                                                                        |
|`primary_language`       |Repository primary language, or `N/A`                                                       |
|`issues_enabled`         |Whether Issues were enabled before the run                                                  |
|`is_archived`            |Whether the repository is archived                                                          |
|`veracode_profile_exists`|`true` / `false` if `--veracode-skip-existing` is used, empty otherwise                     |
|`last_check_date`        |Timestamp of most recent Veracode check run (empty if none found or `--stale-days` not used)|
|`days_since_check`       |Days since the last Veracode check (empty if none found or `--stale-days` not used)         |
|`action`                 |Outcome - see values below                                                                  |

|`action` value                   |Meaning                                                                                                                                                                          |
|---------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|`created`                        |Issue created successfully                                                                                                                                                       |
|`created_restore_failed`         |Issue created, but failed to restore the original issues-disabled state on the repo. The repo now has Issues enabled when it previously did not - manual cleanup may be required.|
|`skipped_veracode_profile_exists`|Application profile already exists on the Veracode platform (only when `--veracode-skip-existing` is used)                                                                       |
|`skipped_recent_check`           |Veracode check found within `--stale-days` threshold                                                                                                                             |
|`skipped_existing_issue`         |Open trigger issue already exists                                                                                                                                                |
|`skipped_archived`               |Repository is archived, skipped                                                                                                                                                  |
|`skipped_cant_enable_issues`     |Issues could not be enabled (insufficient permissions)                                                                                                                           |
|`failed_create`                  |Issue creation API call failed                                                                                                                                                   |
|`failed_create_restore_failed`   |Issue creation failed AND failed to restore the original issues-disabled state. Manual cleanup may be required.                                                                  |
|`failed_query_issues`            |Could not check for existing issues; creation skipped to prevent duplicates                                                                                                      |

#### Delete Mode

|Field             |Description                          |
|------------------|-------------------------------------|
|`org`             |GitHub organization name             |
|`repo`            |Full `org/repo` name                 |
|`primary_language`|Repository primary language, or `N/A`|
|`is_archived`     |Whether the repository is archived   |
|`issues_deleted`  |Number of issues closed              |
|`action`          |Outcome - see values below           |

|`action` value       |Meaning                                   |
|---------------------|------------------------------------------|
|`deleted`            |All matching issues closed                |
|`partial_delete`     |Some issues closed, some failed           |
|`no_issues_found`    |No open trigger issues found              |
|`skipped_archived`   |Repository is archived, skipped           |
|`failed_query_issues`|Could not query issues; repository skipped|

-----

## Duplicate Protection

Before creating an issue, the script checks for an existing open issue with the same title using the GitHub REST issues endpoint with client-side title matching. If the check fails (API error), creation is skipped and recorded as `failed_query_issues` this prevents accidental duplicates when the existing-issue check cannot be completed.

To re-trigger scans:

- Use `--delete` to close existing issues, then run again in create mode
- Close the existing issue manually
- Update `ISSUE_TITLE` in the script

-----

## Troubleshooting

- **“API rate limit exceeded / HTTP 403 / secondary rate limit / submitted too quickly”**
  - Handled automatically. See [Rate Limit Handling](#rate-limit-handling) for the exact retry behavior. To reduce risk on large runs:
    
    ```bash
    python script.py --min-remaining 200 --rl-check-every 25 --org-file orgs.txt
    ```
- **“Cannot access the following org(s)” on startup**
  - Check for typos in the org name or file, or confirm your token has access to all listed orgs. No changes are made until access is confirmed for all orgs.
- **“No org names found” when using –org-file**
  - Every line is blank or starts with `#`. Add at least one uncommented org name.
- **“No repo names found” when using –repo-file**
  - Every line is blank or starts with `#`. Add at least one uncommented repo name.
- **“Invalid org name(s)” when using –org-file or a positional org**
  - Org names must start and end with a letter or digit, contain only alphanumeric characters and hyphens between them, and be at most 39 characters.
- **“Invalid repo name(s)” when using –repo-file**
  - Repo names must contain only alphanumeric characters, hyphens, underscores, and periods.
- **“No repos match filter” after applying –repo-file or –repo-wildcard**
  - Verify repo names/patterns are correct. Matching is case-insensitive. Check that the repos exist in the target org.
- **“Could not enable issues” on some repositories**
  - The token lacks admin permissions on those repos. The script skips them and records `skipped_cant_enable_issues` in the CSV.
- **`created_restore_failed` or `failed_create_restore_failed` rows in the CSV**
  - The script temporarily enabled Issues on a repo, then could not re-disable them afterward. The repo now has Issues enabled when it previously did not. Filter the CSV for these actions and manually disable Issues if needed.
- **“Veracode profile fetch failed” on startup**
  - Confirm `VERACODE_API_KEY_ID`/`VERACODE_API_KEY_SECRET` are set or `~/.veracode/credentials` exists, the API account has read access to the Applications API, and `--veracode-region` matches your account’s region. Install dependencies with `pip install veracode-api-signing requests`.
- **“SyntaxError” or unexpected failures**
  - Requires Python 3.10 or newer. Check with `python --version`.
- **GHES: “authentication failed” or “Could not resolve host”**
  - Ensure `GH_ENTERPRISE_TOKEN` is set, `gh auth login --hostname your.ghes.host` has been run at least once, and the host is reachable from your machine.
- **GHEC: targeting the wrong GitHub instance**
  - Use `--hostname myorg.ghe.com` to force the correct host. GHEC uses `GH_TOKEN`, not `GH_ENTERPRISE_TOKEN`.
