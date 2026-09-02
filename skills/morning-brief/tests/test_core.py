"""纯合成、无网络、无原生 provider 的确定性核心测试。"""

import copy
import hashlib
import importlib.util
import json
import pathlib
import re
import tempfile
import unicodedata
import unittest
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("brief_core", ROOT / "scripts" / "brief_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.config = core.load_json(ROOT / "assets" / "config.example.json")
        self.candidate = core.load_json(ROOT / "assets" / "candidate.example.json")
        self.now = "2026-09-02T06:10:00+08:00"

    def build(self):
        return core.build_package(self.config, self.candidate, now=self.now)

    def updates(self):
        return self.candidate["modules"]["updates"]

    def enable(self, name, scope, required=True):
        self.config["modules"][name].update(enabled=True, required=required, scope=scope)

    def envelope(self, items=None, lookahead=False):
        value = copy.deepcopy(self.updates())
        value["items"] = items or []
        value["result_count"] = len(value["items"])
        value["query_window"] = {"start_at": "2026-09-02T06:00:00+08:00", "end_at": "2026-09-02T21:00:00+08:00"} if lookahead else None
        return value

    def test_demo_ready_and_deterministic(self):
        before = copy.deepcopy((self.config, self.candidate))
        first, second = self.build(), self.build()
        self.assertEqual(first, second)
        self.assertEqual(first["readiness"], "READY")
        self.assertEqual(before, (self.config, self.candidate))
        self.assertNotIn("publish_state", first)
        self.assertNotIn("天气与出门提醒", first["body_text"])

    def test_window_nine_hours_and_cross_midnight(self):
        windows = core.resolve_windows(self.config, "2026-09-02")
        self.assertEqual(windows["lookback"]["duration_hours"], 9)
        self.assertEqual(windows["lookahead"]["duration_hours"], 15)
        self.assertTrue(windows["lookback"]["start_at"].startswith("2026-09-01"))

    def test_arbitrary_and_exact_24_hours(self):
        self.config["windows"]["lookback"]["start"] = {"day_offset": 0, "time": "02:13"}
        self.assertAlmostEqual(core.resolve_windows(self.config, "2026-09-02")["lookback"]["duration_hours"], 227 / 60)
        self.config["windows"]["lookback"]["start"] = {"day_offset": -1, "time": "06:00"}
        self.assertEqual(core.resolve_windows(self.config, "2026-09-02")["lookback"]["duration_hours"], 24)

    def test_zero_and_over_24_hours_fail(self):
        for start in ({"day_offset": 0, "time": "06:00"}, {"day_offset": -1, "time": "05:59"}):
            with self.subTest(start=start):
                self.config["windows"]["lookback"]["start"] = start
                with self.assertRaises(core.ValidationError):
                    core.resolve_windows(self.config, "2026-09-02")

    def test_cross_midnight_lookahead(self):
        self.config["windows"]["lookahead"] = {"start": {"day_offset": 0, "time": "20:00"}, "end": {"day_offset": 1, "time": "03:00"}}
        self.assertEqual(core.resolve_windows(self.config, "2026-09-02")["lookahead"]["duration_hours"], 7)

    def test_dst_fall_back_actual_25_fails(self):
        self.config["timezone"] = "America/New_York"
        self.config["windows"]["lookback"]["start"] = {"day_offset": -1, "time": "06:00"}
        with self.assertRaisesRegex(core.ValidationError, "实际经过跨度"):
            core.resolve_windows(self.config, "2026-11-01")

    def test_dst_spring_forward_nominal_25_actual_24_valid(self):
        self.config["timezone"] = "America/New_York"
        self.config["windows"]["lookback"]["start"] = {"day_offset": -1, "time": "05:00"}
        self.assertEqual(core.resolve_windows(self.config, "2026-03-08")["lookback"]["duration_hours"], 24)

    def test_dst_ambiguous_and_nonexistent_fail_closed(self):
        self.config["timezone"] = "America/New_York"
        for day, clock in (("2026-11-01", "01:30"), ("2026-03-08", "02:30")):
            with self.subTest(day=day):
                self.config["windows"]["lookback"]["start"] = {"day_offset": 0, "time": clock}
                with self.assertRaisesRegex(core.ValidationError, "DST"):
                    core.resolve_windows(self.config, day)

    def test_weekday_timezone_and_explicit_offsets(self):
        self.config["schedule"]["weekdays"] = [1]
        with self.assertRaises(core.ValidationError):
            core.resolve_windows(self.config, "2026-09-02")
        for zone in ("Mars/Olympus", "EST", "../etc/passwd"):
            self.config["timezone"] = zone
            with self.assertRaises(core.ValidationError):
                core.validate_config(self.config)

    def test_schedule_future_end_order_and_buffers(self):
        for key, value in (("generate_at", "05:45"), ("ready_by", "06:04"), ("wake_at", "06:25"), ("executor", "cloud")):
            with self.subTest(key=key):
                config = copy.deepcopy(self.config)
                config["schedule"][key] = value
                with self.assertRaises(core.ValidationError):
                    core.validate_config(config)

    def test_early_preview_never_ready(self):
        self.candidate["generated_at"] = "2026-09-02T05:45:00+08:00"
        self.now = self.candidate["generated_at"]
        self.updates().update(as_of=self.now, collected_through=self.now)
        package = self.build()
        self.assertEqual(package["readiness"], "PARTIAL")
        self.assertIn("回看窗口尚未结束", package["body_text"])

    def test_future_generated_and_source_times_fail(self):
        self.candidate["generated_at"] = "2026-09-02T06:11:00+08:00"
        with self.assertRaises(core.ValidationError):
            self.build()
        self.candidate["generated_at"] = self.now
        self.updates()["as_of"] = "2026-09-02T06:11:00+08:00"
        with self.assertRaises(core.ValidationError):
            self.build()

    def test_date_generation_hard_gate(self):
        for stamp in ("2026-09-01T06:10:00+08:00", "2026-09-02T06:21:00+08:00"):
            self.candidate["generated_at"] = stamp
            with self.assertRaises(core.ValidationError):
                core.build_package(self.config, self.candidate, now="2026-09-03T00:00:00+08:00")

    def test_required_missing_partial_optional_missing_disclosed(self):
        self.enable("calendar", {"calendar_ids": ["synthetic-calendar"]})
        self.assertEqual(self.build()["readiness"], "PARTIAL")
        self.config["modules"]["calendar"]["required"] = False
        package = self.build()
        self.assertEqual(package["readiness"], "READY")
        self.assertIn("没有提交该启用模块", package["body_text"])

    def test_disabled_input_rejected_even_empty(self):
        for value in ({}, None, self.envelope()):
            self.candidate["modules"]["weather"] = value
            with self.assertRaises(core.ValidationError):
                self.build()

    def test_at_least_one_enabled_and_disabled_no_scope_required(self):
        self.config["modules"]["weather"]["required"] = True
        self.assertEqual(self.build()["readiness"], "READY")
        self.config["modules"]["updates"]["enabled"] = False
        with self.assertRaises(core.ValidationError):
            core.validate_config(self.config)

    def test_stale_partial_and_unknown_source_time(self):
        self.config["modules"]["updates"]["max_age_hours"] = 0.01
        self.assertEqual(self.build()["readiness"], "PARTIAL")
        self.config["modules"]["updates"]["max_age_hours"] = 2
        self.updates().update(as_of=None, collected_through=None)
        self.assertEqual(self.build()["readiness"], "PARTIAL")

    def test_coverage_zero_not_failure_and_failure_not_zero(self):
        self.updates().update(items=[], result_count=0)
        package = self.build()
        self.assertEqual(package["readiness"], "READY")
        self.assertIn("所选范围内未发现", package["body_text"])
        self.updates().update(coverage="unavailable", as_of=None, collected_through=None, query_window=None, error="synthetic failure")
        package = self.build()
        self.assertEqual(package["readiness"], "PARTIAL")
        self.assertIn("不能据此断言没有事项", package["body_text"])

    def test_partial_truncation_and_declined(self):
        self.updates().update(coverage="partial", truncated_reason="synthetic limit")
        self.assertEqual(self.build()["readiness"], "PARTIAL")
        self.updates().update(coverage="declined", items=[], result_count=0)
        self.assertEqual(self.build()["readiness"], "PARTIAL")
        self.updates().update(coverage="complete")
        with self.assertRaises(core.ValidationError):
            self.build()

    def test_topics_required_and_membership(self):
        self.config["modules"]["updates"]["scope"]["topics"] = []
        with self.assertRaises(core.ValidationError):
            self.build()
        self.setUp()
        self.updates()["items"][0]["topic_id"] = "unrelated-topic"
        with self.assertRaises(core.ValidationError):
            self.build()

    def test_update_half_open_and_future_occurrence_valid(self):
        self.assertEqual(self.build()["readiness"], "READY")
        for stamp in ("2026-09-02T06:00:00+08:00", "2026-09-01T20:59:59+08:00"):
            self.updates()["items"][0]["published_at"] = stamp
            with self.assertRaises(core.ValidationError):
                self.build()

    def test_query_window_cannot_expand_partial_is_disclosed(self):
        self.updates()["query_window"]["start_at"] = "2026-09-01T20:00:00+08:00"
        with self.assertRaises(core.ValidationError):
            self.build()
        self.updates()["query_window"]["start_at"] = "2026-09-01T22:00:00+08:00"
        self.assertEqual(self.build()["readiness"], "PARTIAL")

    def test_urls_strict(self):
        for url in ("javascript:alert(1)", "https:///empty", "https://user:secret@example.org/", "https://example.org/%0A", "https://example.org/%", "https://example.org:bad/", "https://example.org/x MB:DATE=bad"):
            with self.subTest(url=url):
                self.updates()["items"][0]["source_url"] = url
                with self.assertRaises(core.ValidationError):
                    self.build()

    def test_machine_marker_and_control_injection(self):
        for text in ("MB:DATE=2026-09-02", "x mb:END", "newline\ntext", "bidi\u202e", "nbsp\u00a0"):
            self.updates()["items"][0]["summary"] = text
            with self.assertRaises(core.ValidationError):
                self.build()

    def test_visible_body_and_content_hash(self):
        package = self.build()
        body = package["body_text"]
        self.assertEqual(body.splitlines()[0], package["title"])
        self.assertFalse(body.endswith("\n"))
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), package["body_sha256"])
        matches = re.findall(r"(?s)\nMB:CONTENT-BEGIN\n(.*?)\nMB:CONTENT-END\n", body)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0], package["content_text"])
        self.assertEqual(hashlib.sha256(matches[0].encode()).hexdigest(), package["content_sha256"])
        self.assertIn("https://example.org/synthetic-observatory/announcement", body)
        readable, metadata = package["content_text"].split("\n校验信息（供快捷指令读取）\n")
        self.assertTrue(readable.startswith("适用日期：2026-09-02；时区：Asia/Shanghai\n"))
        self.assertIn("合成天文台发布演示观测安排", readable)
        self.assertNotIn("MB:", readable)
        self.assertEqual(len(metadata.splitlines()), 12)
        self.assertEqual(package["schema_version"], 1)
        self.assertEqual(package["protocol_version"], core.PHONE_PROTOCOL_VERSION)
        self.assertIn("MB:SCHEMA=2\n", metadata)
        for key in ("SCHEMA", "CONFIG", "CONFIG-REVISION", "DATE", "TIMEZONE", "BRIEF", "REVISION", "STATUS", "GENERATED", "FRESH-UNTIL", "VALID-FROM", "VALID-UNTIL"):
            self.assertEqual(len(re.findall(r"(?m)^MB:" + key + "=", body)), 1)

    def test_phone_validity_uses_local_day_not_collection_window(self):
        package = self.build()
        self.assertEqual(package["valid_from"], "2026-09-02T00:00:00+08:00")
        self.assertEqual(package["valid_until"], "2026-09-03T00:00:00+08:00")
        self.assertNotEqual(package["valid_from"], package["windows"]["lookback"]["start_at"])

    def test_phone_day_validity_allows_dst_23_and_25_hours(self):
        self.config["timezone"] = "America/New_York"
        self.config["schedule"]["weekdays"] = [1, 2, 3, 4, 5, 6, 7]
        self.candidate["modules"] = {}
        for day, offset, hours, start_offset, end_offset in (
                ("2026-03-08", "-04:00", 23, "-05:00", "-04:00"),
                ("2026-11-01", "-05:00", 25, "-04:00", "-05:00")):
            with self.subTest(day=day):
                self.candidate.update(applicable_date=day, generated_at=day + "T06:10:00" + offset)
                self.now = self.candidate["generated_at"]
                package = self.build()
                start, end = (datetime.fromisoformat(package[key]) for key in ("valid_from", "valid_until"))
                self.assertEqual((end - start).total_seconds(), hours * 3600)
                self.assertTrue(package["valid_from"].endswith(start_offset))
                self.assertTrue(package["valid_until"].endswith(end_offset))
                self.assertEqual(package["fresh_until"], package["valid_until"])

    def test_fresh_until_required_age_and_all_optional_day_end(self):
        self.assertEqual(self.build()["fresh_until"], "2026-09-02T08:05:00+08:00")
        self.config["modules"]["updates"]["required"] = False
        self.assertEqual(self.build()["fresh_until"], "2026-09-03T00:00:00+08:00")

    def test_fresh_until_stale_or_unknown_asof_never_fabricated(self):
        self.config["modules"]["updates"]["max_age_hours"] = 0.01
        package = self.build()
        self.assertEqual(package["fresh_until"], "2026-09-02T06:05:36+08:00")
        self.assertEqual(package["readiness"], "PARTIAL")
        self.updates().update(as_of=None, collected_through=None)
        package = self.build()
        self.assertEqual(package["fresh_until"], "2026-09-03T00:00:00+08:00")
        self.assertEqual(package["readiness"], "PARTIAL")

    def test_stale_source_can_expire_before_phone_day_without_false_ready(self):
        self.updates().update(as_of="2026-09-01T20:00:00+08:00", collected_through="2026-09-01T20:00:00+08:00",
                              coverage="partial", items=[], result_count=0, query_window=None)
        package = self.build()
        self.assertEqual(package["fresh_until"], "2026-09-01T22:00:00+08:00")
        self.assertEqual(package["valid_from"], "2026-09-02T00:00:00+08:00")
        self.assertEqual(package["readiness"], "PARTIAL")

    def test_nonexistent_local_next_midnight_rejects_phone_validity(self):
        self.config["timezone"] = "Pacific/Apia"
        self.config["schedule"]["weekdays"] = [1, 2, 3, 4, 5, 6, 7]
        self.candidate.update(applicable_date="2011-12-29", generated_at="2011-12-29T06:10:00-10:00", modules={})
        self.now = self.candidate["generated_at"]
        with self.assertRaisesRegex(core.ValidationError, "valid_until"):
            self.build()

    def test_fresh_until_weather_validity_is_minimum(self):
        self.enable("weather", {"location": "合成城市", "source_urls": ["https://example.org/weather"]})
        forecast = {"title": "合成预报", "summary": "虚构", "source_url": "https://example.org/weather", "location": "合成城市", "valid_from": "2026-09-02T06:00:00+08:00", "valid_until": "2026-09-02T06:45:00+08:00"}
        self.candidate["modules"]["weather"] = self.envelope([forecast])
        self.assertEqual(self.build()["fresh_until"], "2026-09-02T06:45:00+08:00")

    def test_nfc_rendering(self):
        self.updates()["items"][0]["summary"] = "cafe\u0301"
        body = self.build()["body_text"]
        self.assertEqual(body, unicodedata.normalize("NFC", body))

    def test_identity_revision_and_config_isolation(self):
        first = self.build()
        self.candidate["revision"] = 2
        second = self.build()
        self.assertEqual(first["brief_id"], second["brief_id"])
        self.assertNotEqual(first["body_sha256"], second["body_sha256"])
        self.config["config_revision"] = 2
        self.candidate.update(config_revision=2, revision=1)
        updated = self.build()
        self.assertEqual(first["brief_id"], updated["brief_id"])
        self.assertNotEqual(first["title"], updated["title"])
        self.assertTrue(first["title"].endswith(" · c01 · r01"))
        self.assertTrue(updated["title"].endswith(" · c02 · r01"))
        self.config["storage"]["notes"]["account"] = "other-synthetic-account"
        self.assertNotEqual(first["brief_id"], self.build()["brief_id"])

    def test_ordinary_preferences_do_not_change_daily_brief_identity(self):
        first = self.build()
        self.config["config_revision"] = 2
        self.candidate["config_revision"] = 2
        self.config["modules"]["updates"]["scope"]["topics"][0]["query"] = "合成偏好更新"
        self.config["modules"]["updates"]["max_age_hours"] = 3
        self.assertEqual(first["brief_id"], self.build()["brief_id"])

    def test_calendar_uses_lookahead_and_overlap(self):
        self.enable("calendar", {"calendar_ids": ["synthetic-calendar"]})
        event = {"title": "合成跨午夜事件", "summary": "虚构", "source_url": None, "start_at": "2026-09-01T23:00:00+08:00", "end_at": "2026-09-02T07:00:00+08:00", "all_day": False, "status": "tentative", "availability": "free"}
        self.candidate["modules"]["calendar"] = self.envelope([event], lookahead=True)
        self.assertEqual(self.build()["readiness"], "READY")
        event["start_at"] = "2026-09-02T19:00:00+08:00"
        event["end_at"] = "2026-09-02T20:00:00+08:00"
        self.assertEqual(self.build()["readiness"], "READY")
        event["status"] = "canceled"
        with self.assertRaises(core.ValidationError):
            self.build()

    def test_reminder_date_only_and_out_of_scope(self):
        self.enable("reminders", {"list_ids": ["synthetic-list"], "overdue_days": 7, "include_undated_important": False})
        reminder = {"title": "合成仅日期待办", "summary": "虚构", "source_url": None, "due_date": "2026-09-02", "due_at": None, "important": False}
        self.candidate["modules"]["reminders"] = self.envelope([reminder])
        self.assertIn("不代表 00:00 逾期", self.build()["body_text"])
        reminder["due_date"] = "2025-09-02"
        with self.assertRaises(core.ValidationError):
            self.build()

    def test_reminder_query_scope_and_date_only_midnight_boundary(self):
        self.enable("reminders", {"list_ids": ["synthetic-list"], "overdue_days": 7, "include_undated_important": False})
        reminder = {"title": "合成仅日期待办", "summary": "虚构", "source_url": None, "due_date": "2026-09-02", "due_at": None, "important": False}
        envelope = self.envelope([reminder])
        envelope["query_window"] = {"start_at": "2026-08-26T00:00:00+08:00", "end_at": "2026-09-02T21:00:00+08:00"}
        self.candidate["modules"]["reminders"] = envelope
        self.assertEqual(self.build()["readiness"], "READY")
        envelope["query_window"]["start_at"] = "2025-08-26T00:00:00+08:00"
        with self.assertRaises(core.ValidationError):
            self.build()
        envelope["query_window"]["start_at"] = "2026-08-26T00:00:00+08:00"
        self.config["windows"]["lookahead"]["end"] = {"day_offset": 1, "time": "00:00"}
        envelope["query_window"]["end_at"] = "2026-09-03T00:00:00+08:00"
        reminder["due_date"] = "2026-09-03"
        with self.assertRaises(core.ValidationError):
            self.build()

    def test_managed_goal_projection_without_action_id(self):
        self.enable("calendar", {"calendar_ids": ["synthetic-calendar"]})
        event = {"title": "合成投影", "summary": "虚构", "source_url": None, "start_at": "2026-09-02T08:00:00+08:00", "end_at": "2026-09-02T09:00:00+08:00", "all_day": False, "status": "confirmed", "availability": "busy", "managed": {"goal_id": "G-DEMO", "projection_id": "P-DEMO"}}
        self.candidate["modules"]["calendar"] = self.envelope([event], lookahead=True)
        self.assertEqual(self.build()["readiness"], "READY")
        event["managed"]["action_id"] = None
        self.assertEqual(self.build()["readiness"], "READY")

    def test_goals_require_active_approved_action(self):
        self.enable("goals", {"vault_path": "/tmp/synthetic-vault", "goal_paths": ["Goals/G-DEMO/G-DEMO.md"]})
        goal = {"title": "合成行动", "summary": "虚构", "source_url": "obsidian://open?vault=SYNTHETIC&file=Goals%2FG-DEMO.md", "goal_id": "G-DEMO", "action_id": "A-DEMO", "approved": True, "status": "active"}
        self.candidate["modules"]["goals"] = self.envelope([goal])
        self.assertEqual(self.build()["readiness"], "READY")
        goal["status"] = "paused"
        with self.assertRaises(core.ValidationError):
            self.build()

    def test_weather_scope_freshness_and_validity(self):
        self.enable("weather", {"location": "合成城市", "source_urls": ["https://example.org/weather"]})
        forecast = {"title": "合成预报", "summary": "虚构", "source_url": "https://example.org/weather", "location": "合成城市", "valid_from": "2026-09-02T06:00:00+08:00", "valid_until": "2026-09-02T21:00:00+08:00"}
        self.candidate["modules"]["weather"] = self.envelope([forecast])
        self.assertEqual(self.build()["readiness"], "READY")
        forecast["valid_until"] = "2026-09-02T06:15:00+08:00"
        self.assertEqual(self.build()["readiness"], "PARTIAL")

    def test_field_types_bool_as_int_and_nonfinite(self):
        for value in (True, "1", 0, float("nan"), float("inf")):
            self.config["config_revision"] = value
            with self.assertRaises(core.ValidationError):
                core.validate_config(self.config)
        self.setUp()
        self.candidate["revision"] = True
        with self.assertRaises(core.ValidationError):
            self.build()

    def test_strict_date_time_types_and_unknown_fields(self):
        for value in ("2026-9-2", "2026-02-30", True):
            self.candidate["applicable_date"] = value
            with self.assertRaises(core.ValidationError):
                self.build()
        self.setUp()
        for value in ("2026-09-02T06:10:00", "2026-09-02T06:10:00-00:00", "2026-09-02T06:10:00+25:00", "2026-09-02T06:10:00+13:60"):
            self.candidate["generated_at"] = value
            with self.assertRaises(core.ValidationError):
                self.build()
        self.setUp()
        self.candidate["unexpected"] = "x"
        with self.assertRaises(core.ValidationError):
            self.build()

    def test_bounds_and_private_storage_scope(self):
        self.updates()["items"][0]["summary"] = "x" * 8193
        with self.assertRaises(core.ValidationError):
            self.build()
        self.setUp()
        self.config["storage"]["notes"]["shared"] = True
        with self.assertRaises(core.ValidationError):
            core.validate_config(self.config)
        self.setUp()
        validated = core.validate_config(self.config)
        self.assertEqual(validated["storage"]["scope"], "private-local")

    def test_json_duplicate_keys_and_nonfinite(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with tempfile.NamedTemporaryFile() as handle:
                handle.write(raw)
                handle.flush()
                with self.assertRaises(core.ValidationError):
                    core.load_json(handle.name)

    def test_aware_now_required(self):
        with self.assertRaises(core.ValidationError):
            core.build_package(self.config, self.candidate, now=datetime(2026, 9, 2))
        self.assertEqual(core.build_package(self.config, self.candidate, now=datetime(2026, 9, 2, tzinfo=timezone.utc))["readiness"], "READY")


if __name__ == "__main__":
    unittest.main()
