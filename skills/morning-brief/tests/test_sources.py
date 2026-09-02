"""Offline EventKit transport and semantic tests; never reads private native data."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
import unittest


SKILL = Path(__file__).resolve().parents[1]
ROOT = SKILL / "scripts/apple-eventkit-reader"
ENTRY = SKILL / "scripts/apple-eventkit-reader.sh"
SPEC = importlib.util.spec_from_file_location("morning_brief_reader", ROOT / "reader.py")
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


def request(reminders=False):
    value = {"list_ids" if reminders else "calendar_ids": ["SYNTHETIC-ID"],
             "window": {"start_at": "2026-09-02T06:00:00+08:00", "end_at": "2026-09-02T21:00:00+08:00"},
             "timezone": "Asia/Shanghai", "limit": 50}
    if reminders:
        value["include_undated"] = False
    return value


def fixture(reminders=False):
    query = request(reminders)
    item = {"item_id": "SYNTHETIC-ITEM", "title": "Synthetic title", "created_at": None, "last_modified_at": None,
            "managed": None, "managed_status": "not_requested", "recurring": False}
    scope = {"list_ids" if reminders else "calendar_ids": ["SYNTHETIC-ID"], "backend_query_window": query["window"],
             "include_goal_links": False, "notes_exported": False}
    if reminders:
        item.update(list_id="SYNTHETIC-ID", due_date="2026-09-02", due_at=None,
                    due={"kind": "date", "year": 2026, "month": 9, "day": 2, "timezone": None,
                         "effective_timezone": "Asia/Shanghai", "timezone_inferred_from_request": True},
                    completed=False, completion_at=None, priority=1, current_instance_only=True)
        scope.update(candidate_mode="incomplete_due_with_civil_day_timezone_guard", candidate_count=1,
                     include_undated=False, completed_included=False,
                     sort="due_instant_or_end_of_civil_due_day_then_id; undated_last")
    else:
        item.update(calendar_id="SYNTHETIC-ID", start_at="2026-09-02T08:00:00+08:00", end_at="2026-09-02T09:00:00+08:00",
                    all_day=False, timezone="Asia/Shanghai", status="tentative", availability="free", detached=False,
                    occurrence_start_at="2026-09-02T08:00:00+08:00", original_occurrence_at=None)
        scope.update(candidate_mode="events_overlapping_window", cancelled_and_free_included=True)
    return {"ok": True, "reader_version": "1.0.0", "protocol_version": 1, "eventkit_data_mutated": False,
            "command": "reminders list" if reminders else "events list", "eventkit_data_accessed": True,
            "event_store_id": "SYNTHETIC-STORE", "coverage": "complete", "as_of": "2026-09-02T06:01:00+08:00",
            "collected_through": "2026-09-02T06:01:01+08:00", "query_window": query["window"], "timezone": "Asia/Shanghai",
            "scope": scope, "result_count": 1, "matched_count": 1, "limit": 50, "truncated": False,
            "truncated_reason": None, "error": None, "items": [item]}


class InputTests(unittest.TestCase):
    def test_explicit_scopes_validate_without_native_access(self):
        reader.validate_input("events list", request())
        reader.validate_input("reminders list", request(True))
        for command in ("setup authorize", "setup containers list"):
            reader.validate_input(command, {"entity": "event", "confirmed": True})

    def test_blank_duplicate_and_missing_allowlists(self):
        for ids in ([], [" "], ["\n"], ["A", "A"], ["x\0"], ["x" * 4097], ["X"] * 51):
            value = request(); value["calendar_ids"] = ids
            with self.subTest(ids=ids[:2]), self.assertRaises(ValueError):
                reader.validate_input("events list", value)

    def test_date_windows_are_bounded_and_real(self):
        for end in ("2026-09-02T06:00:00+08:00", "2026-09-01T06:00:00+08:00", "2027-09-02T06:00:00+08:00", "2026-02-30T06:00:00+08:00", "2026-09-02T21:00:00"):
            value = request(); value["window"]["end_at"] = end
            with self.subTest(end=end), self.assertRaises(ValueError):
                reader.validate_input("events list", value)

    def test_source_window_exact_100_elapsed_days_and_overflow(self):
        start = datetime.fromisoformat("2026-06-04T00:00:00+08:00")
        for reminders in (False, True):
            value = request(reminders)
            value["window"] = {"start_at": start.isoformat(), "end_at": (start + timedelta(days=100)).isoformat()}
            command = "reminders list" if reminders else "events list"
            reader.validate_input(command, value)
            value["window"]["end_at"] = (start + timedelta(days=100, seconds=1)).isoformat()
            with self.assertRaises(ValueError):
                reader.validate_input(command, value)

    def test_90_day_config_interoperates_with_native_request_limit(self):
        spec = importlib.util.spec_from_file_location("morning_brief_core_for_sources", SKILL / "scripts/brief_core.py")
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        config = json.loads((SKILL / "assets/config.example.json").read_text())
        config["modules"]["reminders"].update(enabled=True, scope={"list_ids": ["SYNTHETIC-ID"], "overdue_days": 90, "include_undated_important": False})
        config["windows"]["lookahead"] = {"start": {"day_offset": 7, "time": "00:00"}, "end": {"day_offset": 7, "time": "23:59"}}
        core.validate_config(config)
        windows = core.resolve_windows(config, "2026-09-02")
        value = request(True)
        value["window"] = {"start_at": "2026-06-04T00:00:00+08:00", "end_at": windows["lookahead"]["end_at"]}
        reader.validate_input("reminders list", value)
        self.assertLess((reader.timestamp(value["window"]["end_at"]) - reader.timestamp(value["window"]["start_at"])).total_seconds(), 99 * 86400)

    def test_required_boolean_and_setup_confirmation(self):
        for flag in (None, 0, 1, "true"):
            value = request(True); value["include_undated"] = flag
            with self.assertRaises(ValueError):
                reader.validate_input("reminders list", value)
        with self.assertRaises(ValueError):
            reader.validate_input("setup containers list", {"entity": "event", "confirmed": False})

    def test_strict_json_rejects_ambiguous_encodings(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e9999}', b'{}{}', b'[]', b'{x = 1;}', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                reader.strict_json(raw)

    def test_mutation_fields_and_commands_rejected_before_build(self):
        value = request(); value["notes"] = "PRIVATE"
        with self.assertRaises(ValueError):
            reader.validate_input("events list", value)
        for args, data in ((["events", "delete"], b"{}"), (["containers", "list"], b"{}"),
                           (["events", "list"], b'{"calendar_ids":[" "]}'),
                           (["setup", "authorize"], b'{"entity":"event","confirmed":false}')):
            done = subprocess.run([sys.executable, str(ROOT / "reader.py"), *args], input=data, capture_output=True, timeout=5)
            self.assertEqual(done.returncode, 2)
            self.assertFalse(json.loads(done.stdout)["ok"])
            self.assertEqual(done.stderr, b"")


class OutputTests(unittest.TestCase):
    def test_native_status_dates_and_scope_preserved(self):
        for reminders in (False, True):
            value = fixture(reminders)
            self.assertEqual(reader.validate_output(value, 0, value["command"], request(reminders)), 0)
        self.assertEqual(fixture()["items"][0]["availability"], "free")
        self.assertIsNone(fixture(True)["items"][0]["due_at"])

    def test_privacy_allowlists_at_item_and_nested_scope(self):
        for key in ("notes", "url", "obsidian_url", "goal_path", "attendees", "organizer", "location"):
            value = fixture(); value["items"][0][key] = "PRIVATE"
            with self.subTest(key=key), self.assertRaises(ValueError):
                reader.validate_output(value, 0, "events list")
        value = fixture(); value["scope"]["backend_query_window"]["notes"] = "PRIVATE"
        with self.assertRaises(ValueError):
            reader.validate_output(value, 0, "events list")
        for key in ("created_at", "last_modified_at", "timezone"):
            value = fixture(); value["items"][0][key] = {"notes": "PRIVATE"}
            with self.subTest(key=key), self.assertRaises(ValueError):
                reader.validate_output(value, 0, "events list")

    def test_managed_projection_only_stable_ids(self):
        value = fixture(); item = value["items"][0]
        value["scope"]["include_goal_links"] = True
        item["managed_status"] = "valid"
        item["managed"] = {"schema_version": 2, "goal_id": "G-2026-001", "action_id": "G-2026-001-A001", "projection_id": "G-2026-001-E001"}
        self.assertEqual(reader.validate_output(value, 0, "events list"), 0)
        for changes in ({"obsidian_url": "obsidian://open?vault=PRIVATE"}, {"projection_id": "G-2026-002-E001"}, {"action_id": "G-2026-002-A001"}):
            bad = copy.deepcopy(value); bad["items"][0]["managed"].update(changes)
            with self.assertRaises((ValueError, TypeError)):
                reader.validate_output(bad, 0, "events list")

    def test_cross_scope_items_and_complete_truncated_mismatch_rejected(self):
        for changes in ({"calendar_id": "UNSELECTED"}, {"occurrence_start_at": "2026-09-02T01:00:00+08:00"}):
            bad = fixture(); bad["items"][0].update(changes)
            with self.assertRaises(ValueError):
                reader.validate_output(bad, 0, "events list", request())
        bad = fixture(); bad["matched_count"] = 100
        with self.assertRaises(ValueError):
            reader.validate_output(bad, 0, "events list")
        bad.update(coverage="partial", truncated=True, truncated_reason="result_limit", limit=1)
        self.assertEqual(reader.validate_output(bad, 0, "events list"), 0)

    def test_completed_and_unrequested_undated_items_rejected(self):
        for changes in ({"completed": True}, {"due": None, "due_date": None, "due_at": None},
                        {"due_date": "2026-09-02", "due_at": "2026-09-02T08:00:00+08:00"}):
            bad = fixture(True); bad["items"][0].update(changes)
            with self.assertRaises(ValueError):
                reader.validate_output(bad, 0, "reminders list")

    def test_exit_and_mutation_flag_fail_closed(self):
        with self.assertRaises(ValueError):
            reader.validate_output(fixture(), 2, "events list")
        value = fixture(); value["eventkit_data_mutated"] = True
        with self.assertRaises(ValueError):
            reader.validate_output(value, 0, "events list")


class TransportTests(unittest.TestCase):
    def worker(self, code, raw=b"", timeout=3):
        return reader.bounded_run([sys.executable, "-c", code], raw, timeout, dict(os.environ))

    def test_bounded_stdin_stdout_and_suppressed_stderr(self):
        output, code = self.worker("import sys; data=sys.stdin.buffer.read(); sys.stderr.write('PRIVATE secret'); sys.stdout.buffer.write(data)", b'{"synthetic":true}')
        self.assertEqual(output, b'{"synthetic":true}')
        self.assertEqual(code, 0)

    def test_stdout_limit_enforced_while_streaming(self):
        with self.assertRaises(reader.TransportError) as caught:
            self.worker("import sys; sys.stdout.buffer.write(b'x' * 5000000)")
        self.assertEqual(caught.exception.code, "output_limit_exceeded")

    def test_watchdog_terminates_without_waiting_for_worker(self):
        started = time.monotonic()
        with self.assertRaises(reader.TransportError) as caught:
            self.worker("import time; time.sleep(30)", timeout=0.2)
        self.assertEqual(caught.exception.code, "operation_timeout")
        self.assertLess(time.monotonic() - started, 3)


class NativeOfflineTests(unittest.TestCase):
    def test_no_native_mutation_or_executor_dependency(self):
        source = (ROOT / "Sources/main.swift").read_text()
        for pattern in (r"\.\s*(save|remove|commit|rollback|reset)\s*\(", r"\b(EKEvent|EKReminder)\s*\(\s*eventStore\s*:",
                        r"requestWriteOnlyAccess", r"scheduler_executor", r"personal-scheduler", r"weekly-review"):
            self.assertIsNone(re.search(pattern, source), pattern)
        doctor = source.split('case "doctor":', 1)[1].split('case "self-test":', 1)[0]
        self.assertNotIn("EKEventStore(", doctor)
        self.assertNotIn("authorizationStatus(for:", doctor)

    def test_protocol_identity_and_command_surface(self):
        protocol = json.loads((ROOT / "protocol-v1.json").read_text())
        plist = plistlib.loads((ROOT / "Info.plist").read_bytes())
        self.assertEqual(set(protocol["commands"]), reader.COMMANDS)
        self.assertEqual(protocol["limits"]["maximum_logical_source_window_days"], reader.MAX_WINDOW_DAYS)
        self.assertEqual(plist["CFBundleIdentifier"], protocol["identity"]["bundle_id"])
        self.assertEqual(plist["LSMinimumSystemVersion"], "14.0")
        self.assertIn(".build/", (ROOT / ".gitignore").read_text())

    @unittest.skipUnless(sys.platform == "darwin", "native build is macOS-only")
    def test_actual_native_offline_commands(self):
        for command in ("doctor", "capabilities", "self-test"):
            done = subprocess.run(["/bin/zsh", str(ENTRY), command], capture_output=True, timeout=150)
            self.assertEqual(done.returncode, 0, done.stdout.decode())
            value = json.loads(done.stdout)
            self.assertFalse(value["eventkit_data_accessed"])
            self.assertFalse(value["eventkit_data_mutated"])
            if command == "doctor":
                self.assertEqual(value["permissions"], "not_checked_offline")
                self.assertTrue(value["bundle_identity_matches"])
            elif command == "self-test":
                self.assertFalse(value["native_store_initialized"])
                self.assertGreaterEqual(len(value["tests"]), 18)
                self.assertTrue(all(test["passed"] for test in value["tests"]))


if __name__ == "__main__":
    unittest.main()
