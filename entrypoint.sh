#!/bin/bash
set -eu

script_dir="$(dirname "$0")"
cd $script_dir

pull_request_id=$(cat "$GITHUB_EVENT_PATH" | jq 'if (.issue.number != null) then .issue.number else .number end')
branch_name="pull_request_branch"

if [ $pull_request_id == "null" ]; then
  echo "Could not find a pull request ID. Is this a pull request?"
  exit 1
fi

maintainer=${GITHUB_REPOSITORY%/*}
eval git clone "https://${maintainer}:${INPUT_GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" ${GITHUB_REPOSITORY}
cd $GITHUB_REPOSITORY
eval git config remote.origin.fetch +refs/heads/*:refs/remotes/origin/*
eval git fetch origin pull/$pull_request_id/head:$branch_name

compare_with_base=$(echo "${INPUT_COMPARE_WITH_BASE:-false}" | tr '[:upper:]' '[:lower:]')
base_results_json=""

if [ "$compare_with_base" = "true" ]; then
  # Determine the base ref
  base_ref="${INPUT_BASE_REF:-}"
  if [ -z "$base_ref" ]; then
    base_ref=$(cat "$GITHUB_EVENT_PATH" | jq -r '.pull_request.base.ref // "master"')
  fi
  echo "Comparing against base ref: $base_ref"

  # Checkout base branch and run detection
  eval git checkout "origin/$base_ref" 2>/dev/null || eval git checkout "$base_ref" 2>/dev/null || {
    echo "Warning: could not checkout base ref '$base_ref', skipping comparison"
    compare_with_base="false"
  }

  if [ "$compare_with_base" = "true" ]; then
    echo "Running detection on base branch..."
    base_results_json=$(python3 /action/run_action.py --latest-head "$base_ref" --pull-request-id "$pull_request_id" --json-only 2>/dev/null) || {
      echo "Warning: base branch detection failed, skipping comparison"
      base_results_json=""
    }
  fi
fi

# Checkout PR branch and run detection
eval git checkout $branch_name
latest_head=$(git rev-parse HEAD)

extra_args=""
if [ -n "$base_results_json" ]; then
  # Write base results to a temp file for the Python script to read
  echo "$base_results_json" > /tmp/base_results.json
  extra_args="--base-results /tmp/base_results.json"
fi

eval python3 /action/run_action.py --latest-head $latest_head --pull-request-id $pull_request_id $extra_args
