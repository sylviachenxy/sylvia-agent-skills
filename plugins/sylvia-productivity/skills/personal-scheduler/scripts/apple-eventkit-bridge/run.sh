#!/bin/zsh
set -euo pipefail
umask 077

script_dir=${0:A:h}
app_path="$script_dir/.build/PersonalSchedulerEventKitBridge.app"
binary_path="$app_path/Contents/MacOS/personal-scheduler-eventkit"
expected_internal_token="personal-scheduler-executor-v1-9F2D7B1C"

if [[ "${PERSONAL_SCHEDULER_INTERNAL_TOKEN:-}" != "$expected_internal_token" ]]; then
  print -r -- '{"ok":false,"bridge_version":"1.0.0","protocol_version":1,"managed_metadata_schema_version":1,"error":{"code":"internal_entrypoint_required","message":"Use scripts/personal-scheduler.sh; the EventKit runner is internal."}}'
  exit 2
fi
unset PERSONAL_SCHEDULER_INTERNAL_TOKEN
export PERSONAL_SCHEDULER_SWIFT_TOKEN="personal-scheduler-swift-v1-4C8E1A73"

# build.sh is a cheap no-op for a fresh cache and is the single authority for
# source freshness, signature, deployment target, and native CPU architecture.
"$script_dir/build.sh" >/dev/null

command_name="$*"
cache_base=$(/usr/bin/getconf DARWIN_USER_CACHE_DIR)
runtime_root="${cache_base}io.github.sylviachenxy.sylvia-agent-skills.personal-scheduler-eventkit/runtime"
if [[ -L "$runtime_root" ]]; then
  print -r -- '{"ok":false,"bridge_version":"1.0.0","protocol_version":1,"managed_metadata_schema_version":1,"error":{"code":"unsafe_runtime","message":"The private runtime directory is a symbolic link."}}'
  exit 2
fi
/bin/mkdir -p "$runtime_root"
/bin/chmod 700 "$runtime_root"
runtime_owner=$(/usr/bin/stat -f %u "$runtime_root")
runtime_mode=$(/usr/bin/stat -f %Lp "$runtime_root")
if [[ "$runtime_owner" != "$(/usr/bin/id -u)" || "$runtime_mode" != "700" ]]; then
  print -r -- '{"ok":false,"bridge_version":"1.0.0","protocol_version":1,"managed_metadata_schema_version":1,"error":{"code":"unsafe_runtime","message":"The private runtime directory failed owner or 0700 mode validation."}}'
  exit 2
fi
/usr/bin/find "$runtime_root" -mindepth 1 -maxdepth 1 -type d -name 'run.*' -user "$(/usr/bin/id -u)" -mmin +60 -exec /bin/rm -rf {} + 2>/dev/null || true
runtime_dir=$(/usr/bin/mktemp -d "$runtime_root/run.XXXXXX")
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
  "items find"|"items create"|"items patch"|"items delete"|"items claim"|"reminders complete"|"unmanaged items patch"|"unmanaged reminders complete"|"unmanaged items delete")
    requires_input=true
    timeout_base=20
    ;;
  *)
    requires_input=true
    timeout_base=50
    ;;
esac

if [[ "$requires_input" == true ]]; then
  /usr/bin/head -c 1048577 > "$input_path"
  input_size=$(/usr/bin/stat -f %z "$input_path")
  if (( input_size > 1048576 )); then
    print -r -- '{"ok":false,"bridge_version":"1.0.0","protocol_version":1,"managed_metadata_schema_version":1,"error":{"code":"invalid_json","message":"stdin exceeds the 1 MiB input limit."}}'
    exit 2
  fi
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

if [[ -e "$timeout_marker" ]]; then
  case "$command_name" in
    "containers create"|"items create"|"items patch"|"items delete"|"items claim"|"reminders complete"|"unmanaged items patch"|"unmanaged reminders complete"|"unmanaged items delete")
      error_code="operation_timeout_outcome_unknown"
      error_message="The EventKit operation exceeded the outer watchdog. A mutation may have committed; reconcile by listing/finding/getting before any retry."
      ;;
    *)
      error_code="operation_timeout"
      error_message="The EventKit operation exceeded the outer watchdog and was terminated."
      ;;
  esac
  print -r -- "{\"ok\":false,\"bridge_version\":\"1.0.0\",\"protocol_version\":1,\"managed_metadata_schema_version\":1,\"error\":{\"code\":\"$error_code\",\"message\":\"$error_message\",\"details\":{\"outer_timeout_seconds\":$outer_timeout}}}"
  exit 2
fi

stdout_size=$(/usr/bin/stat -f %z "$stdout_path")
if (( stdout_size > 1048576 )); then
  print -r -- '{"ok":false,"bridge_version":"1.0.0","protocol_version":1,"managed_metadata_schema_version":1,"error":{"code":"bridge_protocol_error","message":"The internal bridge output exceeded 1 MiB."}}'
  exit 2
fi
if [[ -s "$stderr_path" ]]; then
  stderr_size=$(/usr/bin/stat -f %z "$stderr_path")
  print -u2 -- "Internal EventKit diagnostics suppressed (${stderr_size} bytes)."
fi
/bin/cat "$stdout_path"
exit "$worker_status"
