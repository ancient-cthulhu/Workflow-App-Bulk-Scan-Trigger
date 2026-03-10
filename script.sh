#!/usr/bin/env bash

set -o pipefail

OUTPUT_FILE="vcbaseline.csv"
> "$OUTPUT_FILE"

ORG=""
ORG_FILE=""
DELETE_MODE=false
ISSUE_TITLE="Veracode Baseline Scans"
ISSUE_BODY="Veracode All Scans"

# Simple proactive rate limiting
GH_RL_MIN_REMAINING="${GH_RL_MIN_REMAINING:-100}"
GH_RL_CHECK_EVERY="${GH_RL_CHECK_EVERY:-50}"
REPO_LIST_LIMIT="${REPO_LIST_LIMIT:-1000}"

CALL_COUNT=0
RATE_REMAINING=""
RATE_RESET_EPOCH=""

query_rate_limit() {
  local out remaining reset_epoch
  out=$(gh api rate_limit --jq '.resources.core.remaining, .resources.core.reset' 2>/dev/null) || return 1
  remaining=$(printf '%s\n' "$out" | sed -n '1p')
  reset_epoch=$(printf '%s\n' "$out" | sed -n '2p')
  [[ -n "$remaining" && -n "$reset_epoch" ]] || return 1
  RATE_REMAINING="$remaining"
  RATE_RESET_EPOCH="$reset_epoch"
  return 0
}

maybe_pause_for_rate_limit() {
  CALL_COUNT=$((CALL_COUNT + 1))
  if (( CALL_COUNT % GH_RL_CHECK_EVERY != 0 )); then
    return 0
  fi

  if query_rate_limit; then
    if (( RATE_REMAINING <= GH_RL_MIN_REMAINING )); then
      local now sleep_for reset_human
      now=$(date +%s)
      sleep_for=$(( RATE_RESET_EPOCH - now + 1 ))
      if (( sleep_for > 0 )); then
        reset_human=$(date -d @"$RATE_RESET_EPOCH" 2>/dev/null || date -r "$RATE_RESET_EPOCH")
        echo "Core rate limit low (remaining=${RATE_REMAINING}). Sleeping ~${sleep_for}s until reset (${reset_human})." >&2
        sleep "$sleep_for"
      fi
    fi
  fi
}

gh_call() {
  maybe_pause_for_rate_limit

  local tmp_out tmp_err status output err
  tmp_out="$(mktemp)"; tmp_err="$(mktemp)"
  "$@" >"$tmp_out" 2>"$tmp_err"
  status=$?
  output="$(cat "$tmp_out")"
  err="$(cat "$tmp_err")"
  rm -f "$tmp_out" "$tmp_err"

  if [[ $status -eq 0 ]]; then
    printf '%s\n' "$output"
    return 0
  fi

  if printf '%s\n%s' "$err" "$output" | grep -qiE 'rate limit|secondary rate|abuse detection|HTTP 403|API rate limit exceeded'; then
    if query_rate_limit; then
      local now sleep_for
      now=$(date +%s)
      sleep_for=$(( RATE_RESET_EPOCH - now + 1 ))
      if (( sleep_for > 0 )); then
        echo "Rate limited. Sleeping ${sleep_for}s until reset, then retrying once..." >&2
        sleep "$sleep_for"
      fi
    else
      echo "Rate limited. Sleeping 30s (could not query reset)..." >&2
      sleep 30
    fi

    tmp_out="$(mktemp)"; tmp_err="$(mktemp)"
    "$@" >"$tmp_out" 2>"$tmp_err"
    status=$?
    output="$(cat "$tmp_out")"
    err="$(cat "$tmp_err")"
    rm -f "$tmp_out" "$tmp_err"
    if [[ $status -eq 0 ]]; then
      printf '%s\n' "$output"
      return 0
    fi
  fi

  printf '%s\n' "$err" >&2
  return $status
}

# -------------------------
# Parse command line arguments
# -------------------------
while [[ $# -gt 0 ]]; do
  case $1 in
    --delete)
      DELETE_MODE=true
      shift
      ;;
    --org-file)
      ORG_FILE="$2"; shift 2
      ;;
    --repo-limit)
      REPO_LIST_LIMIT="$2"; shift 2
      ;;
    --min-remaining)
      if [[ "$2" =~ ^[0-9]+$ ]]; then
        GH_RL_MIN_REMAINING="$2"; shift 2
      else
        echo "Invalid --min-remaining value: $2 (must be an integer >= 0)" >&2
        exit 1
      fi
      ;;
    --rl-check-every)
      if [[ "$2" =~ ^[0-9]+$ ]] && (( $2 > 0 )); then
        GH_RL_CHECK_EVERY="$2"; shift 2
      else
        echo "Invalid --rl-check-every value: $2 (must be an integer > 0)" >&2
        exit 1
      fi
      ;;
    --help|-h)
      echo "Usage: $0 [--delete] [--org-file FILE | <github-org-name>] [--repo-limit N] [--min-remaining N] [--rl-check-every N]"
      echo
      echo "Org input (choose one):"
      echo "  <github-org-name>    Single org name as a positional argument."
      echo "  --org-file FILE      Path to a text file with one org per line (# lines are comments)."
      echo
      echo "Options:"
      echo "  --delete             Close previously created trigger issues instead of creating new ones."
      echo "  --repo-limit N       Max repos to fetch per org (default: ${REPO_LIST_LIMIT})."
      echo "  --min-remaining N    Pause when Core API remaining <= N (default: ${GH_RL_MIN_REMAINING})."
      echo "  --rl-check-every N   Check rate limit every N gh calls (default: ${GH_RL_CHECK_EVERY})."
      exit 0
      ;;
    *)
      ORG="$1"
      shift
      ;;
  esac
done

# Validate: must have either a positional org or --org-file, not both
if [[ -n "$ORG" && -n "$ORG_FILE" ]]; then
  echo "Error: Provide either a positional org name or --org-file, not both." >&2
  exit 1
fi

if [[ -z "$ORG" && -z "$ORG_FILE" ]]; then
  echo "Usage: $0 [--delete] [--org-file FILE | <github-org-name>] [--repo-limit N] [--min-remaining N] [--rl-check-every N]"
  echo
  echo "Examples:"
  echo "  $0 my-org"
  echo "  $0 --org-file orgs.txt"
  echo "  $0 --delete --org-file orgs.txt"
  echo "  $0 --repo-limit 5000 --org-file orgs.txt"
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: GitHub CLI (gh) is not installed." >&2
  exit 1
fi

# -------------------------
# Load orgs from file or positional arg
# -------------------------
declare -a ORGS=()

if [[ -n "$ORG_FILE" ]]; then
  if [[ ! -f "$ORG_FILE" ]]; then
    echo "Error: Org file not found: $ORG_FILE" >&2
    exit 1
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Strip leading/trailing whitespace
    trimmed="${line#"${line%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    # Skip blank lines and comments
    [[ -z "$trimmed" || "$trimmed" == \#* ]] && continue
    ORGS+=("$trimmed")
  done < "$ORG_FILE"

  if [[ ${#ORGS[@]} -eq 0 ]]; then
    echo "Error: No org names found in '$ORG_FILE' (all lines are blank or comments)." >&2
    exit 1
  fi
  echo "Loaded ${#ORGS[@]} org(s) from '$ORG_FILE'."
else
  ORGS=("$ORG")
fi

TOTAL_ORGS=${#ORGS[@]}
MULTI_ORG_MODE=false
(( TOTAL_ORGS > 1 )) && MULTI_ORG_MODE=true

# -------------------------
# Pre-flight: check access to all orgs before starting work
# -------------------------
echo ""
echo "Checking access to all organizations..."
INACCESSIBLE=()
for org in "${ORGS[@]}"; do
  printf "  Checking %s... " "$org"
  if gh_call gh api "orgs/$org" --silent >/dev/null; then
    echo "OK"
  else
    echo "FAILED"
    INACCESSIBLE+=("$org")
  fi
done

if [[ ${#INACCESSIBLE[@]} -gt 0 ]]; then
  echo "" >&2
  echo "Error: Cannot access the following org(s): ${INACCESSIBLE[*]}" >&2
  echo "Aborting. Fix access or remove them from the org file and retry." >&2
  exit 1
fi
echo ""

# -------------------------
# CSV header
# -------------------------
if [[ "$DELETE_MODE" == true ]]; then
  printf "org,repo,primary_language,is_archived,issues_deleted,action\n" >> "$OUTPUT_FILE"
else
  printf "org,repo,primary_language,issues_enabled,is_archived,action\n" >> "$OUTPUT_FILE"
fi

# Per-org stats arrays (parallel to ORGS)
declare -a STAT_TOTAL=()
declare -a STAT_ARCHIVED=()
declare -a STAT_CREATED=()
declare -a STAT_DELETED=()
declare -a STAT_FAILED=()
declare -a STAT_SKIPPED_EXISTING=()
declare -a STAT_SKIPPED_PERM=()
declare -a STAT_IAC=()
declare -a STAT_NO_ISSUES=()

# -------------------------
# Main loop over orgs
# -------------------------
org_index=0
for org in "${ORGS[@]}"; do
  org_index=$((org_index + 1))

  if [[ "$MULTI_ORG_MODE" == true ]]; then
    echo ""
    printf '%0.s#' {1..64}; echo
    echo "# Organization ${org_index}/${TOTAL_ORGS}: ${org}"
    printf '%0.s#' {1..64}; echo
  fi

  # ---- DELETE MODE ----
  if [[ "$DELETE_MODE" == true ]]; then
    echo "================================================================"
    echo "DELETE MODE: Removing issues with title '$ISSUE_TITLE'"
    echo "================================================================"
    echo ""

    total_repos=0
    deleted_count=0
    skipped_no_issues=0
    failed_delete_count=0
    archived_count=0

    while IFS=$'\t' read -r name_with_owner _issues_enabled primary_lang is_archived; do
      echo "-------------------------------------------"
      echo "Processing $name_with_owner"
      total_repos=$((total_repos + 1))

      if [[ "$is_archived" == "true" ]]; then
        archived_count=$((archived_count + 1))
        echo "Repository is archived. Skipping."
        echo "$org,$name_with_owner,$primary_lang,$is_archived,0,skipped_archived" >> "$OUTPUT_FILE"
        continue
      fi

      issue_numbers="$(
        gh_call gh issue list \
          --repo "$name_with_owner" \
          --state open \
          --search "$ISSUE_TITLE in:title" \
          --json number \
          --jq '.[].number' || true
      )"

      if [[ -z "$issue_numbers" ]]; then
        echo "No matching issues found."
        skipped_no_issues=$((skipped_no_issues + 1))
        echo "$org,$name_with_owner,$primary_lang,$is_archived,0,no_issues_found" >> "$OUTPUT_FILE"
        continue
      fi

      issues_deleted=0
      issues_failed=0

      while IFS= read -r issue_num; do
        if [[ -n "$issue_num" ]]; then
          echo "Closing issue #$issue_num..."
          if gh_call gh issue close "$issue_num" --repo "$name_with_owner" --comment "Closed by cleanup script" >/dev/null; then
            issues_deleted=$((issues_deleted + 1))
            deleted_count=$((deleted_count + 1))
          else
            echo "Failed to close issue #$issue_num"
            issues_failed=$((issues_failed + 1))
            failed_delete_count=$((failed_delete_count + 1))
          fi
        fi
      done <<< "$issue_numbers"

      if [[ $issues_failed -gt 0 ]]; then
        action="partial_delete"
      else
        action="deleted"
      fi

      echo "$org,$name_with_owner,$primary_lang,$is_archived,$issues_deleted,$action" >> "$OUTPUT_FILE"

    done < <(
      gh_call gh repo list "$org" \
        --limit "$REPO_LIST_LIMIT" \
        --json nameWithOwner,hasIssuesEnabled,primaryLanguage,isArchived \
        --jq '.[] | [
          .nameWithOwner,
          (.hasIssuesEnabled|tostring),
          (.primaryLanguage.name // "N/A"),
          (.isArchived|tostring)
        ] | @tsv'
    )

    echo ""
    echo "Finished closing issues for org: $org"
    echo ""
    echo "Delete Stats for Organization: $org"
    echo "----------------------------------------------------------------"
    echo "Total Repositories:            $total_repos"
    echo "Archived Repositories:         $archived_count"
    echo "Repositories with No Issues:   $skipped_no_issues"
    echo "Issues Closed:                 $deleted_count"
    echo "Failed Closes:                 $failed_delete_count"
    echo "----------------------------------------------------------------"

    STAT_TOTAL+=("$total_repos")
    STAT_ARCHIVED+=("$archived_count")
    STAT_NO_ISSUES+=("$skipped_no_issues")
    STAT_DELETED+=("$deleted_count")
    STAT_FAILED+=("$failed_delete_count")

  # ---- CREATE MODE ----
  else
    total_count=0
    iac_count=0
    archived_count=0
    created_count=0
    skipped_existing_count=0
    skipped_archived_count=0
    skipped_issues_perm_count=0
    failed_count=0

    while IFS=$'\t' read -r name_with_owner issues_enabled primary_lang is_archived; do
      echo "-------------------------------------------"
      echo "Processing $name_with_owner"
      total_count=$((total_count + 1))

      if [[ "$primary_lang" =~ ^(HCL|Bicep)$ ]]; then
        iac_count=$((iac_count + 1))
      fi

      if [[ "$is_archived" == "true" ]]; then
        archived_count=$((archived_count + 1))
        skipped_archived_count=$((skipped_archived_count + 1))
        echo "Repository is archived. Skipping."
        echo "$org,$name_with_owner,$primary_lang,$issues_enabled,$is_archived,skipped_archived" >> "$OUTPUT_FILE"
        continue
      fi

      WAS_DISABLED=false
      if [[ "$issues_enabled" == "false" ]]; then
        echo "Issues are disabled. Temporarily enabling..."
        if ! gh_call gh repo edit "$name_with_owner" --enable-issues >/dev/null; then
          echo "Could not enable issues. Skipping issue creation."
          skipped_issues_perm_count=$((skipped_issues_perm_count + 1))
          echo "$org,$name_with_owner,$primary_lang,$issues_enabled,$is_archived,skipped_cant_enable_issues" >> "$OUTPUT_FILE"
          continue
        fi
        WAS_DISABLED=true
      fi

      existing_open_count="$(
        gh_call gh issue list \
          --repo "$name_with_owner" \
          --state open \
          --search "$ISSUE_TITLE in:title" \
          --json number \
          --jq 'length' || true
      )"

      if [[ -n "$existing_open_count" && "$existing_open_count" != "0" ]]; then
        echo "Open issue with same title already exists. Skipping."
        skipped_existing_count=$((skipped_existing_count + 1))

        if [[ "$WAS_DISABLED" == true ]]; then
          echo "Restoring state: Disabling issues..."
          gh_call gh repo edit "$name_with_owner" --enable-issues=false >/dev/null || true
        fi

        echo "$org,$name_with_owner,$primary_lang,$issues_enabled,$is_archived,skipped_existing_issue" >> "$OUTPUT_FILE"
        continue
      fi

      echo "Creating issue..."
      if gh_call gh issue create --repo "$name_with_owner" --title "$ISSUE_TITLE" --body "$ISSUE_BODY" >/dev/null; then
        created_count=$((created_count + 1))
        action="created"
      else
        echo "Failed to create issue."
        failed_count=$((failed_count + 1))
        action="failed_create"
      fi

      if [[ "$WAS_DISABLED" == true ]]; then
        echo "Restoring state: Disabling issues..."
        gh_call gh repo edit "$name_with_owner" --enable-issues=false >/dev/null || true
      fi

      echo "$org,$name_with_owner,$primary_lang,$issues_enabled,$is_archived,$action" >> "$OUTPUT_FILE"

    done < <(
      gh_call gh repo list "$org" \
        --limit "$REPO_LIST_LIMIT" \
        --json nameWithOwner,hasIssuesEnabled,primaryLanguage,isArchived \
        --jq '.[] | [
          .nameWithOwner,
          (.hasIssuesEnabled|tostring),
          (.primaryLanguage.name // "N/A"),
          (.isArchived|tostring)
        ] | @tsv'
    )

    echo ""
    echo "Finished processing all repositories for org: $org"
    echo ""
    echo "Repository Stats for Organization: $org"
    echo "----------------------------------------------------------------"
    echo "Total Repositories:       $total_count"
    echo "Archived Repositories:    $archived_count"
    echo "Skipped Archived:         $skipped_archived_count"
    echo "IaC Repositories:         $iac_count (Primary language: HCL/Bicep)"
    echo "Issues Permission Skips:  $skipped_issues_perm_count"
    echo "Skipped Existing Issues:  $skipped_existing_count"
    echo "Created Issues:           $created_count"
    echo "Failed Creates:           $failed_count"
    echo "----------------------------------------------------------------"

    STAT_TOTAL+=("$total_count")
    STAT_ARCHIVED+=("$archived_count")
    STAT_IAC+=("$iac_count")
    STAT_SKIPPED_PERM+=("$skipped_issues_perm_count")
    STAT_SKIPPED_EXISTING+=("$skipped_existing_count")
    STAT_CREATED+=("$created_count")
    STAT_FAILED+=("$failed_count")
  fi

done

# -------------------------
# Multi-org summary
# -------------------------
if [[ "$MULTI_ORG_MODE" == true ]]; then
  echo ""
  printf '%0.s=' {1..64}; echo
  echo "MULTI-ORG SUMMARY"
  printf '%0.s=' {1..64}; echo

  if [[ "$DELETE_MODE" == true ]]; then
    printf "%-30s %6s %8s %8s %9s %10s\n" "Org" "Repos" "Closed" "Failed" "Archived" "No Issues"
    printf '%0.s-' {1..75}; echo
    grand_repos=0; grand_deleted=0; grand_failed=0; grand_archived=0; grand_no_issues=0
    for i in "${!ORGS[@]}"; do
      printf "%-30s %6s %8s %8s %9s %10s\n" \
        "${ORGS[$i]}" "${STAT_TOTAL[$i]}" "${STAT_DELETED[$i]}" "${STAT_FAILED[$i]}" \
        "${STAT_ARCHIVED[$i]}" "${STAT_NO_ISSUES[$i]}"
      grand_repos=$((grand_repos + STAT_TOTAL[i]))
      grand_deleted=$((grand_deleted + STAT_DELETED[i]))
      grand_failed=$((grand_failed + STAT_FAILED[i]))
      grand_archived=$((grand_archived + STAT_ARCHIVED[i]))
      grand_no_issues=$((grand_no_issues + STAT_NO_ISSUES[i]))
    done
    printf '%0.s-' {1..75}; echo
    printf "%-30s %6s %8s %8s %9s %10s\n" "TOTAL" \
      "$grand_repos" "$grand_deleted" "$grand_failed" "$grand_archived" "$grand_no_issues"
  else
    printf "%-30s %6s %8s %8s %9s %8s %9s %5s\n" "Org" "Repos" "Created" "Failed" "Archived" "Skipped" "PermSkip" "IaC"
    printf '%0.s-' {1..90}; echo
    grand_repos=0; grand_created=0; grand_failed=0; grand_archived=0
    grand_skipped=0; grand_perm=0; grand_iac=0
    for i in "${!ORGS[@]}"; do
      printf "%-30s %6s %8s %8s %9s %8s %9s %5s\n" \
        "${ORGS[$i]}" "${STAT_TOTAL[$i]}" "${STAT_CREATED[$i]}" "${STAT_FAILED[$i]}" \
        "${STAT_ARCHIVED[$i]}" "${STAT_SKIPPED_EXISTING[$i]}" "${STAT_SKIPPED_PERM[$i]}" "${STAT_IAC[$i]}"
      grand_repos=$((grand_repos + STAT_TOTAL[i]))
      grand_created=$((grand_created + STAT_CREATED[i]))
      grand_failed=$((grand_failed + STAT_FAILED[i]))
      grand_archived=$((grand_archived + STAT_ARCHIVED[i]))
      grand_skipped=$((grand_skipped + STAT_SKIPPED_EXISTING[i]))
      grand_perm=$((grand_perm + STAT_SKIPPED_PERM[i]))
      grand_iac=$((grand_iac + STAT_IAC[i]))
    done
    printf '%0.s-' {1..90}; echo
    printf "%-30s %6s %8s %8s %9s %8s %9s %5s\n" "TOTAL" \
      "$grand_repos" "$grand_created" "$grand_failed" "$grand_archived" \
      "$grand_skipped" "$grand_perm" "$grand_iac"
  fi

  echo ""
  printf '%0.s=' {1..64}; echo
fi

echo ""
echo "CSV Output: $OUTPUT_FILE"
