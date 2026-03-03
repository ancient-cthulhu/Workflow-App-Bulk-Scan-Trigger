#!/usr/bin/env bash

set -o pipefail

OUTPUT_FILE="vcbaseline.csv"
> "$OUTPUT_FILE"

ORG=""
DELETE_MODE=false
ISSUE_TITLE="Veracode Baseline Scans"
ISSUE_BODY="Veracode All Scans"

# Simple proactive rate limiting
# Defaults (flags override these; env can override defaults)
GH_RL_MIN_REMAINING="${GH_RL_MIN_REMAINING:-100}"  # pause when remaining <= this
GH_RL_CHECK_EVERY="${GH_RL_CHECK_EVERY:-50}"       # check every N GitHub calls

# Only flag we keep for scale
REPO_LIST_LIMIT="${REPO_LIST_LIMIT:-1000}"

CALL_COUNT=0
RATE_REMAINING=""
RATE_RESET_EPOCH=""

# Query core REST rate limit
# Returns 0 on success; sets RATE_REMAINING and RATE_RESET_EPOCH
query_rate_limit() {
  local out remaining reset_epoch
  # Call gh directly here; do not wrap to avoid recursion
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
  # Only check periodically to minimize overhead
  if (( CALL_COUNT % GH_RL_CHECK_EVERY != 0 )); then
    return 0
  fi

  if query_rate_limit; then
    if (( RATE_REMAINING <= GH_RL_MIN_REMAINING )); then
      local now sleep_for reset_human
      now=$(date +%s)
      sleep_for=$(( RATE_RESET_EPOCH - now + 1 ))
      if (( sleep_for > 0 )); then
        # date -d is GNU; on BSD/macOS fall back to -r
        reset_human=$(date -d @"$RATE_RESET_EPOCH" 2>/dev/null || date -r "$RATE_RESET_EPOCH")
        echo "Core rate limit low (remaining=${RATE_REMAINING}). Sleeping ~${sleep_for}s until reset (${reset_human})." >&2
        sleep "$sleep_for"
      fi
    fi
  fi
}

# Minimal wrapper for gh:
# - runs the command
# - checks/pauses for rate limit
# - if we still hit limit, sleeps until reset and retries once
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

  # If we hit rate limit/403, pause to reset and retry once
  if printf '%s\n%s' "$err" "$output" | grep -qiE 'rate limit|secondary rate|abuse detection|HTTP 403|API rate limit exceeded'; then
    # Query reset and sleep
    if query_rate_limit; then
      local now sleep_for
      now=$(date +%s)
      sleep_for=$(( RATE_RESET_EPOCH - now + 1 ))
      if (( sleep_for > 0 )); then
        echo "Rate limited. Sleeping ${sleep_for}s until reset, then retrying once..." >&2
        sleep "$sleep_for"
      fi
    else
      # Fallback: short fixed sleep
      echo "Rate limited. Sleeping 30s (could not query reset)..." >&2
      sleep 30
    fi

    # Retry once
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

  # Non-rate-limit error or retry failed
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
    --repo-limit)
      REPO_LIST_LIMIT="$2"; shift 2
      ;;
    --min-remaining)
      # Validate integer >= 0
      if [[ "$2" =~ ^[0-9]+$ ]]; then
        GH_RL_MIN_REMAINING="$2"; shift 2
      else
        echo "Invalid --min-remaining value: $2 (must be an integer >= 0)" >&2
        exit 1
      fi
      ;;
    --rl-check-every)
      # Validate integer > 0
      if [[ "$2" =~ ^[0-9]+$ ]] && (( $2 > 0 )); then
        GH_RL_CHECK_EVERY="$2"; shift 2
      else
        echo "Invalid --rl-check-every value: $2 (must be an integer > 0)" >&2
        exit 1
      fi
      ;;
    --help|-h)
      echo "Usage: $0 [--delete] [--repo-limit N] [--min-remaining N] [--rl-check-every N] <github-org-name>"
      echo
      echo "Options:"
      echo "  --delete             Close previously created trigger issues instead of creating new ones."
      echo "  --repo-limit N       Max repos to fetch via 'gh repo list' (default: ${REPO_LIST_LIMIT})."
      echo "  --min-remaining N    Pause when Core API remaining <= N (default: ${GH_RL_MIN_REMAINING})."
      echo "  --rl-check-every N   Check rate limit every N gh calls (default: ${GH_RL_CHECK_EVERY})."
      echo
      echo "Notes: flags override environment variables if both are provided."
      exit 0
      ;;
    *)
      ORG="$1"
      shift
      ;;
  esac
done

# Has the org name been provided as a parameter
if [[ -z "$ORG" ]]; then
  echo "Usage: $0 [--delete] [--repo-limit N] [--min-remaining N] [--rl-check-every N] <github-org-name>"
  echo
  echo "Examples:"
  echo "  $0 my-org"
  echo "  $0 --delete my-org"
  echo "  $0 --repo-limit 5000 --min-remaining 200 --rl-check-every 25 my-org"
  exit 1
fi

# Is the github cli installed
if ! command -v gh >/dev/null 2>&1; then
  echo "Error: GitHub CLI (gh) is not installed." >&2
  exit 1
fi

# Can we access the supplied org
echo "Checking access to organization: $ORG..."
if ! gh_call gh api "orgs/$ORG" --silent >/dev/null; then
  echo "Error: You do not have access to the '$ORG' organization or it does not exist." >&2
  exit 1
fi

# DELETE MODE
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
  
  printf "repo,primary_language,is_archived,issues_deleted,action\n" >> "$OUTPUT_FILE"
  
  while IFS=$'\t' read -r name_with_owner issues_enabled primary_lang is_archived; do
    echo "-------------------------------------------"
    echo "Processing $name_with_owner"
    total_repos=$((total_repos + 1))
    
    if [[ "$is_archived" == "true" ]]; then
      archived_count=$((archived_count + 1))
      echo "Repository is archived. Skipping."
      echo "$name_with_owner,$primary_lang,$is_archived,0,skipped_archived" >> "$OUTPUT_FILE"
      continue
    fi
    
    # Find all open issues with matching title
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
      echo "$name_with_owner,$primary_lang,$is_archived,0,no_issues_found" >> "$OUTPUT_FILE"
      continue
    fi
    
    issues_deleted=0
    issues_failed=0
    
    while IFS= read -r issue_num; do
      if [[ -n "$issue_num" ]]; then
        echo "Deleting issue #$issue_num..."
        if gh_call gh issue close "$issue_num" --repo "$name_with_owner" --comment "Closed by cleanup script" >/dev/null; then
          issues_deleted=$((issues_deleted + 1))
          deleted_count=$((deleted_count + 1))
        else
          echo "Failed to delete issue #$issue_num"
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
    
    echo "$name_with_owner,$primary_lang,$is_archived,$issues_deleted,$action" >> "$OUTPUT_FILE"
    
  done < <(
    gh_call gh repo list "$ORG" \
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
  echo "Finished deleting issues."
  echo ""
  echo "Delete Stats for Organization: $ORG"
  echo "----------------------------------------------------------------"
  echo "Total Repositories: $total_repos"
  echo "Archived Repositories: $archived_count"
  echo "Repositories with No Issues: $skipped_no_issues"
  echo "Issues Deleted: $deleted_count"
  echo "Failed Deletes: $failed_delete_count"
  echo "CSV Output: $OUTPUT_FILE"
  echo "----------------------------------------------------------------"
  
  exit 0
fi

# CREATE MODE 
total_count=0
iac_count=0
archived_count=0
created_count=0
skipped_existing_count=0
skipped_archived_count=0
skipped_issues_perm_count=0
failed_count=0

printf "repo,primary_language,issues_enabled,is_archived,action\n" >> "$OUTPUT_FILE"

# 1. Fetch repositories as TSV
# 2. Iterate using a while loop
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
    echo "$name_with_owner,$primary_lang,$issues_enabled,$is_archived,skipped_archived" >> "$OUTPUT_FILE"
    continue
  fi

  WAS_DISABLED=false
  if [[ "$issues_enabled" == "false" ]]; then
    echo "Issues are disabled. Temporarily enabling..."
    if ! gh_call gh repo edit "$name_with_owner" --enable-issues >/dev/null; then
      echo "Could not enable issues. Skipping issue creation."
      skipped_issues_perm_count=$((skipped_issues_perm_count + 1))
      echo "$name_with_owner,$primary_lang,$issues_enabled,$is_archived,skipped_cant_enable_issues" >> "$OUTPUT_FILE"
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

    echo "$name_with_owner,$primary_lang,$issues_enabled,$is_archived,skipped_existing_issue" >> "$OUTPUT_FILE"
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

  echo "$name_with_owner,$primary_lang,$issues_enabled,$is_archived,$action" >> "$OUTPUT_FILE"

done < <(
  gh_call gh repo list "$ORG" \
    --limit "$REPO_LIST_LIMIT" \
    --json nameWithOwner,hasIssuesEnabled,primaryLanguage,isArchived \
    --jq '.[] | [
      .nameWithOwner,
      (.hasIssuesEnabled|tostring),
      (.primaryLanguage.name // "N/A"),
      (.isArchived|tostring)
    ] | @tsv'
)

echo "Finished processing all repositories."
echo
echo "Repository Stats for Organization: $ORG"
echo "----------------------------------------------------------------"
echo "Total Repositories: $total_count"
echo "Archived Repositories: $archived_count"
echo "Skipped Archived: $skipped_archived_count"
echo "IaC Repositories: $iac_count (Primary language: HCL/Bicep)"
echo "Issues Permission Skips: $skipped_issues_perm_count"
echo "Skipped Existing Issues: $skipped_existing_count"
echo "Created Issues: $created_count"
echo "Failed Creates: $failed_count"
echo "CSV Output: $OUTPUT_FILE"
echo "----------------------------------------------------------------"