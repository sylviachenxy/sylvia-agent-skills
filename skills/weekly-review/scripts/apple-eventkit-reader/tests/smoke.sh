#!/bin/zsh
set -euo pipefail
umask 077

test_dir=${0:A:h}
reader_dir=${test_dir:h}
scripts_dir=${reader_dir:h}
entrypoint="$scripts_dir/apple-eventkit-reader.sh"
binary_path="$reader_dir/.build/WeeklyReviewEventKitReader.app/Contents/MacOS/weekly-review-eventkit-reader"
fixture_dir="$test_dir/fixtures"
fixture_root=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/weekly-review-eventkit-transport.XXXXXX")

cleanup() {
  /bin/rm -rf "$fixture_root" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

prepare_transport_case() {
  local case_name="$1"
  local build_fixture="$2"
  local worker_fixture="${3:-}"
  local case_root="$fixture_root/$case_name"
  /bin/mkdir -p "$case_root/.build/WeeklyReviewEventKitReader.app/Contents/MacOS"
  /bin/cp "$reader_dir/run.sh" "$case_root/run.sh"
  /bin/cp "$reader_dir/validate-envelope.py" "$case_root/validate-envelope.py"
  /bin/cp "$fixture_dir/$build_fixture" "$case_root/build.sh"
  if [[ -n "$worker_fixture" ]]; then
    /bin/cp "$fixture_dir/$worker_fixture" "$case_root/.build/WeeklyReviewEventKitReader.app/Contents/MacOS/weekly-review-eventkit-reader"
  fi
  /bin/chmod 755 "$case_root/run.sh" "$case_root/build.sh"
  if [[ -n "$worker_fixture" ]]; then
    /bin/chmod 755 "$case_root/.build/WeeklyReviewEventKitReader.app/Contents/MacOS/weekly-review-eventkit-reader"
  fi
  print -r -- "$case_root"
}

"$reader_dir/build.sh" >/dev/null
/usr/bin/python3 "$test_dir/test_offline.py"

capabilities_output=$($entrypoint capabilities)
doctor_output=$($entrypoint doctor)
self_test_output=$($entrypoint self-test)

set +e
false_authorize_output=$(print -r -- '{"entity":"event","confirmed":false}' | $entrypoint authorize 2>/dev/null)
false_authorize_status=$?
empty_events_output=$(print -r -- '{"calendar_ids":[],"window":{"start_at":"2026-08-31T00:00:00+08:00","end_at":"2026-09-07T00:00:00+08:00"},"detail":"summary","limit":200}' | $entrypoint events list 2>/dev/null)
empty_events_status=$?
oversized_window_output=$(print -r -- '{"calendar_ids":["UNUSED"],"window":{"start_at":"2026-01-01T00:00:00Z","end_at":"2026-04-01T00:00:00Z"},"detail":"summary","limit":200}' | $entrypoint events list 2>/dev/null)
oversized_window_status=$?
invalid_reminders_output=$(print -r -- '{"list_ids":["UNUSED"],"window":{"start_at":"2026-08-31T00:00:00+08:00","end_at":"2026-09-07T00:00:00+08:00"},"selection":"all","limit":200}' | $entrypoint reminders list 2>/dev/null)
invalid_reminders_status=$?
missing_entity_output=$(print -r -- '{}' | $entrypoint sources list 2>/dev/null)
missing_entity_status=$?
direct_runner_output=$(print -r -- '{}' | "$reader_dir/run.sh" events list 2>/dev/null)
direct_runner_status=$?
direct_binary_output=$(WEEKLY_REVIEW_EVENTKIT_SWIFT_TOKEN="weekly-review-eventkit-swift-v1-87B451D2" "$binary_path" self-test 2>/dev/null)
direct_binary_status=$?

build_failure_root=$(prepare_transport_case build-failure build-fail.sh)
build_failure_output=$(WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN="weekly-review-eventkit-run-v1-DA2194C7" "$build_failure_root/run.sh" capabilities 2>&1)
build_failure_status=$?
empty_worker_root=$(prepare_transport_case empty-worker build-pass.sh worker-empty.sh)
empty_worker_output=$(WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN="weekly-review-eventkit-run-v1-DA2194C7" "$empty_worker_root/run.sh" capabilities 2>&1)
empty_worker_status=$?
malformed_worker_root=$(prepare_transport_case malformed-worker build-pass.sh worker-malformed.sh)
malformed_worker_output=$(WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN="weekly-review-eventkit-run-v1-DA2194C7" "$malformed_worker_root/run.sh" capabilities 2>&1)
malformed_worker_status=$?
oversized_worker_root=$(prepare_transport_case oversized-worker build-pass.sh worker-oversized.sh)
oversized_worker_output=$(WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN="weekly-review-eventkit-run-v1-DA2194C7" "$oversized_worker_root/run.sh" capabilities 2>&1)
oversized_worker_status=$?
openstep_worker_root=$(prepare_transport_case openstep-worker build-pass.sh worker-openstep.sh)
openstep_worker_output=$(WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN="weekly-review-eventkit-run-v1-DA2194C7" "$openstep_worker_root/run.sh" capabilities 2>&1)
openstep_worker_status=$?
multiple_worker_root=$(prepare_transport_case multiple-worker build-pass.sh worker-multiple-json.sh)
multiple_worker_output=$(WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN="weekly-review-eventkit-run-v1-DA2194C7" "$multiple_worker_root/run.sh" capabilities 2>&1)
multiple_worker_status=$?
overflow_worker_root=$(prepare_transport_case overflow-worker build-pass.sh worker-overflow-number.sh)
overflow_worker_output=$(WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN="weekly-review-eventkit-run-v1-DA2194C7" "$overflow_worker_root/run.sh" capabilities 2>&1)
overflow_worker_status=$?
set -e

(( false_authorize_status != 0 ))
(( empty_events_status != 0 ))
(( oversized_window_status != 0 ))
(( invalid_reminders_status != 0 ))
(( missing_entity_status != 0 ))
(( direct_runner_status != 0 ))
(( direct_binary_status == 0 ))
(( build_failure_status == 2 ))
(( empty_worker_status == 2 ))
(( malformed_worker_status == 2 ))
(( oversized_worker_status == 2 ))
(( openstep_worker_status == 2 ))
(( multiple_worker_status == 2 ))
(( overflow_worker_status == 2 ))

CAPABILITIES_OUTPUT="$capabilities_output" \
DOCTOR_OUTPUT="$doctor_output" \
SELF_TEST_OUTPUT="$self_test_output" \
FALSE_AUTHORIZE_OUTPUT="$false_authorize_output" \
EMPTY_EVENTS_OUTPUT="$empty_events_output" \
OVERSIZED_WINDOW_OUTPUT="$oversized_window_output" \
INVALID_REMINDERS_OUTPUT="$invalid_reminders_output" \
MISSING_ENTITY_OUTPUT="$missing_entity_output" \
DIRECT_RUNNER_OUTPUT="$direct_runner_output" \
DIRECT_BINARY_OUTPUT="$direct_binary_output" \
BUILD_FAILURE_OUTPUT="$build_failure_output" \
EMPTY_WORKER_OUTPUT="$empty_worker_output" \
MALFORMED_WORKER_OUTPUT="$malformed_worker_output" \
OVERSIZED_WORKER_OUTPUT="$oversized_worker_output" \
OPENSTEP_WORKER_OUTPUT="$openstep_worker_output" \
MULTIPLE_WORKER_OUTPUT="$multiple_worker_output" \
OVERFLOW_WORKER_OUTPUT="$overflow_worker_output" \
/usr/bin/python3 - <<'PY'
import json
import os

capabilities = json.loads(os.environ["CAPABILITIES_OUTPUT"])
doctor = json.loads(os.environ["DOCTOR_OUTPUT"])
self_test = json.loads(os.environ["SELF_TEST_OUTPUT"])
false_authorize = json.loads(os.environ["FALSE_AUTHORIZE_OUTPUT"])
empty_events = json.loads(os.environ["EMPTY_EVENTS_OUTPUT"])
oversized_window = json.loads(os.environ["OVERSIZED_WINDOW_OUTPUT"])
invalid_reminders = json.loads(os.environ["INVALID_REMINDERS_OUTPUT"])
missing_entity = json.loads(os.environ["MISSING_ENTITY_OUTPUT"])
direct_runner = json.loads(os.environ["DIRECT_RUNNER_OUTPUT"])
direct_binary = json.loads(os.environ["DIRECT_BINARY_OUTPUT"])
build_failure = json.loads(os.environ["BUILD_FAILURE_OUTPUT"])
empty_worker = json.loads(os.environ["EMPTY_WORKER_OUTPUT"])
malformed_worker = json.loads(os.environ["MALFORMED_WORKER_OUTPUT"])
oversized_worker = json.loads(os.environ["OVERSIZED_WORKER_OUTPUT"])
openstep_worker = json.loads(os.environ["OPENSTEP_WORKER_OUTPUT"])
multiple_worker = json.loads(os.environ["MULTIPLE_WORKER_OUTPUT"])
overflow_worker = json.loads(os.environ["OVERFLOW_WORKER_OUTPUT"])

for result in (capabilities, doctor, self_test, direct_binary):
    assert result["ok"] is True
    assert result["eventkit_data_mutated"] is False

assert capabilities["access"]["eventkit_data_mode"] == "read_only"
assert capabilities["privacy"]["busy_omits_titles"] is True
assert doctor["bundle"]["identity_matches"] is True
assert doctor["bundle"]["ad_hoc_rebuild_may_require_reauthorization"] is True
assert doctor["eventkit_data_accessed"] is False
assert self_test["eventkit_data_accessed"] is False
assert self_test["production_state_accessed"] is False
assert all(test["passed"] is True for test in self_test["tests"])
assert false_authorize["error"]["code"] == "confirmation_required"
assert empty_events["error"]["code"] == "validation_error"
assert oversized_window["error"]["code"] == "validation_error"
assert invalid_reminders["error"]["code"] == "validation_error"
assert missing_entity["error"] == {
    "code": "validation_error",
    "message": "request.entity must be a non-empty string.",
}
assert direct_runner["error"]["code"] == "internal_entrypoint_required"
assert direct_binary["command"] == "self-test"
assert build_failure["error"]["code"] == "reader_build_failed"
assert build_failure["error"]["details"]["native_exit_status"] == 3
assert "CANARY_RAW_BUILD_DIAGNOSTIC" not in os.environ["BUILD_FAILURE_OUTPUT"]
assert empty_worker["error"]["code"] == "reader_protocol_error"
assert empty_worker["error"]["details"]["native_exit_status"] == 9
assert malformed_worker["error"]["code"] == "reader_protocol_error"
assert malformed_worker["error"]["details"]["native_exit_status"] == 0
assert oversized_worker["error"]["code"] == "output_limit_exceeded"
assert oversized_worker["error"]["details"]["native_exit_status"] == 0
assert openstep_worker["error"]["code"] == "reader_protocol_error"
assert multiple_worker["error"]["code"] == "reader_protocol_error"
assert overflow_worker["error"]["code"] == "reader_protocol_error"
for transport_failure in (
    build_failure,
    empty_worker,
    malformed_worker,
    oversized_worker,
    openstep_worker,
    multiple_worker,
    overflow_worker,
):
    assert transport_failure["ok"] is False
    assert transport_failure["eventkit_data_mutated"] is False
PY

print "Smoke tests passed. No authorization prompt was shown and no Calendar or Reminders data was read or changed."
