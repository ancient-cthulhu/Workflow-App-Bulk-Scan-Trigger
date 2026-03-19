# Veracode Workflow App - Issue Scan Trigger Script

Creates or closes GitHub issues across repositories in one or more GitHub organizations to trigger Veracode scans via the Veracode Workflow App. Handles archived repos, temporarily disabled Issues, duplicate prevention, rate limiting, and multi-org runs with a CSV audit trail.

---

## How It Works

For each repository in the target organization, the script:

1. Skips archived repositories
2. Temporarily enables Issues if disabled (restores original state after)
3. Checks for an existing open trigger issue to avoid duplicates
4. Creates the trigger issue
5. Writes a per-repo result row to the CSV audit trail
6. Proactively checks GitHub API rate limits and sleeps until reset if limits are low

> **Scans are not triggered directly.** The issue acts as a signal to the Veracode Workflow App, which initiates scans based on the `veracode.yml` configuration in each repository. If `issues.trigger` is not set to `true` in `veracode.yml`, no scan will start.

---

## Quickstart

### Create trigger issues across one org

```bash
python script.py my-github-org
```

### Create trigger issues across multiple orgs

```bash
python script.py --org-file orgs.txt
```

### Clean up trigger issues

```bash
python script.py --delete my-github-org
python script.py --delete --org-file orgs.txt
```

---

## Requirements

```bash
gh --version    # GitHub CLI v2+
python --version  # Python 3.10+
```

No additional dependencies - stdlib only.

---

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

### Required Permissions

| Operation | Required |
|-----------|----------|
| Create / delete issues | Read access + permission to create issues |
| Enable Issues on repos where disabled | Admin permission on those repos |

If Issues cannot be enabled on a repository, the script skips it and records the reason in the CSV.

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
| `--org-file FILE` | Path to a text file with one org per line. `#` lines and blank lines are ignored. Mutually exclusive with the positional org argument. |
| `--delete` | Close previously created trigger issues instead of creating new ones |
| `--hostname HOSTNAME` | GitHub hostname. Omit for github.com. GHES: `github.mycompany.com`. GHEC: `myorg.ghe.com`. Sets `GH_HOST` per-call without modifying your shell. |
| `--output FILE` | CSV output path (default: `vcbaseline.csv`) |
| `--repo-limit N` | Max repos to fetch per org (default: `1000`) |
| `--min-remaining N` | Pause when Core API remaining ≤ N (default: `100`) |
| `--rl-check-every N` | Check rate limit every N gh calls (default: `50`) |

Environment variables `GH_RL_MIN_REMAINING`, `GH_RL_CHECK_EVERY`, and `REPO_LIST_LIMIT` set the same defaults as their flag equivalents. Flags take precedence.

---

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

---

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

Rate limit pauses are logged to stderr so the CSV output stays clean.

---

## Output

`vcbaseline.csv` is written to the working directory after every run (override with `--output`). One row per repository. In multi-org runs all orgs land in the same file, filter by the `org` column to isolate results per organization.

#### Create Mode

| Field | Description |
|-------|-------------|
| `org` | GitHub organization name |
| `repo` | Full `org/repo` name |
| `primary_language` | Repository primary language, or `N/A` |
| `issues_enabled` | Whether Issues were enabled before the run |
| `is_archived` | Whether the repository is archived |
| `action` | Outcome - see values below |

| `action` value | Meaning |
|----------------|---------|
| `created` | Issue created successfully |
| `skipped_existing_issue` | Open trigger issue already exists |
| `skipped_archived` | Repository is archived, skipped |
| `skipped_cant_enable_issues` | Issues could not be enabled (insufficient permissions) |
| `failed_create` | Issue creation API call failed |
| `failed_query_issues` | Could not check for existing issues; creation skipped to prevent duplicates |

#### Delete Mode

| Field | Description |
|-------|-------------|
| `org` | GitHub organization name |
| `repo` | Full `org/repo` name |
| `primary_language` | Repository primary language, or `N/A` |
| `is_archived` | Whether the repository is archived |
| `issues_deleted` | Number of issues closed |
| `action` | Outcome - see values below |

| `action` value | Meaning |
|----------------|---------|
| `deleted` | All matching issues closed |
| `partial_delete` | Some issues closed, some failed |
| `no_issues_found` | No open trigger issues found |
| `skipped_archived` | Repository is archived, skipped |
| `failed_query_issues` | Could not query issues; repository skipped |

---

## Duplicate Protection

Before creating an issue, the script checks for an existing open issue with the same title. If the check fails (API error), creation is skipped and recorded as `failed_query_issues` - this prevents accidental duplicates when the existing-issue check cannot be completed.

To re-trigger scans:

- Use `--delete` to close existing issues, then run again in create mode
- Close the existing issue manually
- Update `ISSUE_TITLE` in the script

---

## Troubleshooting

- **"API rate limit exceeded / HTTP 403 / abuse detection"**
  - The script automatically sleeps until reset and retries. To reduce risk on large runs:
    ```bash
    python script.py --min-remaining 200 --rl-check-every 25 --org-file orgs.txt
    ```

- **"Cannot access the following org(s)" on startup**
  - Check for typos in the org name or file, or confirm your token has access to all listed orgs. No changes are made until access is confirmed for all orgs.

- **"No org names found" when using --org-file**
  - Every line is blank or starts with `#`. Add at least one uncommented org name.

- **"Invalid org name(s)" when using --org-file or a positional org**
  - Org names must start with a letter or digit, contain only alphanumeric characters and hyphens, and be at most 39 characters.

- **"Could not enable issues" on some repositories**
  - The token lacks admin permissions on those repos. The script skips them and records `skipped_cant_enable_issues` in the CSV.

- **"SyntaxError" or unexpected failures**
  - Requires Python 3.10 or newer. Check with `python --version`.

- **GHES: "authentication failed" or "Could not resolve host"**
  - Ensure `GH_ENTERPRISE_TOKEN` is set, `gh auth login --hostname your.ghes.host` has been run at least once, and the host is reachable from your machine.

- **GHEC: targeting the wrong GitHub instance**
  - Use `--hostname myorg.ghe.com` to force the correct host. GHEC uses `GH_TOKEN`, not `GH_ENTERPRISE_TOKEN`.

---

Supported platforms: GitHub.com · GitHub Enterprise Cloud · GitHub Enterprise Server
