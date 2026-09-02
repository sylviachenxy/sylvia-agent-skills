#!/bin/zsh
set -euo pipefail
umask 077

script_dir=${0:A:h}
app_path="$script_dir/.build/WeeklyReviewEventKitReader.app"
binary_path="$app_path/Contents/MacOS/weekly-review-eventkit-reader"
expected_run_token="weekly-review-eventkit-run-v1-DA2194C7"

emit_failure() {
  local code="$1"
  local message="$2"
  local native_status="${3:-}"
  if [[ -n "$native_status" ]]; then
    print -r -- "{\"ok\":false,\"reader_version\":\"1.0.0\",\"protocol_version\":1,\"eventkit_data_mutated\":false,\"error\":{\"code\":\"$code\",\"message\":\"$message\",\"details\":{\"native_exit_status\":$native_status}}}"
  else
    print -r -- "{\"ok\":false,\"reader_version\":\"1.0.0\",\"protocol_version\":1,\"eventkit_data_mutated\":false,\"error\":{\"code\":\"$code\",\"message\":\"$message\"}}"
  fi
}

if [[ "${WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN:-}" != "$expected_run_token" ]]; then
  print -r -- '{"ok":false,"reader_version":"1.0.0","protocol_version":1,"eventkit_data_mutated":false,"error":{"code":"internal_entrypoint_required","message":"Use scripts/apple-eventkit-reader.sh; the native runner is internal."}}'
  exit 2
fi
unset WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN
export WEEKLY_REVIEW_EVENTKIT_SWIFT_TOKEN="weekly-review-eventkit-swift-v1-87B451D2"

set +e
"$script_dir/build.sh" >/dev/null 2>/dev/null
build_status=$?
set -e
if (( build_status != 0 )); then
  emit_failure "reader_build_failed" "The native EventKit reader could not be built or validated." "$build_status"
  exit 2
fi

command_name="$*"
case "$command_name" in
  capabilities|doctor|self-test)
    requires_input=false
    timeout_base=20
    ;;
  authorize)
    requires_input=true
    timeout_base=130
    ;;
  *)
    requires_input=true
    timeout_base=60
    ;;
esac

cache_base=$(/usr/bin/getconf DARWIN_USER_CACHE_DIR 2>/dev/null || true)
if [[ -z "$cache_base" ]]; then
  emit_failure "unsafe_runtime" "The private runtime base directory is unavailable."
  exit 2
fi
runtime_root="${cache_base}io.github.sylviachenxy.sylvia-agent-skills.weekly-review-eventkit-reader/runtime"
if [[ -L "$runtime_root" ]]; then
  emit_failure "unsafe_runtime" "The private runtime directory is a symbolic link."
  exit 2
fi
if ! /bin/mkdir -p "$runtime_root" 2>/dev/null || ! /bin/chmod 700 "$runtime_root" 2>/dev/null; then
  emit_failure "unsafe_runtime" "The private runtime directory could not be prepared."
  exit 2
fi
runtime_owner=$(/usr/bin/stat -f %u "$runtime_root" 2>/dev/null || true)
runtime_mode=$(/usr/bin/stat -f %Lp "$runtime_root" 2>/dev/null || true)
if [[ "$runtime_owner" != "$(/usr/bin/id -u)" || "$runtime_mode" != "700" ]]; then
  emit_failure "unsafe_runtime" "The private runtime directory failed owner or 0700 mode validation."
  exit 2
fi
/usr/bin/find "$runtime_root" -mindepth 1 -maxdepth 1 -type d -name 'run.*' -user "$(/usr/bin/id -u)" -mmin +60 -exec /bin/rm -rf {} + 2>/dev/null || true
runtime_dir=$(/usr/bin/mktemp -d "$runtime_root/run.XXXXXX" 2>/dev/null || true)
if [[ -z "$runtime_dir" || ! -d "$runtime_dir" ]]; then
  emit_failure "unsafe_runtime" "A private runtime directory could not be created."
  exit 2
fi
input_path="$runtime_dir/input.json"
stdout_path="$runtime_dir/stdout.json"
stderr_path="$runtime_dir/stderr.log"
normalized_path="$runtime_dir/normalized.json"
timeout_marker="$runtime_dir/timed-out"
worker_pid=""
watcher_pid=""

cleanup() {
  if [[ -n "$watcher_pid" ]]; then kill "$watcher_pid" 2>/dev/null || true; fi
  if [[ -n "$worker_pid" ]]; then kill "$worker_pid" 2>/dev/null || true; fi
  /bin/rm -rf "$runtime_dir" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

if [[ "$requires_input" == true ]]; then
  if ! /usr/bin/head -c 1048577 > "$input_path" 2>/dev/null; then
    emit_failure "invalid_json" "stdin could not be read safely."
    exit 2
  fi
  input_size=$(/usr/bin/stat -f %z "$input_path" 2>/dev/null || true)
  if [[ ! "$input_size" =~ '^[0-9]+$' ]]; then
    emit_failure "unsafe_runtime" "The private input file could not be validated."
    exit 2
  fi
  if (( input_size > 1048576 )); then
    emit_failure "invalid_json" "stdin exceeds the 1 MiB input limit."
    exit 2
  fi
  requested_timeout=$(/usr/bin/plutil -extract timeout_seconds raw -o - "$input_path" 2>/dev/null || true)
  if [[ "$requested_timeout" =~ '^[0-9]+$' ]]; then timeout_base=$requested_timeout; fi
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
  print -r -- "{\"ok\":false,\"reader_version\":\"1.0.0\",\"protocol_version\":1,\"eventkit_data_mutated\":false,\"error\":{\"code\":\"operation_timeout\",\"message\":\"The read-only EventKit operation exceeded the outer watchdog and was terminated.\",\"details\":{\"outer_timeout_seconds\":$outer_timeout}}}"
  exit 2
fi

stdout_size=$(/usr/bin/stat -f %z "$stdout_path" 2>/dev/null || true)
if [[ ! "$stdout_size" =~ '^[0-9]+$' || "$stdout_size" == "0" ]]; then
  emit_failure "reader_protocol_error" "The native reader returned no valid protocol output." "$worker_status"
  exit 2
fi
if (( stdout_size > 4194304 )); then
  emit_failure "output_limit_exceeded" "The native reader output exceeded the 4 MiB protocol limit." "$worker_status"
  exit 2
fi

set +e
/usr/bin/python3 -I "$script_dir/validate-envelope.py" "$stdout_path" "$worker_status" > "$normalized_path" 2>/dev/null
validation_status=$?
set -e
normalized_size=$(/usr/bin/stat -f %z "$normalized_path" 2>/dev/null || true)
if [[ "$validation_status" != "0" && "$validation_status" != "2" ]] \
    || [[ ! "$normalized_size" =~ '^[0-9]+$' || "$normalized_size" == "0" ]] \
    || (( normalized_size > 4194304 )); then
  emit_failure "reader_protocol_error" "The native reader returned malformed or inconsistent JSON protocol output." "$worker_status"
  exit 2
fi

/bin/cat "$normalized_path"
exit "$validation_status"
