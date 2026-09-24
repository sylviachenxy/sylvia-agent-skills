"""合成 Notes 正文的离线消费测试；没有真实配置、设备或 provider 读取。"""
import copy
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import unicodedata
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import brief_core as core
import phone_protocol as phone

CONFIG_ID = "stable-synthetic-receiver"
NOW = "2026-09-02T07:00:00+08:00"


def metadata(**changes):
    fields = {"SCHEMA": "2", "CONFIG": CONFIG_ID, "CONFIG-REVISION": "1",
              "DATE": "2026-09-02", "TIMEZONE": "Asia/Shanghai", "BRIEF": "mb-synthetic-day",
              "REVISION": "1", "STATUS": "READY", "GENERATED": "2026-09-02T06:10:00+08:00",
              "FRESH-UNTIL": "2026-09-02T08:05:00+08:00", "VALID-FROM": "2026-09-02T00:00:00+08:00",
              "VALID-UNTIL": "2026-09-03T00:00:00+08:00"}
    fields.update(changes)
    return fields


def wrap_content(content, title=True):
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ("合成标题，不是实际来源\n" if title else "") + "MB:BEGIN\nMB:CONTENT-BEGIN\n" + content + "\nMB:CONTENT-END\nMB:CONTENT-SHA256=" + checksum + "\nMB:END"


def body(fields=None, text="合成私人正文 café 中文\nhttps://example.org/synthetic-private", title=True):
    fields = metadata() if fields is None else fields
    content = text + "\n\n校验信息（供快捷指令读取）\n" + "\n".join("MB:" + key + "=" + value for key, value in fields.items())
    return wrap_content(content, title)


class ConsumerTests(unittest.TestCase):
    def select(self, bodies, now=NOW, config_id=CONFIG_ID):
        return phone.select_note(config_id, bodies, now)

    def test_valid_snapshot_returns_only_selected_index_and_metadata(self):
        result = self.select([body()])
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["selected_index"], 0)
        self.assertEqual(result["metadata"]["config_id"], CONFIG_ID)
        self.assertEqual(result["metadata"]["config_revision"], 1)
        self.assertFalse(result["fallback"])
        self.assertTrue(result["not_latest_verified"])
        for secret in ("合成私人正文", "example.org", "合成标题"):
            self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))

    def test_wrapper_transport_forms_without_arbitrary_trim(self):
        canonical = body()
        for candidate in (canonical, body(title=False), canonical + "\n", canonical.replace("\n", "\r\n"),
                          canonical.replace("\n", "\r"), unicodedata.normalize("NFD", canonical), canonical.replace(" ", "\u00a0")):
            with self.subTest(candidate=candidate[:10]):
                self.assertEqual(self.select([candidate])["status"], "READY")
        for candidate in (canonical + "\n\n", "extra\n" + canonical, canonical + "\n用户注释", canonical[:-4],
                          canonical.replace("合成私人正文", "合成私人正文 ")):
            with self.subTest(candidate=candidate[:10]):
                self.assertEqual(self.select([candidate])["status"], "READ_ERROR")

    def test_metadata_is_unique_complete_known_and_not_prose(self):
        original = body()
        content = original.split("MB:CONTENT-BEGIN\n")[1].split("\nMB:CONTENT-END")[0]
        variants = [content + "\nMB:CONFIG=" + CONFIG_ID,
                    content.replace("MB:STATUS=READY\n", ""),
                    content + "\nMB:UNKNOWN=value", content + "\nMB:BEGIN",
                    content + "\n文中插入 MB:CONFIG=other", content + "\nmb:STATUS=READY"]
        for value in variants:
            with self.subTest(value=value[-35:]):
                self.assertEqual(self.select([wrap_content(value)])["status"], "READ_ERROR")
        reversed_fields = dict(reversed(list(metadata().items())))
        self.assertEqual(self.select([body(reversed_fields)])["status"], "READY")

    def test_invalid_schema_identity_status_date_and_versions_fail_closed(self):
        changes = [{"SCHEMA": "3"}, {"CONFIG": "invalid config"}, {"BRIEF": "bad\tidentifier"},
                   {"STATUS": "NOT_READY"}, {"DATE": "2026-02-30"}, {"DATE": "2026-9-2"},
                   {"TIMEZONE": ""}, {"TIMEZONE": "../bad"}, {"TIMEZONE": "EST"},
                   {"CONFIG-REVISION": "0"}, {"CONFIG-REVISION": "-1"}, {"CONFIG-REVISION": "1.0"},
                   {"REVISION": "01"}, {"REVISION": "2e2"}, {"REVISION": str(phone.MAX_SAFE_INTEGER + 1)}]
        for change in changes:
            with self.subTest(change=change):
                result = self.select([body(metadata(**change))])
                # A foreign/malformed CONFIG value is outside the receiver scope.
                self.assertIn(result["status"], ("READ_ERROR", "NOT_READY"))
                self.assertIsNone(result["selected_index"])

    def test_timestamp_requires_seconds_known_offset_and_real_values(self):
        for value in ("2026-09-02T06:10:00", "2026-09-02T06:10+08:00", "2026-09-02T06:10:00-00:00",
                      "2026-09-02T06:10:00+00:60", "2026-09-02T06:10:00+14:01",
                      "2026-09-02T25:10:00+08:00", "2026-09-02T06:10:60+08:00"):
            with self.subTest(value=value):
                self.assertEqual(self.select([body(metadata(GENERATED=value))])["status"], "READ_ERROR")

    def test_validity_uses_applicable_and_next_civil_midnights(self):
        for change in ({"VALID-FROM": "2026-09-02T01:00:00+08:00"},
                       {"VALID-UNTIL": "2026-09-04T00:00:00+08:00"},
                       {"VALID-UNTIL": "2026-09-02T00:00:00+08:00"},
                       {"DATE": "2026-09-01"},
                       {"GENERATED": "2026-09-01T23:59:59+08:00"},
                       {"GENERATED": "2026-09-03T00:00:00+08:00"},
                       {"FRESH-UNTIL": "2026-09-03T00:00:01+08:00"}):
            with self.subTest(change=change):
                self.assertEqual(self.select([body(metadata(**change))])["status"], "READ_ERROR")

    def test_validity_start_inclusive_end_exclusive(self):
        candidate = body(metadata(GENERATED="2026-09-02T00:00:00+08:00"))
        self.assertEqual(self.select([candidate], "2026-09-02T00:00:00+08:00")["status"], "READY")
        result = self.select([candidate], "2026-09-03T00:00:00+08:00")
        self.assertEqual(result["status"], "NOT_READY")
        self.assertEqual(result["ignored"], {"historical": 1})

    def test_runtime_freshness_degrades_and_never_upgrades_partial(self):
        self.assertEqual(self.select([body()], "2026-09-02T08:05:00+08:00")["status"], "READY")
        for value in ("2026-09-02T08:05:01+08:00", "2026-09-02T09:00:00+08:00"):
            result = self.select([body()], value)
            self.assertEqual(result["status"], "PARTIAL")
            self.assertEqual(result["metadata"]["generated_status"], "READY")
        self.assertEqual(self.select([body(metadata(STATUS="PARTIAL"))])["status"], "PARTIAL")
        ancient = body(metadata(STATUS="PARTIAL", **{"FRESH-UNTIL": "2026-09-01T12:00:00+08:00"}))
        self.assertEqual(self.select([ancient])["status"], "PARTIAL")

    def test_config_revision_precedes_numeric_report_revision(self):
        old = body(metadata(**{"CONFIG-REVISION": "1", "REVISION": "999"}))
        new = body(metadata(**{"CONFIG-REVISION": "2", "REVISION": "1"}))
        result = self.select([old, new])
        self.assertEqual(result["selected_index"], 1)
        self.assertEqual(result["metadata"]["config_revision"], 2)
        self.assertEqual(self.select([body(metadata(REVISION="2")), body(metadata(REVISION="10"))])["selected_index"], 1)
        restored = body(metadata(**{"CONFIG-REVISION": "3", "REVISION": "1"}))
        self.assertEqual(self.select([old, new, restored])["selected_index"], 2)

    def test_any_duplicate_current_logical_version_is_read_error(self):
        for duplicate in (body(), body(text="不同正文且重新计算hash"), body(metadata(BRIEF="mb-another-identity"))):
            with self.subTest(duplicate=duplicate[:15]):
                result = self.select([body(), duplicate])
                self.assertEqual(result["status"], "READ_ERROR")
                self.assertEqual(result["error"], "DUPLICATE_VERSION")
                self.assertIsNone(result["selected_index"])

    def test_target_malformed_candidate_allows_explicit_complete_fallback(self):
        damaged = body(metadata(**{"CONFIG-REVISION": "9"})).replace("合成私人正文", "损坏内容")
        result = self.select([damaged, body()])
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["selected_index"], 1)
        self.assertTrue(result["fallback"])
        self.assertTrue(result["not_latest_verified"])
        self.assertEqual(result["rejected"], [{"index": 0, "code": "CONTENT_HASH_MISMATCH"}])

    def test_future_generated_current_note_is_not_selected(self):
        future = body(metadata(**{"CONFIG-REVISION": "9", "GENERATED": "2026-09-02T07:01:00+08:00"}))
        self.assertEqual(self.select([future])["status"], "READ_ERROR")
        result = self.select([future, body()])
        self.assertEqual(result["selected_index"], 1)
        self.assertTrue(result["fallback"])
        self.assertEqual(result["rejected"][0]["code"], "FUTURE_GENERATED")

    def test_foreign_profile_never_counts_as_target_fallback(self):
        foreign = body(metadata(CONFIG="another-profile"))
        self.assertEqual(self.select([foreign])["status"], "NOT_READY")
        result = self.select([foreign, body()])
        self.assertEqual(result["status"], "READY")
        self.assertFalse(result["fallback"])
        self.assertEqual(result["ignored"], {"other_profile": 1})

    def test_unattributable_parse_error_does_not_invent_profile_or_leak_body(self):
        result = self.select(["SYNTHETIC PRIVATE malformed body", body()])
        self.assertEqual(result["status"], "READY")
        self.assertFalse(result["fallback"])
        self.assertTrue(result["not_latest_verified"])
        self.assertNotIn("SYNTHETIC PRIVATE", json.dumps(result))
        self.assertEqual(self.select([None])["status"], "READ_ERROR")

    def test_legacy_protocol_is_never_ready_and_does_not_poison_current_body(self):
        fields = metadata(SCHEMA="1")
        del fields["VALID-FROM"], fields["VALID-UNTIL"]
        legacy = body(fields)
        self.assertEqual(self.select([legacy])["status"], "NOT_READY")
        result = self.select([legacy, body()])
        self.assertEqual(result["selected_index"], 1)
        self.assertFalse(result["fallback"])
        self.assertEqual(result["ignored"], {"legacy_protocol": 1})

    def test_complete_historical_future_and_empty_inputs_are_not_ready_not_error(self):
        self.assertEqual(self.select([])["status"], "NOT_READY")
        self.assertEqual(self.select([body()], "2026-09-03T07:00:00+08:00")["status"], "NOT_READY")
        self.assertEqual(self.select([body()], "2026-09-01T07:00:00+08:00")["status"], "NOT_READY")
        yesterday = body(metadata(**{"CONFIG-REVISION": "999", "DATE": "2026-09-01",
                                      "VALID-FROM": "2026-09-01T00:00:00+08:00", "VALID-UNTIL": "2026-09-02T00:00:00+08:00",
                                      "GENERATED": "2026-09-01T06:10:00+08:00", "FRESH-UNTIL": "2026-09-01T08:05:00+08:00"}))
        result = self.select([yesterday, yesterday, body()])
        self.assertEqual(result["selected_index"], 2)
        self.assertFalse(result["fallback"])
        self.assertEqual(result["ignored"], {"historical": 2})

    def test_device_date_and_offset_are_not_used_to_match_applicable_date(self):
        # This instant is still September 1 in UTC, but the received Shanghai day is active.
        result = self.select([body()], "2026-09-01T23:00:00Z")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["metadata"]["applicable_date"], "2026-09-02")
        self.assertEqual(self.select([body()], datetime(2026, 9, 1, 23, tzinfo=timezone.utc)), result)

    def test_static_receiver_accepts_timezone_change_and_orders_overlapping_days(self):
        los_angeles = metadata(**{"CONFIG-REVISION": "2", "TIMEZONE": "America/Los_Angeles", "DATE": "2026-09-01",
                                  "VALID-FROM": "2026-09-01T00:00:00-07:00", "VALID-UNTIL": "2026-09-02T00:00:00-07:00",
                                  "GENERATED": "2026-09-01T06:10:00-07:00", "FRESH-UNTIL": "2026-09-01T08:05:00-07:00"})
        result = self.select([body(), body(los_angeles)], "2026-09-01T23:00:00Z")
        self.assertEqual(result["selected_index"], 1)
        self.assertEqual(result["metadata"]["timezone"], "America/Los_Angeles")
        self.assertEqual(result["metadata"]["applicable_date"], "2026-09-01")
        self.assertEqual(result["status"], "PARTIAL")

    def test_dst_23_and_25_hour_validity_intervals(self):
        for day, following, before, after in (("2026-03-08", "2026-03-09", "-05:00", "-04:00"),
                                             ("2026-11-01", "2026-11-02", "-04:00", "-05:00")):
            fields = metadata(**{"DATE": day, "TIMEZONE": "America/New_York", "VALID-FROM": day + "T00:00:00" + before,
                                 "VALID-UNTIL": following + "T00:00:00" + after, "GENERATED": day + "T06:10:00" + after,
                                 "FRESH-UNTIL": day + "T08:05:00" + after})
            self.assertEqual(self.select([body(fields)], day + "T07:00:00" + after)["status"], "READY")
            self.assertEqual(self.select([body(fields)], following + "T00:00:00" + after)["status"], "NOT_READY")

    def test_input_bounds_and_invalid_now_fail_without_partial_selection(self):
        for value in (None, "2026-09-02T07:00:00", datetime(2026, 9, 2, 7), True):
            self.assertEqual(self.select([body()], value)["status"], "READ_ERROR")
        self.assertEqual(phone.select_note("bad id", [body()], NOW)["status"], "READ_ERROR")
        self.assertEqual(phone.select_note(CONFIG_ID, "not a list", NOW)["status"], "READ_ERROR")
        with patch.object(phone, "MAX_NOTES", 1):
            self.assertEqual(self.select([body(), body()])["status"], "READ_ERROR")
        with patch.object(phone, "MAX_TOTAL_BYTES", 10):
            self.assertEqual(self.select([body()])["status"], "READ_ERROR")

    def test_consumer_is_pure_and_does_not_require_any_file_or_config(self):
        candidate = body()
        with patch("builtins.open", side_effect=AssertionError("Consumer must not open files")):
            self.assertEqual(phone.select_note(CONFIG_ID, [candidate], NOW)["status"], "READY")


class GeneratorIntegrationTests(unittest.TestCase):
    def build(self, revision=1, report_revision=1, zone_name="Asia/Shanghai", day="2026-09-02", **changes):
        config = json.loads((ROOT / "assets/config.example.json").read_text())
        config.update(config_id=CONFIG_ID, config_revision=revision, timezone=zone_name)
        config["schedule"]["wake_at"] = "07:00"
        config["modules"]["updates"]["max_items"] = changes.get("max_items", 3)
        config["modules"]["updates"]["scope"]["topics"][0]["query"] = changes.get("topic", "仅合成对象；不是实际用户偏好")
        if changes.get("lookback_start"):
            config["windows"]["lookback"]["start"]["time"] = changes["lookback_start"]
        if changes.get("generate_at"):
            config["schedule"].update(generate_at=changes["generate_at"], ready_by="06:40")
        windows = core.resolve_windows(config, day)
        zone = ZoneInfo(zone_name)
        local_day = datetime.fromisoformat(day).date()
        generated_clock = "06:20" if changes.get("generate_at") else "06:10"
        generated = datetime.combine(local_day, time.fromisoformat(generated_clock), zone)
        as_of = generated - timedelta(minutes=5)
        query = {key: windows["lookback"][key] for key in ("start_at", "end_at")}
        candidate = {"schema_version": 1, "config_id": CONFIG_ID, "config_revision": revision,
                     "applicable_date": day, "revision": report_revision, "generated_at": generated.isoformat(),
                     "modules": {"updates": {"coverage": "complete", "as_of": as_of.isoformat(),
                                 "collected_through": query["end_at"], "query_window": query,
                                 "result_count": 0, "truncated_reason": None, "error": None, "items": []}}}
        return core.build_package(config, candidate, now=generated.isoformat())

    def test_producer_to_static_receiver_survives_ordinary_preference_changes(self):
        old = self.build(report_revision=25)
        content = self.build(revision=2, max_items=4, topic="不同合成兴趣对象")
        window = self.build(revision=3, lookback_start="06:00", generate_at="06:15")
        restored = self.build(revision=4)
        received = []
        for package in (old, content, window, restored):
            received.append(package["body_text"])
            result = phone.select_note(CONFIG_ID, received, NOW)
            self.assertEqual(result["status"], "READY", result)
            self.assertEqual(result["metadata"]["config_revision"], package["config_revision"])
            self.assertEqual(result["metadata"]["content_sha256"], package["content_sha256"])
            self.assertFalse(result["fallback"])
            self.assertTrue(result["not_latest_verified"])

    def test_producer_to_same_receiver_handles_changed_timezone_date_and_dst(self):
        for revision, zone, day in ((1, "Asia/Shanghai", "2026-09-02"), (2, "Asia/Tokyo", "2026-09-03"),
                                    (3, "America/New_York", "2026-03-08"), (4, "America/New_York", "2026-11-01")):
            package = self.build(revision=revision, zone_name=zone, day=day)
            now = datetime.fromisoformat(package["generated_at"]) + timedelta(minutes=30)
            result = phone.select_note(CONFIG_ID, [package["body_text"]], now.astimezone(timezone.utc))
            self.assertEqual(result["status"], "READY", result)
            self.assertEqual(result["metadata"]["timezone"], zone)
            self.assertEqual(result["metadata"]["applicable_date"], day)


if __name__ == "__main__":
    unittest.main()
