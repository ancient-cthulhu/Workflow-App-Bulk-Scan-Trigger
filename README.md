# Veracode Workflow App - Issue Scan Trigger Script

## Overview

This script is executed **locally using GitHub CLI** to create (or clean up) GitHub issues across repositories in one or more GitHub organizations.  
These issues act as **triggers for the Veracode Workflow App**, initiating scans based on the `veracode.yml` configuration.

Two versions of the script are available:

- `script.sh` - Bash version (Linux, macOS, WSL2)
- `script.py` - Python port with identical behavior and flags

Both versions are functionally equivalent and support GitHub.com, GitHub Enterprise Cloud (GHEC), and GitHub Enterprise Server (GHES). The Python version is provided as an alternative for environments where Python is preferred or Bash is unavailable.

---

## What the Script Does

For each repository in the specified GitHub organization, the script:

1. Lists repositories using the GitHub CLI
2. Skips archived repositories
3. Checks whether Issues are enabled
4. Temporarily enables Issues if required
5. Creates a trigger issue
6. Avoids creating duplicate open issues
7. Restores the original Issues configuration
8. Generates a CSV report of all actions taken
9. Proactively checks **GitHub API rate limits** and sleeps until reset if limits are low

---

## What the Script Does Not Do

- It does not run Veracode scans directly
- It does not modify source code
- It does not create issues in archived repositories
- It does not permanently change repository settings
- It does not permanently delete issues (closes them instead)

---

## System Requirements (Local PC)

### Supported Environments

- Linux
- macOS
- Windows via WSL2

### Required Tools

**Both versions require:**

- GitHub CLI (`gh`) v2+

**Bash version (`script.sh`) additionally requires:**

- Bash

**Python version (`script.py`) additionally requires:**

- Python 3.10+

Verify installations:

```bash
gh --version
python --version
```

---

## Authentication and Permissions

### GitHub.com and GHEC

Authenticate interactively before running:

```bash
gh auth login
```

Or set a token in the environment (useful for automation):

```bash
export GH_TOKEN=ghp_yourtoken
```

### GitHub Enterprise Server (GHES)

Authenticate against your GHES instance:

```bash
gh auth login --hostname github.mycompany.com
```

Or set the token and hostname in the environment:

```bash
export GH_ENTERPRISE_TOKEN=ghp_yourtoken
export GH_HOST=github.mycompany.com
```

When using the `--hostname` flag (see Running the Script below), the script sets `GH_HOST` automatically for every `gh` call without requiring you to export it in your shell. You still need to set `GH_ENTERPRISE_TOKEN` (GHES) or `GH_TOKEN` (GitHub.com / GHEC) before running.

The script will warn you if `--hostname` is set to a GHES instance but `GH_ENTERPRISE_TOKEN` is not found in the environment.

### Required Permissions

The authenticated user must have:

- Read access to all target repositories
- Permission to create issues
- Admin permission on repositories where Issues may be disabled (recommended)

If the user cannot enable Issues on a repository, the script will safely skip issue creation and record the reason in the output.

---

## Veracode Workflow App Configuration (Required)

For issue-based triggers to work, each target repository **must allow issue triggers** in `veracode.yml` for each of the desired scan types (SAST, SCA, IaC).

### Required `veracode.yml` Configuration

```yaml
issues:
  trigger: true
  commands:
    - "Veracode **** Scan"
    - "Veracode All Scans"
```

### Important Notes

- Add `"Veracode All Scans"` under each of the `"Veracode **** Scan"` in the 3 scan types.
- If `issues.trigger` is set to `false`, the script will create the issue but **no scan will start**
- The command value **must exactly match** the Workflow App command name: `Veracode All Scans`

This configuration mismatch is the most common reason scans do not trigger.

---

## Installation

### Bash Version

1. Save the script as `script.sh`
2. Make it executable:

```bash
chmod +x script.sh
```

### Python Version

1. Save the script as `script.py`
2. Make it executable (optional):

```bash
chmod +x script.py
```

No additional dependencies or virtual environments are required - the Python version uses stdlib only.

---

## Running the Script

All flags and behavior are identical between the Bash and Python versions. Simply substitute `./script.sh` with `python script.py` (or `./script.py` if made executable).

### Single Org Mode

Pass the org name as a positional argument:

```bash
# Bash
./script.sh my-github-org

# Python
python script.py my-github-org
```

### Multi-Org Mode (org file)

Use `--org-file` to point to a text file with one org per line. Lines starting with `#` and blank lines are ignored.

Example `orgs.txt`:

```
# One org name per line
# Lines starting with # are treated as comments

acme-dev
acme-staging
acme-prod
acme-archive
```

```bash
# Bash
./script.sh --org-file orgs.txt

# Python
python script.py --org-file orgs.txt
```

Before processing begins, the script checks access to **all orgs in the file** and aborts if any are inaccessible - so there are no partial runs due to a typo or permission issue mid-way through.

Each org prints its own per-org stats block as it completes. At the end, a rolled-up summary table is printed across all orgs.

> Note: `--org-file` and a positional org name are mutually exclusive. Use one or the other.

### Delete Issues Mode

Works with both single org and org file:

```bash
# Bash
./script.sh --delete my-github-org
./script.sh --delete --org-file orgs.txt

# Python
python script.py --delete my-github-org
python script.py --delete --org-file orgs.txt
```

### GitHub Enterprise Server (GHES)

Use `--hostname` to target a GHES instance. The script sets `GH_HOST` for every `gh` call internally - no need to export it in your shell. Set `GH_ENTERPRISE_TOKEN` before running.

```bash
export GH_ENTERPRISE_TOKEN=ghp_yourtoken

# Bash
./script.sh --hostname github.mycompany.com my-org
./script.sh --hostname github.mycompany.com --org-file orgs.txt

# Python
python script.py --hostname github.mycompany.com my-org
python script.py --hostname github.mycompany.com --org-file orgs.txt
```

### GitHub Enterprise Cloud (GHEC)

GHEC uses `GH_TOKEN` (same as GitHub.com). Use `--hostname` with your GHEC subdomain if needed:

```bash
export GH_TOKEN=ghp_yourtoken

# Bash
./script.sh --hostname myorg.ghe.com my-org

# Python
python script.py --hostname myorg.ghe.com my-org
```

> Note: For most GHEC setups authenticated with `gh auth login`, `--hostname` is not required as `gh` will already be configured for the correct host.

### Large Organizations (More Than 1,000 Repos)

`--repo-limit` applies per org:

```bash
# Bash
./script.sh --repo-limit 5000 my-github-org
./script.sh --repo-limit 5000 --org-file orgs.txt

# Python
python script.py --repo-limit 5000 my-github-org
python script.py --repo-limit 5000 --org-file orgs.txt
```

> Tip: You can set `--repo-limit` higher than 1000 for very large orgs, the script paces itself using rate-limit checks (see below).

### Usage Help

```bash
./script.sh --help
python script.py --help
```

---

## Rate Limiting & Large Orgs

To keep things simple and reliable at scale, the script uses **proactive GitHub API rate-limit checks**:

- Periodically queries `gh api rate_limit` (Core REST quota).
- If the **remaining** requests drop below a threshold, it **sleeps until the official reset time** and then resumes.
- This prevents failures from "API rate limit exceeded" and "secondary rate limit" scenarios.

**Flags (simple & explicit):**

- `--min-remaining N` - pause when Core remaining <= N (default: `100`)
- `--rl-check-every N` - check the rate limit every N GitHub calls (default: `50`)

**Examples:**

Be cautious for very large orgs:

```bash
# Bash
./script.sh --repo-limit 10000 --min-remaining 200 --rl-check-every 25 my-github-org
./script.sh --repo-limit 10000 --min-remaining 200 --rl-check-every 25 --org-file orgs.txt

# Python
python script.py --repo-limit 10000 --min-remaining 200 --rl-check-every 25 my-github-org
python script.py --repo-limit 10000 --min-remaining 200 --rl-check-every 25 --org-file orgs.txt
```

Keep defaults (still safe for thousands of repos):

```bash
./script.sh --repo-limit 5000 my-github-org
python script.py --repo-limit 5000 my-github-org
```

**Notes:**

- The script logs rate-limit pauses to **stderr** (so CSV output remains clean).
- You can also set environment variables (`GH_RL_MIN_REMAINING`, `GH_RL_CHECK_EVERY`), but **flags take precedence**.

---

## Output

### CSV Report

A file named `vcbaseline.csv` is generated in the working directory by both versions. When running in multi-org mode, all orgs are written to the same CSV file with an `org` column added as the first field.

#### Create Mode Fields:

- `org`
- `repo`
- `primary_language`
- `issues_enabled`
- `is_archived`
- `action`

Common `action` values:

- `created`
- `skipped_archived`
- `skipped_existing_issue`
- `skipped_cant_enable_issues`
- `failed_create`

#### Delete Mode Fields:

- `org`
- `repo`
- `primary_language`
- `is_archived`
- `issues_deleted`
- `action`

Common `action` values:

- `deleted`
- `partial_delete`
- `no_issues_found`
- `skipped_archived`

The CSV serves as the execution audit trail. In multi-org runs, filtering by the `org` column isolates results per organization.

---

## Duplicate Protection

The script checks for an **open issue with the same title** before creating a new one.

To re-trigger scans:

- Use the `--delete` flag to clean up existing issues, then run in create mode again
- Close the existing issue manually, or
- Update the issue title in the script

---

## Intended Use

- Veracode onboarding at scale
- Organization-wide scan triggering
- Periodic re-scans
- Local DevSecOps automation
- Cleanup of trigger issues after scan completion

---

## Troubleshooting

- **"API rate limit exceeded / HTTP 403 / abuse detection"**
  - The script automatically sleeps until reset and retries.
  - To be extra safe, lower the check interval and raise the remaining threshold:
    ```bash
    ./script.sh --min-remaining 200 --rl-check-every 25 --org-file orgs.txt
    python script.py --min-remaining 200 --rl-check-every 25 --org-file orgs.txt
    ```

- **"Cannot access the following org(s)" on startup**
  - The script validates access to all orgs before doing any work. Check for typos in the org file, or that your `gh auth login` token has access to all listed orgs.
  - Fix the issue and retry - no partial state is written until the run starts.

- **"No org names found" when using --org-file**
  - Every line in the file is either blank or starts with `#`. Add at least one uncommented org name.

- **"Could not enable issues" on some repositories**
  - Your token/account likely lacks admin permissions on those repos.
  - The script skips them and logs the reason in the CSV.

- **Python version: "SyntaxError" or unexpected failures**
  - The Python version requires Python 3.10 or newer due to `list[str]` type hint syntax.
  - Check your version with `python --version` and upgrade if needed.

- **GHES: "authentication failed" or "Could not resolve host"**
  - Ensure `GH_ENTERPRISE_TOKEN` is set and that `gh auth login --hostname your.ghes.host` has been run at least once.
  - Pass `--hostname your.ghes.host` explicitly so the script targets the right instance.
  - Confirm network connectivity to the GHES host from your local machine.

- **GHEC: targeting the wrong GitHub instance**
  - If authenticated to both GitHub.com and GHEC, use `--hostname myorg.ghe.com` to force the correct host.
  - GHEC uses `GH_TOKEN`, not `GH_ENTERPRISE_TOKEN`.
