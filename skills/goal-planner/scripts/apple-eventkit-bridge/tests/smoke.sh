#!/bin/zsh
set -euo pipefail

test_dir=${0:A:h}
bridge_dir=${test_dir:h}
entrypoint=${bridge_dir:h}/apple-eventkit-bridge.sh

app_path=$($bridge_dir/build.sh)
binary_path="$app_path/Contents/MacOS/goal-planner-eventkit"
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

doctor_output=$($entrypoint doctor)
self_test_output=$($entrypoint self-test)
status_output=$($entrypoint status)
mtime_after_runs=$(/usr/bin/stat -f %m "$binary_path")
cdhash_after_runs=$(/usr/bin/codesign -dvvv "$app_path" 2>&1 | /usr/bin/awk -F= '/^CDHash=/{print $2; exit}')
[[ "$mtime_after_runs" == "$mtime_after" && "$cdhash_after_runs" == "$cdhash_after" ]]

strict_request='{"entity":"reminder","source_id":"unused","container_id":"unused","managed":{"schema_version":2,"goal_id":"G-2026-001","projection_id":"G-2026-001-R001","action_id":"G-2026-001-A001","role":"action","goal_path":"Goals/G-2026-001/G-2026-001.md","obsidian_url":"obsidian://open?vault=Example&file=Goal"},"payload":{"title":"test","due":{"kind":"none"},"priority":0,"recurrence":{"frequency":"daily"}}}'
set +e
strict_output=$(print -r -- "$strict_request" | $entrypoint items create 2>/dev/null)
strict_status=$?
set -e
(( strict_status != 0 ))

all_day_timezone_request='{"entity":"event","source_id":"unused","container_id":"unused","managed":{"schema_version":2,"goal_id":"G-2026-001","projection_id":"G-2026-001-E001","action_id":"G-2026-001-A001","role":"work-block","goal_path":"Goals/G-2026-001/G-2026-001.md","obsidian_url":"obsidian://open?vault=Example&file=Goal"},"payload":{"title":"test","location":null,"time":{"kind":"all_day","start_date":"2026-09-07","end_date_exclusive":"2026-09-08","timezone":"Asia/Shanghai"},"alarms":[]},"search_window":{"start_at":"2026-09-01T00:00:00+08:00","end_at":"2026-10-01T00:00:00+08:00"}}'
set +e
all_day_timezone_output=$(print -r -- "$all_day_timezone_request" | $entrypoint items create 2>/dev/null)
all_day_timezone_status=$?
set -e
(( all_day_timezone_status != 0 ))

DOCTOR_OUTPUT="$doctor_output" SELF_TEST_OUTPUT="$self_test_output" STATUS_OUTPUT="$status_output" STRICT_OUTPUT="$strict_output" ALL_DAY_TIMEZONE_OUTPUT="$all_day_timezone_output" PROTOCOL_PATH="$bridge_dir/protocol-v1.json" /usr/bin/python3 - <<'PY'
import json
import os

doctor = json.loads(os.environ["DOCTOR_OUTPUT"])
self_test = json.loads(os.environ["SELF_TEST_OUTPUT"])
status = json.loads(os.environ["STATUS_OUTPUT"])
strict = json.loads(os.environ["STRICT_OUTPUT"])
all_day_timezone = json.loads(os.environ["ALL_DAY_TIMEZONE_OUTPUT"])
protocol_path = os.environ["PROTOCOL_PATH"]
protocol = json.load(open(protocol_path, encoding="utf-8"))
sample_dir = os.path.join(os.path.dirname(protocol_path), "samples")
samples = [
    json.load(open(os.path.join(sample_dir, name), encoding="utf-8"))
    for name in ("reminder-create.json", "event-create.json", "event-all-day-create.json")
]

assert doctor["ok"] is True
assert doctor["protocol_version"] == 1
assert doctor["managed_metadata_schema_version"] == 2
assert doctor["command"] == "doctor"
assert doctor["bundle"]["identity_matches"] is True
assert doctor["event_store"]["data_accessed"] is False
assert doctor["mutated"] is False
assert self_test["ok"] is True
assert all(item["passed"] for item in self_test["tests"])
assert self_test["eventkit_data_accessed"] is False
assert self_test["mutated"] is False
assert status["ok"] is True and status["command"] == "doctor"
assert strict["ok"] is False
assert strict["error"]["code"] == "validation_error"
assert all_day_timezone["ok"] is False
assert all_day_timezone["error"]["code"] == "validation_error"
assert protocol["protocol_version"] == 1
assert protocol["managed_metadata_schema_version"] == 2
assert protocol["managed"]["schema_version"] == 2
assert protocol["managed"]["reminder_roles"] == ["action", "check-in"]
assert all(sample["managed"]["schema_version"] == 2 for sample in samples)
all_day_sample = next(
    sample
    for sample in samples
    if sample["entity"] == "event" and sample["payload"]["time"]["kind"] == "all_day"
)
assert "timezone" not in all_day_sample["payload"]["time"]
assert all(sample["dry_run"] is True for sample in samples)
PY

print "Smoke tests passed. No Calendar or Reminders data was read or changed."
