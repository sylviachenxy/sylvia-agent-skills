#!/bin/zsh
set -euo pipefail
umask 077

script_dir=${0:A:h}
export WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN="weekly-review-eventkit-run-v1-DA2194C7"
exec "$script_dir/apple-eventkit-reader/run.sh" "$@"
