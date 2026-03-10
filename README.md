# Veracode Workflow App - Issue Scan Trigger Script

## Overview

This script is executed **locally using GitHub CLI** to create (or clean up) GitHub issues across repositories in a GitHub organization.  
These issues act as **triggers for the Veracode Workflow App**, initiating scans based on the `veracode.yml` configuration.

Two versions of the script are available:

- `script.sh` - Bash version (Linux, macOS, WSL2)
- `script.py` - Python port with identical behavior and flags

Both versions are functionally equivalent. The Python version is provided as an alternative for environments where Python is preferred or Bash is unavailable.

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

Authenticate the GitHub CLI before running either version:

```bash
gh auth login
```

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

All flags and behavior are identical between the Bash and Python versions. Simply substitute `./script.sh` with `python3 script.py` (or `./script.py` if made executable).

### Trigger Scans / Create Issues Mode (Default)

```bash
# Bash
./script.sh <github-org-name>

# Python
python script.py <github-org-name>
```

Example:

```bash
./script.sh my-github-org
python script.py my-github-org
```

### Delete Issues Mode

```bash
# Bash
./script.sh --delete <github-org-name>

# Python
python script.py --delete <github-org-name>
```

### Large Organizations (More Than 1,000 Repos)

```bash
# Bash
./script.sh --repo-limit 5000 my-github-org

# Python
python script.py --repo-limit 5000 my-github-org
```

> Tip: You can set `--repo-limit` higher than 1000 for very large orgs. The script remains safe by pacing itself using rate-limit checks (see below).

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

Be extra cautious for very large orgs:

```bash
# Bash
./script.sh --repo-limit 10000 --min-remaining 200 --rl-check-every 25 my-github-org

# Python
python script.py --repo-limit 10000 --min-remaining 200 --rl-check-every 25 my-github-org
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

A file named `vcbaseline.csv` is generated in the working directory by both versions.

#### Create Mode Fields:

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

The CSV serves as the execution audit trail.

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
- DevSecOps automation
- Cleanup of trigger issues after scan completion

---

## Troubleshooting

- **"API rate limit exceeded / HTTP 403 / abuse detection"**
  - The script automatically sleeps until reset and retries.
  - To be extra safe, lower the check interval and raise the remaining threshold:
    ```bash
    ./script.sh --min-remaining 200 --rl-check-every 25 my-github-org
    python script.py --min-remaining 200 --rl-check-every 25 my-github-org
    ```

- **"Could not enable issues" on some repositories**
  - Your token/account likely lacks admin permissions on those repos.
  - The script skips them and logs the reason in the CSV.

- **Python version: "python3: command not found"**
  - Ensure Python 3.10+ is installed and on your PATH.

- **Python version: "SyntaxError" or unexpected failures**
  - The Python version requires Python 3.10 or newer due to `list[str]` type hint syntax.
  - Check your version with `python --version` and upgrade if needed.
