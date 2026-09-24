#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
app_path="$script_dir/.build/GoalPlannerEventKitBridge.app"
binary_path="$app_path/Contents/MacOS/goal-planner-eventkit"

# build.sh is a cheap no-op for a fresh cache and is the single authority for
# source freshness, signature, deployment target, and native CPU architecture.
"$script_dir/build.sh" >/dev/null

command_name="$*"
runtime_dir=$(/usr/bin/mktemp -d "$script_dir/.build/run.XXXXXX")
input_path="$runtime_dir/input.json"
stdout_path="$runtime_dir/stdout.json"
stderr_path="$runtime_dir/stderr.log"
timeout_marker="$runtime_dir/timed-out"
worker_pid=""
watcher_pid=""

cleanup() {
  if [[ -n "$watcher_pid" ]]; then
    kill "$watcher_pid" 2>/dev/null || true
  fi
  if [[ -n "$worker_pid" ]]; then
    kill "$worker_pid" 2>/dev/null || true
  fi
  /bin/rm -rf "$runtime_dir"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

case "$command_name" in
  doctor|status|self-test)
    requires_input=false
    timeout_base=20
    ;;
  authorize)
    requires_input=true
    timeout_base=120
    ;;
  "items find"|"items create"|"items patch"|"items delete"|"reminders complete")
    requires_input=true
    timeout_base=20
    ;;
  *)
    requires_input=true
    timeout_base=50
    ;;
esac

if [[ "$requires_input" == true ]]; then
  /bin/cat > "$input_path"
  requested_timeout=$(/usr/bin/plutil -extract timeout_seconds raw -o - "$input_path" 2>/dev/null || true)
  if [[ "$requested_timeout" =~ '^[0-9]+$' ]]; then
    timeout_base=$requested_timeout
  fi
  (( timeout_base > 300 )) && timeout_base=300
  (( timeout_base < 1 )) && timeout_base=1
  "$binary_path" "$@" < "$input_path" > "$stdout_path" 2> "$stderr_path" &
else
  "$binary_path" "$@" < /dev/null > "$stdout_path" 2> "$stderr_path" &
fi
worker_pid=$!
outer_timeout=$(( timeout_base + 10 ))

(
  deadline=$(( SECONDS + outer_timeout ))
  while kill -0 "$worker_pid" 2>/dev/null; do
    if (( SECONDS >= deadline )); then
      : > "$timeout_marker"
      kill -TERM "$worker_pid" 2>/dev/null || true
      /bin/sleep 2
      kill -KILL "$worker_pid" 2>/dev/null || true
      exit 0
    fi
    /bin/sleep 0.1
  done
) &
watcher_pid=$!

set +e
wait "$worker_pid"
worker_status=$?
set -e
worker_pid=""
wait "$watcher_pid" 2>/dev/null || true
watcher_pid=""

if [[ -s "$stderr_path" ]]; then
  /bin/cat "$stderr_path" >&2
fi

if [[ -e "$timeout_marker" ]]; then
  case "$command_name" in
    "containers create"|"items create"|"items patch"|"items delete"|"reminders complete")
      error_code="operation_timeout_outcome_unknown"
      error_message="The EventKit operation exceeded the outer watchdog. A mutation may have committed; reconcile by listing/finding/getting before any retry."
      ;;
    *)
      error_code="operation_timeout"
      error_message="The EventKit operation exceeded the outer watchdog and was terminated."
      ;;
  esac
  print -r -- "{\"ok\":false,\"bridge_version\":\"1.0.0\",\"protocol_version\":1,\"managed_metadata_schema_version\":2,\"error\":{\"code\":\"$error_code\",\"message\":\"$error_message\",\"details\":{\"outer_timeout_seconds\":$outer_timeout}}}"
  exit 2
fi

/bin/cat "$stdout_path"
exit "$worker_status"
