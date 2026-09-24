#!/bin/zsh
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

test_dir=${0:A:h}
bridge_dir=${test_dir:h}
scripts_dir=${bridge_dir:h}
low_entrypoint=${bridge_dir}/run.sh
executor=${scripts_dir}/personal-scheduler.sh
protocol=${scripts_dir}/protocol-v1.json
export PERSONAL_SCHEDULER_INTERNAL_TOKEN="personal-scheduler-executor-v1-9F2D7B1C"

app_path=$($bridge_dir/build.sh)
binary_path="$app_path/Contents/MacOS/personal-scheduler-eventkit"
mtime_before=$(/usr/bin/stat -f %m "$binary_path")
cdhash_before=$(/usr/bin/codesign -dvvv "$app_path" 2>&1 | /usr/bin/awk -F= '/^CDHash=/{print $2; exit}')
second_app_path=$($bridge_dir/build.sh)
mtime_after=$(/usr/bin/stat -f %m "$binary_path")
cdhash_after=$(/usr/bin/codesign -dvvv "$app_path" 2>&1 | /usr/bin/awk -F= '/^CDHash=/{print $2; exit}')
[[ "$app_path" == "$second_app_path" && "$mtime_before" == "$mtime_after" && "$cdhash_before" == "$cdhash_after" ]]

/usr/bin/plutil -lint "$app_path/Contents/Info.plist" >/dev/null
/usr/bin/codesign --verify --strict "$app_path"
minos=$(/usr/bin/otool -l "$binary_path" | /usr/bin/awk '/LC_BUILD_VERSION/{found=1} found && /minos/{print $2; exit}')
[[ "$minos" == "14.0" ]]
/usr/bin/lipo "$binary_path" -verify_arch "$(/usr/bin/uname -m)"
/bin/zsh -n "$scripts_dir/personal-scheduler.sh" "$low_entrypoint" "$bridge_dir/build.sh" "$bridge_dir/run.sh"
/usr/bin/python3 -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$scripts_dir/scheduler_executor.py"
/usr/bin/python3 "$test_dir/test_executor.py" >/dev/null

doctor_output=$($low_entrypoint doctor)
self_test_output=$($executor self-test)
status_output=$($low_entrypoint status)

set +e
direct_output=$(PERSONAL_SCHEDULER_INTERNAL_TOKEN="" $low_entrypoint doctor 2>/dev/null)
direct_status=$?
set -e
(( direct_status != 0 ))

invalid_managed='{"entity":"reminder","source_id":"unused","container_id":"unused","confirm_private_container":true,"managed":{"schema_version":1,"schedule_id":"PS-11111111-1111-4111-8111-111111111111","entity":"reminder","role":"action"},"payload":{"title":"test","due":{"kind":"none"},"priority":0},"dry_run":true}'
set +e
invalid_managed_output=$(print -r -- "$invalid_managed" | $low_entrypoint items create 2>/dev/null)
invalid_managed_status=$?
set -e
(( invalid_managed_status != 0 ))

recurrence_request='{"entity":"reminder","source_id":"unused","container_id":"unused","confirm_private_container":true,"managed":{"schema_version":1,"schedule_id":"PS-11111111-1111-4111-8111-111111111111","entity":"reminder","role":"task"},"payload":{"title":"test","due":{"kind":"none"},"priority":0,"recurrence":{"frequency":"daily"}},"dry_run":true}'
set +e
recurrence_output=$(print -r -- "$recurrence_request" | $low_entrypoint items create 2>/dev/null)
recurrence_status=$?
set -e
(( recurrence_status != 0 ))

missing_dry_run_request='{"entity":"reminder","source_id":"unused","container_id":"unused","confirm_private_container":true,"managed":{"schema_version":1,"schedule_id":"PS-11111111-1111-4111-8111-111111111111","entity":"reminder","role":"task"},"payload":{"title":"test","due":{"kind":"none"},"priority":0}}'
set +e
missing_dry_run_output=$(print -r -- "$missing_dry_run_request" | $low_entrypoint items create 2>/dev/null)
missing_dry_run_status=$?
set -e
(( missing_dry_run_status != 0 ))

missing_expected_store_request='{"entity":"reminder","source_id":"unused","container_id":"unused","confirm_private_container":true,"managed":{"schema_version":1,"schedule_id":"PS-11111111-1111-4111-8111-111111111111","entity":"reminder","role":"task"},"payload":{"title":"test","due":{"kind":"none"},"priority":0},"dry_run":false}'
set +e
missing_expected_store_output=$(print -r -- "$missing_expected_store_request" | $low_entrypoint items create 2>/dev/null)
missing_expected_store_status=$?
set -e
(( missing_expected_store_status != 0 ))

empty_location_request='{"entity":"event","source_id":"unused","container_id":"unused","confirm_private_container":true,"managed":{"schema_version":1,"schedule_id":"PS-11111111-1111-4111-8111-111111111111","entity":"event","role":"appointment"},"payload":{"title":"test","location":"","time":{"kind":"timed","start_at":"2026-09-08T14:00:00+08:00","end_at":"2026-09-08T15:00:00+08:00","timezone":"Asia/Shanghai"},"alarms":[]},"search_window":{"start_at":"2026-09-08T00:00:00+08:00","end_at":"2026-09-09T00:00:00+08:00"},"dry_run":true}'
set +e
empty_location_output=$(print -r -- "$empty_location_request" | $low_entrypoint items create 2>/dev/null)
empty_location_status=$?
set -e
(( empty_location_status != 0 ))

temp_dir=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/personal-scheduler-bundle-test.XXXXXX")
trap '/bin/rm -rf "$temp_dir"' EXIT
/bin/cp "$binary_path" "$temp_dir/bare-binary"
set +e
bare_output=$(print -r -- '{"entity":"reminder","confirmed":true}' | PERSONAL_SCHEDULER_SWIFT_TOKEN="personal-scheduler-swift-v1-4C8E1A73" "$temp_dir/bare-binary" authorize 2>/dev/null)
bare_status=$?
set -e
(( bare_status != 0 ))

DOCTOR_OUTPUT="$doctor_output" SELF_TEST_OUTPUT="$self_test_output" STATUS_OUTPUT="$status_output" DIRECT_OUTPUT="$direct_output" INVALID_MANAGED_OUTPUT="$invalid_managed_output" RECURRENCE_OUTPUT="$recurrence_output" MISSING_DRY_RUN_OUTPUT="$missing_dry_run_output" MISSING_EXPECTED_STORE_OUTPUT="$missing_expected_store_output" EMPTY_LOCATION_OUTPUT="$empty_location_output" BARE_OUTPUT="$bare_output" PROTOCOL_PATH="$protocol" SAMPLE_DIR="$bridge_dir/samples" SOURCE_PATH="$bridge_dir/Sources/main.swift" EXECUTOR_PATH="$scripts_dir/scheduler_executor.py" /usr/bin/python3 - <<'PY'
import importlib.util
import json
import os

doctor = json.loads(os.environ["DOCTOR_OUTPUT"])
self_test = json.loads(os.environ["SELF_TEST_OUTPUT"])
status = json.loads(os.environ["STATUS_OUTPUT"])
direct = json.loads(os.environ["DIRECT_OUTPUT"])
invalid_managed = json.loads(os.environ["INVALID_MANAGED_OUTPUT"])
recurrence = json.loads(os.environ["RECURRENCE_OUTPUT"])
missing_dry_run = json.loads(os.environ["MISSING_DRY_RUN_OUTPUT"])
missing_expected_store = json.loads(os.environ["MISSING_EXPECTED_STORE_OUTPUT"])
empty_location = json.loads(os.environ["EMPTY_LOCATION_OUTPUT"])
bare = json.loads(os.environ["BARE_OUTPUT"])
protocol = json.load(open(os.environ["PROTOCOL_PATH"], encoding="utf-8"))

assert doctor["ok"] is True
assert doctor["bundle"]["identity_matches"] is True
assert doctor["event_store"]["data_accessed"] is False
assert doctor["mutated"] is False
assert status["ok"] is True and status["command"] == "doctor"
assert direct["error"]["code"] == "internal_entrypoint_required"
assert self_test["ok"] is True
assert self_test["eventkit_data_accessed"] is False
assert self_test["production_state_accessed"] is False
assert self_test["mutated"] is False
spec = importlib.util.spec_from_file_location("scheduler_hash_check", os.environ["EXECUTOR_PATH"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
reminder_request = {"payload": {"title": "hash vector reminder", "due": {"kind": "date_time", "at": "2026-09-08T20:00:00+08:00", "timezone": "Asia/Shanghai"}, "priority": 5}}
event_request = {"payload": {"title": "hash vector event", "location": "Room 101", "time": {"kind": "timed", "start_at": "2026-09-08T14:00:00+08:00", "end_at": "2026-09-08T15:00:00+08:00", "timezone": "Asia/Shanghai"}, "alarms": [{"minutes_before": 30}, {"minutes_before": 10}]}}
vectors = self_test["bridge"]["content_hash_vectors"]
assert vectors["reminder"] == module.desired_content_hash("reminder", reminder_request)
assert vectors["event"] == module.desired_content_hash("event", event_request)
assert invalid_managed["error"]["code"] == "validation_error"
assert recurrence["error"]["code"] == "validation_error"
assert missing_dry_run["error"]["code"] == "validation_error"
assert missing_expected_store["error"]["code"] == "validation_error"
assert empty_location["error"]["code"] == "validation_error"
assert bare["error"]["code"] == "bundle_identity_mismatch"

assert protocol["protocol_version"] == 1
assert protocol["managed_metadata_schema_version"] == 1
assert protocol["state_schema_version"] == 2
assert protocol["state"]["path"].endswith("/state-v2.json")
assert "fail closed" in protocol["state"]["legacy_v1_policy"]
assert protocol["managed_marker"] == "[personal-scheduler:v1]"
assert protocol["managed"]["reminder_roles"] == ["task", "deadline"]
assert protocol["managed"]["event_roles"] == ["appointment", "commitment", "time-block"]
assert "container deletion" in protocol["unsupported"]
assert "expected_event_store_id" in protocol["mutation_protocol"]["actual_store_handshake"]

sample_dir = os.environ["SAMPLE_DIR"]
samples = [json.load(open(os.path.join(sample_dir, name), encoding="utf-8")) for name in ("reminder-create.json", "event-create.json", "claim.json")]
assert all(sample["dry_run"] is True for sample in samples)
assert all(sample["operation_id"].startswith("OP-") for sample in samples)
assert all(sample["managed"]["schema_version"] == 1 for sample in samples)
assert all(sample["managed"]["schedule_id"].startswith("PS-") for sample in samples)

source = open(os.environ["SOURCE_PATH"], encoding="utf-8").read()
for forbidden in ("GoalPlannerEventKitBridge", "goal_path", "obsidian_url", "projection_id"):
    assert forbidden not in source
assert source.count("[goal-planner:v2]") == 2  # classifier + pure self-test only
assert "notes_sha256" not in source
assert 'optionalBool(input, "dry_run"' not in source
assert source.count('requiredBool(input, "dry_run")') == 9
assert source.count("let expectedStoreID = try mutationExpectedStoreID") == 9
assert source.count("try requireExpectedStoreID(expectedStoreID, context: context)") == 18
assert source.count("expectedNotes") >= 12
assert 'mismatches.append("notes")' in source
assert source.count("if event.structuredLocation != nil {") >= 2
PY

print "Smoke tests passed. No Calendar or Reminders data was read or changed."
