#!/usr/bin/env python3
"""Offline tests for file-activity.py using synthetic temporary homes only."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "file-activity.py"
PROTOCOL_VERSION = 1
START = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
END = START + dt.timedelta(days=7)


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("file_activity_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load collector module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


def make_home_and_root(temporary: str, name: str = "Project") -> Tuple[Path, Path]:
    base = Path(temporary).resolve()
    home = base / "synthetic-home"
    root = home / "Work" / name
    root.mkdir(parents=True)
    return home, root


def request_for(root: Path, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "operation": "scan",
        "options": options or {},
        "protocol_version": PROTOCOL_VERSION,
        "request_id": "offline-test",
        "roots": [str(root)],
        "window": {"end": END.isoformat(), "start": START.isoformat()},
    }


def set_mtime(path: Path, value: dt.datetime) -> None:
    nanoseconds = int(value.timestamp() * 1_000_000_000)
    os.utime(path, ns=(nanoseconds, nanoseconds), follow_symlinks=False)


def run_collector_raw(
    request: Dict[str, Any], synthetic_home: Path, expected_exit: int = 0
) -> Tuple[Dict[str, Any], bytes]:
    # The production module never reads this environment variable. The tiny
    # wrapper replaces only its account-home resolver before invoking the real
    # main(), keeping all accepted filesystem access inside the temp fixture.
    wrapper = f"""
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location('file_activity_cli_test', {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._current_user_home = lambda: os.environ['FILE_ACTIVITY_SYNTHETIC_HOME']
raise SystemExit(module.main())
"""
    environment = os.environ.copy()
    environment["FILE_ACTIVITY_SYNTHETIC_HOME"] = str(synthetic_home)
    completed = subprocess.run(
        [sys.executable, "-c", wrapper],
        input=json.dumps(request).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        timeout=10,
    )
    if completed.returncode != expected_exit:
        raise AssertionError(
            f"expected exit {expected_exit}, got {completed.returncode}; "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )
    if completed.stderr:
        raise AssertionError(f"unexpected stderr: {completed.stderr!r}")
    return json.loads(completed.stdout), completed.stdout


def run_collector(
    request: Dict[str, Any], synthetic_home: Path, expected_exit: int = 0
) -> Dict[str, Any]:
    return run_collector_raw(request, synthetic_home, expected_exit)[0]


def parse_request(request: Dict[str, Any], synthetic_home: Path) -> Dict[str, Any]:
    with mock.patch.object(
        MODULE, "_current_user_home", return_value=str(synthetic_home)
    ):
        return MODULE._parse_request(request)


class FileActivityTests(unittest.TestCase):
    def test_half_open_window_and_metadata_only_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            cases = {
                "before.md": START - dt.timedelta(seconds=1),
                "at-start.md": START,
                "near-end.txt": END - dt.timedelta(seconds=1),
                "at-end.md": END,
            }
            for name, timestamp in cases.items():
                path = root / name
                path.write_text(name, encoding="utf-8")
                set_mtime(path, timestamp)

            result = run_collector(request_for(root), home)

            self.assertTrue(result["ok"])
            self.assertEqual(result["protocol_version"], 1)
            self.assertEqual(result["request_id"], "offline-test")
            self.assertEqual(result["mode"], "metadata_only")
            self.assertFalse(result["state_written"])
            self.assertEqual(result["window"]["semantics"], "[start,end)")
            self.assertEqual(
                [item["relative_path"] for item in result["observations"]],
                ["at-start.md", "near-end.txt"],
            )
            first = result["observations"][0]
            self.assertIn("modified", first["activity"])
            self.assertEqual(first["mtime"], "2001-01-01T00:00:00.000000+00:00")
            self.assertEqual(first["type"]["extension"], ".md")
            self.assertIn("size_bytes", first)
            self.assertNotIn("hash", first)
            self.assertEqual(result["content_diff"]["status"], "not_computed")
            self.assertEqual(result["summary"]["candidate_count"], 2)
            self.assertFalse(result["summary"]["truncated"])

    def test_default_exclusions_cloud_stub_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            outside = home / "Work" / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            set_mtime(outside, START)

            safe = root / "achievement.md"
            safe.write_text("safe", encoding="utf-8")
            set_mtime(safe, START)
            (root / "outside-link.md").symlink_to(outside)

            for directory in [".hidden", ".git", "node_modules", "venv", "build", "Caches"]:
                excluded = root / directory
                excluded.mkdir()
                file_path = excluded / "ignored.md"
                file_path.write_text("ignored", encoding="utf-8")
                set_mtime(file_path, START)

            for name in [".hidden-file.md", "private.pem", "credentials.json", "my-secret.txt"]:
                file_path = root / name
                file_path.write_text("ignored", encoding="utf-8")
                set_mtime(file_path, START)

            cloud_stub = root / ".draft.pages.icloud"
            cloud_stub.write_bytes(b"")
            set_mtime(cloud_stub, START)

            result = run_collector(request_for(root, {"exclude_globs": ["*.bak"]}), home)
            self.assertEqual(
                [item["relative_path"] for item in result["observations"]],
                ["achievement.md"],
            )
            skipped = result["summary"]["skipped_by_reason"]
            self.assertEqual(skipped["symlink"], 1)
            self.assertEqual(skipped["hidden_directory"], 2)
            self.assertGreaterEqual(skipped["excluded_directory_name"], 4)
            self.assertEqual(skipped["hidden_file"], 1)
            self.assertEqual(skipped["sensitive_file_type"], 3)
            self.assertEqual(skipped["cloud_placeholder_stub"], 1)
            self.assertTrue(result["capabilities"]["descriptor_relative_traversal"])

    def test_hash_is_explicit_and_aggregate_bytes_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            files = {
                "a-small.txt": b"abc",
                "b-second.txt": b"wxyz",
                "c-large.txt": b"0123456789",
            }
            for name, content in files.items():
                path = root / name
                path.write_bytes(content)
                set_mtime(path, START)

            result = run_collector(
                request_for(
                    root,
                    {
                        "hash_max_bytes": 5,
                        "hash_total_max_bytes": 5,
                        "include_birthtime": False,
                        "include_hash": True,
                    },
                ),
                home,
            )
            by_name = {item["relative_path"]: item for item in result["observations"]}
            self.assertEqual(result["mode"], "metadata_plus_explicit_hash")
            small_results = {
                name: by_name[name]["hash"]
                for name in ["a-small.txt", "b-second.txt"]
            }
            computed_names = [
                name
                for name, hash_result in small_results.items()
                if hash_result["status"] == "computed"
            ]
            self.assertEqual(len(computed_names), 1)
            computed_name = computed_names[0]
            self.assertEqual(
                small_results[computed_name]["value"],
                hashlib.sha256(files[computed_name]).hexdigest(),
            )
            skipped_name = (
                "b-second.txt"
                if computed_name == "a-small.txt"
                else "a-small.txt"
            )
            self.assertEqual(
                small_results[skipped_name],
                {"reason": "aggregate_hash_byte_limit", "status": "skipped"},
            )
            self.assertEqual(
                by_name["c-large.txt"]["hash"],
                {"reason": "size_limit", "status": "skipped"},
            )
            hashes = result["summary"]["hashes"]
            self.assertIn(hashes["bytes_read"], {3, 4})
            self.assertLessEqual(hashes["bytes_read"], 5)
            self.assertEqual(hashes["computed"], 1)
            self.assertTrue(hashes["partial"])
            self.assertTrue(result["summary"]["partial"])

    def test_type_size_and_birthtime_fields_are_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            file_path = root / "paper.md"
            file_path.write_text("paper", encoding="utf-8")
            set_mtime(file_path, START)

            result = run_collector(
                request_for(
                    root,
                    {
                        "include_birthtime": False,
                        "include_size": False,
                        "include_type": False,
                    },
                ),
                home,
            )
            observation = result["observations"][0]
            self.assertNotIn("birthtime", observation)
            self.assertNotIn("size_bytes", observation)
            self.assertNotIn("type", observation)
            self.assertIn("mtime", observation)

    def test_candidate_and_actual_enumeration_limits_report_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            for index in range(10):
                file_path = root / f"item-{index:02d}.md"
                file_path.write_text(str(index), encoding="utf-8")
                set_mtime(file_path, START)

            candidate_result = run_collector(
                request_for(root, {"max_candidates": 1}), home
            )
            self.assertEqual(candidate_result["summary"]["candidate_count"], 1)
            self.assertTrue(candidate_result["summary"]["truncated"])
            self.assertEqual(
                candidate_result["summary"]["truncation_reason"], "max_candidates"
            )

            enumeration_result = run_collector(
                request_for(root, {"max_visited_entries": 3}), home
            )
            self.assertEqual(enumeration_result["summary"]["visited_entries"], 3)
            self.assertLessEqual(
                enumeration_result["summary"]["candidate_count"], 3
            )
            self.assertTrue(enumeration_result["summary"]["truncated"])
            self.assertEqual(
                enumeration_result["summary"]["truncation_reason"],
                "max_visited_entries",
            )

    def test_positive_root_policy_rejects_broad_hidden_sensitive_and_trash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, safe_root = make_home_and_root(temporary)
            forbidden_roots = [
                home,
                home / "Work" / ".hidden-root",
                home / "Work" / "client-secret",
                home / "Trash" / "Review",
                home / "Work" / "build",
            ]
            for path in forbidden_roots[1:]:
                path.mkdir(parents=True)

            for forbidden in forbidden_roots:
                result = run_collector(request_for(forbidden), home, expected_exit=2)
                self.assertEqual(result["error"]["code"], "unsafe_root")
                rendered_details = json.dumps(result["error"].get("details", {}))
                self.assertNotIn(str(forbidden), rendered_details)
                self.assertEqual(result["error"]["details"]["root_index"], 0)
                self.assertEqual(result["error"]["details"]["root_alias"], "root_0")

            accepted = run_collector(request_for(safe_root), home)
            self.assertTrue(accepted["ok"])

    def test_system_other_user_and_external_volume_roots_reject_without_path_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, _root = make_home_and_root(temporary)
            for raw in ["/System", "/Users/not-the-current-user/Work", "/Volumes/Drive/Work"]:
                result = run_collector(request_for(Path(raw)), home, expected_exit=2)
                self.assertEqual(result["error"]["code"], "unsafe_root")
                details = json.dumps(result["error"].get("details", {}))
                self.assertNotIn(raw, details)
                self.assertEqual(result["error"]["details"], {"root_alias": "root_0", "root_index": 0})

    def test_rejects_symlink_root_and_overlapping_roots_without_disclosing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, real_root = make_home_and_root(temporary)
            child = real_root / "child"
            child.mkdir()
            linked_root = home / "Work" / "linked"
            linked_root.symlink_to(real_root, target_is_directory=True)

            symlink_result = run_collector(
                request_for(linked_root), home, expected_exit=2
            )
            self.assertEqual(symlink_result["error"]["code"], "unsafe_root")
            self.assertNotIn(
                str(linked_root), json.dumps(symlink_result["error"].get("details", {}))
            )

            overlap_request = request_for(real_root)
            overlap_request["roots"] = [str(real_root), str(child)]
            overlap_result = run_collector(overlap_request, home, expected_exit=2)
            self.assertEqual(overlap_result["error"]["code"], "unsafe_root")
            self.assertIn("non-overlapping", overlap_result["error"]["message"])
            self.assertEqual(overlap_result["error"]["details"], {"root_indices": [0, 1]})

    def test_cloudstorage_is_specific_and_never_reads_without_reliable_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home = base / "synthetic-home"
            provider = home / "Library" / "CloudStorage" / "GoogleDrive-user"
            root = provider / "Review"
            root.mkdir(parents=True)
            cloud_file = root / "cloud.txt"
            cloud_file.write_bytes(b"must-not-be-read")
            set_mtime(cloud_file, START)

            for forbidden in [home / "Library", provider]:
                result = run_collector(request_for(forbidden), home, expected_exit=2)
                self.assertEqual(result["error"]["code"], "unsafe_root")

            unknown = home / "Library" / "CloudStorage" / "UnknownProvider" / "Review"
            unknown.mkdir(parents=True)
            result = run_collector(request_for(unknown), home, expected_exit=2)
            self.assertEqual(result["error"]["code"], "unsafe_root")

            request = request_for(
                root,
                {"include_birthtime": False, "include_hash": True},
            )
            request["cloud_hash_allowlist"] = [
                {"root_index": 0, "relative_path": "cloud.txt"}
            ]
            parsed = parse_request(request, home)
            real_open = MODULE.os.open

            def guarded_open(
                path: Any,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: Optional[int] = None,
            ) -> int:
                if path == "cloud.txt" and not flags & int(getattr(MODULE.os, "O_DIRECTORY", 0)):
                    raise AssertionError("CloudStorage file content was opened")
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            try:
                with mock.patch.object(MODULE.os, "open", side_effect=guarded_open), mock.patch.object(
                    MODULE.os, "read", side_effect=AssertionError("CloudStorage content read")
                ):
                    result = MODULE._scan(parsed)
            finally:
                MODULE._close_parsed_roots(parsed)

            observation = result["observations"][0]
            self.assertEqual(observation["storage"]["kind"], "cloudstorage")
            self.assertEqual(
                observation["hash"],
                {
                    "reason": "cloud_materialization_proof_unavailable",
                    "status": "skipped",
                },
            )
            self.assertEqual(result["summary"]["hashes"]["bytes_read"], 0)
            self.assertTrue(result["summary"]["hashes"]["partial"])
            self.assertEqual(
                result["capabilities"]["cloud_hashing"]["status"],
                "metadata_only_fail_closed",
            )

    def test_directory_swap_to_symlink_cannot_escape_verified_dirfd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            swapping = root / "swap"
            swapping.mkdir()
            inside = swapping / "inside.md"
            inside.write_text("inside", encoding="utf-8")
            set_mtime(inside, START)
            outside = home / "Work" / "Outside"
            outside.mkdir()
            outside_file = outside / "outside.md"
            outside_file.write_text("outside", encoding="utf-8")
            set_mtime(outside_file, START)

            parsed = parse_request(request_for(root), home)
            real_open = MODULE.os.open
            raced = False

            def racing_open(
                path: Any,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: Optional[int] = None,
            ) -> int:
                nonlocal raced
                if (
                    not raced
                    and path == "swap"
                    and dir_fd is not None
                    and flags & int(getattr(MODULE.os, "O_DIRECTORY", 0))
                ):
                    raced = True
                    inside.unlink()
                    swapping.rmdir()
                    swapping.symlink_to(outside, target_is_directory=True)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            try:
                with mock.patch.object(MODULE.os, "open", side_effect=racing_open):
                    result = MODULE._scan(parsed)
            finally:
                MODULE._close_parsed_roots(parsed)

            self.assertTrue(raced)
            self.assertEqual(result["observations"], [])
            self.assertEqual(
                result["summary"]["skipped_by_reason"][
                    "directory_changed_or_symlink_race"
                ],
                1,
            )

    def test_file_swap_to_symlink_cannot_hash_outside_verified_dirfd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            victim = root / "victim.txt"
            victim.write_bytes(b"victim")
            set_mtime(victim, START)
            outside = home / "Work" / "outside.txt"
            outside.write_bytes(b"outside-secret")
            set_mtime(outside, START)

            request = request_for(
                root, {"include_birthtime": False, "include_hash": True}
            )
            parsed = parse_request(request, home)
            real_open = MODULE.os.open
            raced = False

            def racing_open(
                path: Any,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: Optional[int] = None,
            ) -> int:
                nonlocal raced
                if (
                    not raced
                    and path == "victim.txt"
                    and dir_fd is not None
                    and not flags & int(getattr(MODULE.os, "O_DIRECTORY", 0))
                ):
                    raced = True
                    victim.unlink()
                    victim.symlink_to(outside)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            try:
                with mock.patch.object(MODULE.os, "open", side_effect=racing_open):
                    result = MODULE._scan(parsed)
            finally:
                MODULE._close_parsed_roots(parsed)

            self.assertTrue(raced)
            self.assertEqual(
                result["observations"][0]["hash"],
                {"reason": "symlink_race", "status": "skipped"},
            )
            outside_digest = hashlib.sha256(b"outside-secret").hexdigest()
            self.assertNotIn(outside_digest, json.dumps(result))

    def test_monotonic_deadline_returns_partial_without_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            file_path = root / "late.md"
            file_path.write_text("late", encoding="utf-8")
            set_mtime(file_path, START)

            parsed = parse_request(request_for(root), home)
            parsed["deadline"] = time.monotonic() - 1
            try:
                result = MODULE._scan(parsed)
            finally:
                MODULE._close_parsed_roots(parsed)

            self.assertEqual(result["summary"]["visited_entries"], 0)
            self.assertTrue(result["summary"]["partial"])
            self.assertTrue(result["summary"]["truncated"])
            self.assertEqual(
                result["summary"]["truncation_reason"], "deadline_exceeded"
            )

    def test_cli_output_cap_keeps_one_valid_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            for index in range(40):
                path = root / (f"item-{index:02d}-" + "x" * 80 + ".md")
                path.write_text(str(index), encoding="utf-8")
                set_mtime(path, START)

            request = request_for(root, {"max_output_bytes": 1_024})
            result, raw = run_collector_raw(request, home)
            self.assertLessEqual(len(raw), 1_024)
            decoded, end_index = json.JSONDecoder().raw_decode(raw.decode("utf-8"))
            self.assertEqual(decoded, result)
            self.assertFalse(raw.decode("utf-8")[end_index:].strip())
            self.assertTrue(result["summary"]["partial"])
            self.assertTrue(result["summary"]["truncated"])
            self.assertEqual(
                result["summary"]["truncation_reason"], "max_output_bytes"
            )

    def test_validation_failures_are_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = make_home_and_root(temporary)
            naive = request_for(root)
            naive["window"]["start"] = "2001-01-01T00:00:00"
            result = run_collector(naive, home, expected_exit=2)
            self.assertEqual(result["error"]["code"], "validation_error")

            no_time = request_for(
                root, {"include_birthtime": False, "include_mtime": False}
            )
            result = run_collector(no_time, home, expected_exit=2)
            self.assertEqual(result["error"]["code"], "validation_error")

            unknown = request_for(root)
            unknown["unexpected"] = True
            result = run_collector(unknown, home, expected_exit=2)
            self.assertEqual(result["error"]["code"], "validation_error")

            oversized = request_for(root)
            oversized["window"]["end"] = (START + dt.timedelta(days=9)).isoformat()
            result = run_collector(oversized, home, expected_exit=2)
            self.assertEqual(result["error"]["code"], "validation_error")
            self.assertIn("eight days", result["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
