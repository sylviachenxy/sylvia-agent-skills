#!/usr/bin/env python3
"""Offline contract tests for weekly-review-state.py.

All state, vaults, documents, and source roots live below a fresh temporary
directory. The production Application Support path is never selected.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).resolve().parents[1] / "weekly-review-state.py"
TEMPLATE = Path(__file__).resolve().parents[2] / "assets" / "source-config-template.json"
PROTOCOL_VERSION = 1


def load_state_module() -> Any:
    spec = importlib.util.spec_from_file_location("weekly_review_state_tested", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


class IsolatedHarness:
    def __init__(self, base: Path) -> None:
        self.base = base.resolve()
        self.home = self.base / "home"
        self.storage = self.base / "state"
        self.school = self.home / "Documents" / "School"
        self.repository = self.home / "Documents" / "Code" / "course-project"
        self.provider = self.home / "Library" / "CloudStorage" / "GoogleDrive-test"
        self.vault = self.provider / "My Drive" / "SylviaVault"
        self.school.mkdir(parents=True)
        (self.repository / ".git").mkdir(parents=True)
        (self.vault / ".obsidian").mkdir(parents=True)

    @property
    def env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "WEEKLY_REVIEW_STATE_ALLOW_TEST_OVERRIDE": "1",
                "WEEKLY_REVIEW_STATE_TEST_HOME": str(self.home),
                "WEEKLY_REVIEW_STATE_TEST_ROOT": str(self.storage),
            }
        )
        for name in (
            "WEEKLY_REVIEW_STATE_TEST_FAILPOINT",
            "WEEKLY_REVIEW_STATE_TEST_MAX_CONFIG_BYTES",
            "WEEKLY_REVIEW_STATE_TEST_MAX_STATE_BYTES",
        ):
            env.pop(name, None)
        return env

    def config(
        self,
        *,
        snapshot_text: bool = True,
        goals_read: bool = False,
        content_root: Optional[Path] = None,
    ) -> Dict[str, Any]:
        root = content_root or self.vault
        return {
            "eventkit": {
                "calendar_ids": ["calendar-stable-id"],
                "reminder_list_ids": ["reminders-stable-id"],
            },
            "files": {
                "content_roots": [
                    {
                        "id": "obsidian-vault",
                        "path": str(root),
                        "snapshot_text": snapshot_text,
                    }
                ],
                "discovery_roots": [
                    {"id": "school-files", "path": str(self.school)}
                ],
                "exclude_globs": [
                    ".env",
                    "*.pem",
                    "node_modules/**",
                    "Reviews/Weekly/**",
                    "Goals/**",
                ],
            },
            "git": {
                "repositories": [
                    {
                        "author_emails": ["student@example.invalid"],
                        "id": "course-project",
                        "path": str(self.repository),
                    }
                ]
            },
            "limits": {
                "max_baseline_entries": 1000,
                "max_candidates_per_source": 100,
                "max_content_chars": 10000,
                "max_diff_lines": 100,
                "max_report_bytes": 1024 * 1024,
                "snapshot_max_file_bytes": 4096,
                "snapshot_max_total_bytes": 16384,
            },
            "mail": {
                "scopes": [
                    {
                        "account_id": "mail-account-stable-id",
                        "alias": "school sent",
                        "content_access": "metadata",
                        "date_field": "sent",
                        "id": "gmail-sent",
                        "mailbox_id": "sent-mailbox-stable-id",
                        "scope_kind": "sent",
                    }
                ]
            },
            "notes": {
                "scopes": [
                    {
                        "account_id": "notes-account-stable-id",
                        "alias": "course notes",
                        "content_access": "plaintext",
                        "folder_id": "notes-folder-stable-id",
                        "id": "course-notes",
                    }
                ]
            },
            "schema_version": 1,
            "timezone": "Asia/Shanghai",
            "vault": {
                "goals_read": goals_read,
                "output_root": "Reviews/Weekly",
                "path": str(self.vault),
            },
            "week_start": "monday",
        }

    def run(
        self,
        request: Dict[str, Any],
        *,
        expected_exit: int = 0,
        failpoint: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        env = self.env
        if extra_env:
            env.update(extra_env)
        if failpoint:
            env["WEEKLY_REVIEW_STATE_TEST_FAILPOINT"] = failpoint
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(request).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
            timeout=20,
        )
        if completed.returncode != expected_exit:
            raise AssertionError(
                f"expected exit {expected_exit}, got {completed.returncode}; "
                f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
            )
        if completed.stderr:
            raise AssertionError(f"unexpected stderr: {completed.stderr!r}")
        return json.loads(completed.stdout)

    @staticmethod
    def request(operation: str, **fields: Any) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "operation": operation,
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "offline-test",
        }
        value.update(fields)
        return value

    def set_config(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        expected_revision: int = 0,
        extra_env: Optional[Dict[str, str]] = None,
        expected_exit: int = 0,
    ) -> Dict[str, Any]:
        return self.run(
            self.request(
                "config.set",
                config=config or self.config(),
                confirmed=True,
                expected_revision=expected_revision,
            ),
            expected_exit=expected_exit,
            extra_env=extra_env,
        )

    @staticmethod
    def observation(
        text: Optional[str],
        digest_text: Optional[str] = None,
        *,
        item_id: str = "F001",
        kind: str = "file",
        locator: str = "Notes/course-progress.md",
        scope_id: str = "obsidian-vault",
        container_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        hash_source = text if digest_text is None else digest_text
        assert hash_source is not None
        item: Dict[str, Any] = {
            "item_id": item_id,
            "sha256": sha256_text(hash_source),
            "source": {
                "kind": kind,
                "locator": locator,
                "scope_id": scope_id,
            },
        }
        if container_id is not None:
            item["source"]["container_id"] = container_id
        if text is not None:
            item["text"] = text
        return item

    @staticmethod
    def window(week_id: str, collected: Optional[dt.datetime] = None) -> Dict[str, str]:
        year = int(week_id[:4])
        week = int(week_id[-2:])
        zone = ZoneInfo("Asia/Shanghai")
        monday = dt.date.fromisocalendar(year, week, 1)
        start = dt.datetime.combine(monday, dt.time.min, tzinfo=zone)
        end = start + dt.timedelta(days=7)
        cutoff = collected or end
        return {
            "start": start.isoformat(),
            "end_exclusive": end.isoformat(),
            "collected_through": cutoff.isoformat(),
            "timezone": "Asia/Shanghai",
        }

    def documents(
        self,
        week_id: str,
        *,
        status: str = "confirmed",
        window: Optional[Dict[str, str]] = None,
        frontmatter_collected: Optional[str] = None,
        timezone: Optional[str] = None,
        period_start: Optional[str] = None,
        period_end_exclusive: Optional[str] = None,
        generated: str = "valid",
    ) -> Dict[str, str]:
        review_window = window or self.window(week_id)
        start_date = dt.datetime.fromisoformat(review_window["start"]).date()
        end_date = dt.datetime.fromisoformat(review_window["end_exclusive"]).date()
        generated_text = {
            "valid": (
                "<!-- weekly-review:generated:start -->\n"
                "## Confirmed achievements\n\n"
                "- Test achievement\n"
                "<!-- weekly-review:generated:end -->\n"
            ),
            "missing": "## User-only text\n",
            "duplicate": (
                "<!-- weekly-review:generated:start -->\n"
                "<!-- weekly-review:generated:start -->\n"
                "<!-- weekly-review:generated:end -->\n"
            ),
            "reversed": (
                "<!-- weekly-review:generated:end -->\n"
                "<!-- weekly-review:generated:start -->\n"
            ),
        }[generated]
        report_relative = f"Reviews/Weekly/{week_id[:4]}/{week_id}.md"
        report_text = (
            "---\n"
            "schema_version: 1\n"
            "type: weekly-review\n"
            f"week_id: {week_id}\n"
            f"period_start: {period_start or start_date.isoformat()}\n"
            f"period_end_exclusive: {period_end_exclusive or end_date.isoformat()}\n"
            f"timezone: {timezone or review_window['timezone']}\n"
            f"collected_through: {frontmatter_collected or review_window['collected_through']}\n"
            f"status: {status}\n"
            "---\n\n"
            f"# {week_id} Weekly Review\n\n"
            + generated_text
        )

        index_relative = "Reviews/Weekly/Weekly Reviews.md"
        index_path = self.vault / index_relative
        links: List[str] = []
        if index_path.exists():
            links = [
                line
                for line in index_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("- [[") and f"|{week_id}]]" not in line
            ]
        links.append(
            f"- [[{report_relative[:-3]}|{week_id}]] · confirmed · test-window"
        )
        index_text = (
            "# Weekly Reviews\n\n"
            "<!-- weekly-review:index:start -->\n"
            + "\n".join(sorted(links, reverse=True))
            + "\n<!-- weekly-review:index:end -->\n"
        )
        return {
            "index_relative": index_relative,
            "index_sha256": sha256_text(index_text),
            "index_text": index_text,
            "report_relative": report_relative,
            "report_sha256": sha256_text(report_text),
            "report_text": report_text,
        }

    def stage_request(
        self,
        review_id: str,
        week_id: str,
        documents: Dict[str, str],
        observations: List[Dict[str, Any]],
        *,
        window: Optional[Dict[str, str]] = None,
        coverage: Optional[Dict[str, str]] = None,
        confirmed: bool = True,
        expected_config_revision: int = 1,
        expected_state_revision: int = 0,
        report_preimage: Optional[Dict[str, str]] = None,
        index_preimage: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return self.request(
            "review.stage",
            confirmed=confirmed,
            coverage=coverage or {"files": "complete", "mail": "declined"},
            expected_config_revision=expected_config_revision,
            expected_state_revision=expected_state_revision,
            index_sha256=documents["index_sha256"],
            index_preimage=index_preimage
            or self.preimage(documents["index_relative"]),
            index_text=documents["index_text"],
            observations=observations,
            preview_sha256=documents["report_sha256"],
            report_sha256=documents["report_sha256"],
            report_preimage=report_preimage
            or self.preimage(documents["report_relative"]),
            report_text=documents["report_text"],
            review_id=review_id,
            week_id=week_id,
            window=window or self.window(week_id),
        )

    def stage(
        self,
        review_id: str,
        week_id: str,
        documents: Dict[str, str],
        observations: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return self.run(
            self.stage_request(review_id, week_id, documents, observations, **kwargs)
        )

    def preimage(self, relative: str) -> Dict[str, str]:
        path = self.vault / relative
        if not path.exists():
            return {"state": "absent"}
        return {"state": "sha256", "sha256": sha256_bytes(path.read_bytes())}

    def promote_request(
        self,
        review_id: str,
        documents: Dict[str, str],
        *,
        expected_state_revision: int,
        expected_config_revision: int = 1,
        confirmed: bool = True,
    ) -> Dict[str, Any]:
        return self.request(
            "review.write-promote",
            confirmed=confirmed,
            expected_config_revision=expected_config_revision,
            expected_state_revision=expected_state_revision,
            index={
                "relative_path": documents["index_relative"],
                "target_text": documents["index_text"],
            },
            report={
                "relative_path": documents["report_relative"],
                "target_text": documents["report_text"],
            },
            review_id=review_id,
        )

    def commit_review(
        self,
        review_id: str,
        week_id: str,
        observations: List[Dict[str, Any]],
        *,
        expected_config_revision: int = 1,
        expected_state_revision: int = 0,
        coverage: Optional[Dict[str, str]] = None,
        window: Optional[Dict[str, str]] = None,
    ) -> Tuple[Dict[str, str], Dict[str, Any], Dict[str, Any]]:
        documents = self.documents(week_id, window=window)
        staged = self.stage(
            review_id,
            week_id,
            documents,
            observations,
            coverage=coverage,
            expected_config_revision=expected_config_revision,
            expected_state_revision=expected_state_revision,
            window=window,
        )
        promoted = self.run(
            self.promote_request(
                review_id,
                documents,
                expected_config_revision=expected_config_revision,
                expected_state_revision=staged["state_revision"],
            )
        )
        return documents, staged, promoted


class WeeklyReviewStateTests(unittest.TestCase):
    def test_conditional_rename_uses_portable_rename_excl_flag_only(self) -> None:
        module = load_state_module()
        captured: List[int] = []

        class FakeRename:
            argtypes: Any = None
            restype: Any = None

            def __call__(self, *arguments: Any) -> int:
                captured.append(arguments[-1])
                return 0

        class FakeLibc:
            renameatx_np = FakeRename()

        original_cdll = module.ctypes.CDLL
        module.ctypes.CDLL = lambda *args, **kwargs: FakeLibc()
        try:
            module._rename_noreplace(3, "source", 4, "target")
        finally:
            module.ctypes.CDLL = original_cdll
        self.assertEqual(captured, [0x00000004])

    def test_self_test_is_storage_free_and_advertises_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            result = harness.run(harness.request("self-test"))
            self.assertTrue(result["ok"])
            self.assertFalse(result["production_state_accessed"])
            self.assertEqual(
                result["capabilities"]["review_transaction"],
                ["review.stage", "review.write-promote", "review.abort"],
            )
            self.assertFalse(harness.storage.exists())

    def test_template_and_cloudstorage_scope_boundaries(self) -> None:
        template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(template["files"]["content_roots"], [])
        self.assertFalse(template["vault"]["goals_read"])
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            valid = harness.run(
                harness.request("config.validate", config=harness.config())
            )
            self.assertTrue(valid["valid"])
            self.assertEqual(valid["config"]["vault"]["path"], str(harness.vault))

            unknown = harness.config()
            unknown["schema_version"] = 2
            result = harness.run(
                harness.request("config.validate", config=unknown), expected_exit=2
            )
            self.assertEqual(result["error"]["code"], "unsupported_schema")

            for unsafe in (
                Path(os.sep),
                harness.home,
                harness.home / "Library",
                harness.home / "Library" / "CloudStorage",
                harness.provider,
            ):
                config = harness.config()
                config["files"]["discovery_roots"][0]["path"] = str(unsafe)
                result = harness.run(
                    harness.request("config.validate", config=config),
                    expected_exit=2,
                )
                self.assertIn(result["error"]["code"], {"unsafe_path", "unsafe_scope"})

            missing_excludes = harness.config()
            missing_excludes["files"]["exclude_globs"] = ["node_modules/**"]
            result = harness.run(
                harness.request("config.validate", config=missing_excludes),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "unsafe_scope")
            self.assertCountEqual(
                result["error"]["details"]["required_exclude_globs"],
                ["Goals/**", "Reviews/Weekly/**"],
            )

            output = harness.vault / "Reviews" / "Weekly"
            output.mkdir(parents=True)
            inside_output = output / "Imported"
            inside_output.mkdir()
            config = harness.config(content_root=inside_output)
            result = harness.run(
                harness.request("config.validate", config=config), expected_exit=2
            )
            self.assertEqual(result["error"]["code"], "unsafe_scope")

            config = harness.config()
            config["vault"]["output_root"] = "Goals/Weekly"
            result = harness.run(
                harness.request("config.validate", config=config), expected_exit=2
            )
            self.assertEqual(result["error"]["code"], "unsafe_scope")

            linked = harness.home / "Documents" / "linked-school"
            linked.symlink_to(harness.school, target_is_directory=True)
            config = harness.config()
            config["files"]["discovery_roots"][0]["path"] = str(linked)
            result = harness.run(
                harness.request("config.validate", config=config), expected_exit=2
            )
            self.assertEqual(result["error"]["code"], "unsafe_path")

    def test_provider_id_limits_and_git_layout_match_first_cut_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            config = harness.config()
            config["mail"]["scopes"][0]["mailbox_id"] = "m" * 2048
            config["notes"]["scopes"][0]["folder_id"] = "n" * 512
            accepted = harness.run(
                harness.request("config.validate", config=config)
            )
            self.assertEqual(
                len(accepted["config"]["mail"]["scopes"][0]["mailbox_id"]),
                2048,
            )
            self.assertEqual(
                len(accepted["config"]["notes"]["scopes"][0]["folder_id"]),
                512,
            )

            config["mail"]["scopes"][0]["mailbox_id"] += "m"
            rejected = harness.run(
                harness.request("config.validate", config=config), expected_exit=2
            )
            self.assertEqual(rejected["error"]["code"], "validation_error")

            config = harness.config()
            config["notes"]["scopes"][0]["folder_id"] = "n" * 513
            rejected = harness.run(
                harness.request("config.validate", config=config), expected_exit=2
            )
            self.assertEqual(rejected["error"]["code"], "validation_error")

        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            marker = harness.repository / ".git"
            marker.rmdir()
            marker.write_text("gitdir: ../git-metadata\n", encoding="utf-8")
            rejected = harness.run(
                harness.request("config.validate", config=harness.config()),
                expected_exit=2,
            )
            self.assertEqual(rejected["error"]["code"], "unsafe_path")

        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            (harness.repository / ".git" / "worktrees").mkdir()
            rejected = harness.run(
                harness.request("config.validate", config=harness.config()),
                expected_exit=2,
            )
            self.assertEqual(rejected["error"]["code"], "unsafe_path")

    def test_mail_command_semantics_and_git_author_filters_are_exact(self) -> None:
        module = load_state_module()
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            sent = harness.run(
                harness.request("config.validate", config=harness.config())
            )["config"]
            self.assertEqual(sent["mail"]["scopes"][0]["scope_kind"], "sent")
            self.assertEqual(sent["mail"]["scopes"][0]["date_field"], "sent")
            self.assertEqual(
                sent["git"]["repositories"][0]["author_emails"],
                ["student@example.invalid"],
            )

            selected_config = harness.config()
            selected_scope = selected_config["mail"]["scopes"][0]
            selected_scope["scope_kind"] = "weekly_review_label"
            selected_scope["date_field"] = "received"
            selected = harness.run(
                harness.request("config.validate", config=selected_config)
            )["config"]
            self.assertEqual(
                selected["mail"]["scopes"][0]["date_field"], "received"
            )
            self.assertNotEqual(
                module._baseline_scope_fingerprint(sent),
                module._baseline_scope_fingerprint(selected),
            )

            invalid_mail_scopes = []
            missing_kind = dict(harness.config()["mail"]["scopes"][0])
            missing_kind.pop("scope_kind")
            invalid_mail_scopes.append(missing_kind)
            missing_date = dict(harness.config()["mail"]["scopes"][0])
            missing_date.pop("date_field")
            invalid_mail_scopes.append(missing_date)
            wrong_sent_date = dict(harness.config()["mail"]["scopes"][0])
            wrong_sent_date["date_field"] = "received"
            invalid_mail_scopes.append(wrong_sent_date)
            unknown_kind = dict(harness.config()["mail"]["scopes"][0])
            unknown_kind["scope_kind"] = "alias_inferred"
            invalid_mail_scopes.append(unknown_kind)
            unknown_key = dict(harness.config()["mail"]["scopes"][0])
            unknown_key["mail_command"] = "mail-list-sent"
            invalid_mail_scopes.append(unknown_key)
            for scope in invalid_mail_scopes:
                with self.subTest(scope=scope):
                    config = harness.config()
                    config["mail"]["scopes"] = [scope]
                    rejected = harness.run(
                        harness.request("config.validate", config=config),
                        expected_exit=2,
                    )
                    self.assertEqual(
                        rejected["error"]["code"], "validation_error"
                    )

            empty_authors = harness.config()
            empty_authors["git"]["repositories"][0]["author_emails"] = []
            accepted = harness.run(
                harness.request("config.validate", config=empty_authors)
            )
            self.assertEqual(
                accepted["config"]["git"]["repositories"][0]["author_emails"],
                [],
            )
            for authors in (
                ["Student@example.invalid", "student@example.invalid"],
                ["student@example.invalid", "\u00fcser@example.invalid"],
                [""],
            ):
                with self.subTest(authors=authors):
                    config = harness.config()
                    config["git"]["repositories"][0]["author_emails"] = authors
                    rejected = harness.run(
                        harness.request("config.validate", config=config),
                        expected_exit=2,
                    )
                    self.assertEqual(
                        rejected["error"]["code"], "validation_error"
                    )
            missing_authors = harness.config()
            missing_authors["git"]["repositories"][0].pop("author_emails")
            rejected = harness.run(
                harness.request("config.validate", config=missing_authors),
                expected_exit=2,
            )
            self.assertEqual(rejected["error"]["code"], "validation_error")

    def test_missing_live_paths_are_diagnostic_and_config_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            harness.school.rename(harness.school.with_name("School-moved"))
            harness.repository.rename(harness.repository.with_name("repository-moved"))

            recovered = harness.run(harness.request("config.get"))
            self.assertEqual(recovered["revision"], 1)
            self.assertFalse(recovered["live_paths_valid"])
            diagnostics = {
                item.get("id", item["field"]): item
                for item in recovered["path_diagnostics"]
            }
            self.assertEqual(diagnostics["school-files"]["status"], "unavailable")
            self.assertEqual(diagnostics["course-project"]["status"], "unavailable")
            self.assertEqual(diagnostics["obsidian-vault"]["status"], "available")
            status = harness.run(harness.request("maintenance.status"))
            self.assertEqual(status["config_revision"], 1)
            self.assertFalse(status["live_paths_valid"])

            compared = harness.run(
                harness.request(
                    "baseline.compare",
                    observations=[harness.observation(None, digest_text="still valid")],
                )
            )
            self.assertEqual(compared["comparisons"][0]["change"], "new")
            documents = harness.documents("2026-W36")
            staged = harness.stage(
                "partial-live-sources",
                "2026-W36",
                documents,
                [harness.observation(None, digest_text="still valid")],
                coverage={"files": "partial", "git": "unavailable"},
            )
            aborted = harness.run(
                harness.request(
                    "review.abort",
                    confirmed=True,
                    expected_config_revision=1,
                    expected_state_revision=staged["state_revision"],
                    review_id="partial-live-sources",
                )
            )
            self.assertTrue(aborted["aborted"])

            unchanged = harness.set_config(expected_revision=1, expected_exit=2)
            self.assertEqual(unchanged["error"]["code"], "unsafe_path")
            repaired_config = harness.config()
            repaired_config["files"]["discovery_roots"] = []
            repaired_config["git"]["repositories"] = []
            repaired = harness.set_config(repaired_config, expected_revision=1)
            self.assertEqual(repaired["revision"], 2)

        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            moved_vault = harness.vault.with_name("SylviaVault-moved")
            harness.vault.rename(moved_vault)
            recovered = harness.run(harness.request("config.get"))
            self.assertEqual(recovered["revision"], 1)
            self.assertFalse(recovered["live_paths_valid"])
            status = harness.run(harness.request("maintenance.status"))
            self.assertEqual(status["config_revision"], 1)

            documents = harness.documents("2026-W36")
            rejected = harness.run(
                harness.stage_request("missing-vault", "2026-W36", documents, []),
                expected_exit=2,
            )
            self.assertEqual(rejected["error"]["code"], "unsafe_path")
            self.assertFalse((harness.storage / "state-v1.json").exists())

            replacement_vault = harness.provider / "My Drive" / "ReplacementVault"
            (replacement_vault / ".obsidian").mkdir(parents=True)
            repaired_config = harness.config(content_root=replacement_vault)
            repaired_config["vault"]["path"] = str(replacement_vault)
            repaired = harness.set_config(repaired_config, expected_revision=1)
            self.assertEqual(repaired["revision"], 2)

    def test_config_confirmation_revision_permissions_and_encoded_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            rejected = harness.run(
                harness.request(
                    "config.set",
                    config=harness.config(),
                    confirmed=False,
                    expected_revision=0,
                ),
                expected_exit=2,
            )
            self.assertEqual(rejected["error"]["code"], "confirmation_required")
            self.assertFalse(harness.storage.exists())

            result = harness.set_config()
            self.assertEqual(result["revision"], 1)
            self.assertEqual(stat.S_IMODE(harness.storage.stat().st_mode), 0o700)
            config_path = harness.storage / "config-v1.json"
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE((harness.storage / ".weekly-review-state.lock").stat().st_mode),
                0o600,
            )

            stale = harness.set_config(expected_revision=0, expected_exit=2)
            self.assertEqual(stale["error"]["code"], "revision_conflict")
            original = config_path.read_bytes()
            too_large = harness.set_config(
                expected_revision=1,
                expected_exit=2,
                extra_env={"WEEKLY_REVIEW_STATE_TEST_MAX_CONFIG_BYTES": "256"},
            )
            self.assertEqual(too_large["error"]["code"], "state_too_large")
            self.assertEqual(config_path.read_bytes(), original)

            config_path.chmod(0o644)
            insecure = harness.run(harness.request("config.get"), expected_exit=2)
            self.assertEqual(insecure["error"]["code"], "unsafe_permissions")

    def test_private_storage_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            redirected = harness.base / "redirected-state"
            redirected.mkdir(mode=0o700)
            harness.storage.symlink_to(redirected, target_is_directory=True)
            result = harness.run(harness.request("config.get"), expected_exit=2)
            self.assertEqual(result["error"]["code"], "unsafe_test_override")

    def test_output_only_vault_and_observation_scope_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config(harness.config(snapshot_text=False, goals_read=False))
            goal = harness.observation(
                None,
                digest_text="goal hash",
                kind="goal",
                locator="Goals/G-001/G-001.md",
                scope_id="vault",
            )
            result = harness.run(
                harness.request("baseline.compare", observations=[goal]),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "scope_not_configured")

            enabled = harness.config(snapshot_text=False, goals_read=True)
            harness.set_config(enabled, expected_revision=1)
            accepted = harness.run(
                harness.request("baseline.compare", observations=[goal])
            )
            self.assertEqual(accepted["comparisons"][0]["change"], "new")

            outside_goal = dict(goal)
            outside_goal["source"] = dict(goal["source"])
            outside_goal["source"]["locator"] = "Notes/not-a-goal.md"
            result = harness.run(
                harness.request("baseline.compare", observations=[outside_goal]),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "scope_not_configured")

            for locator in (
                "Reviews/Weekly/2026/2026-W36.md",
                "reviews/weekly/2026/2026-W36.md",
                "Goals/G-001/G-001.md",
                "goals/G-001/G-001.md",
            ):
                item = harness.observation(None, digest_text="x", locator=locator)
                result = harness.run(
                    harness.request("baseline.compare", observations=[item]),
                    expected_exit=2,
                )
                self.assertEqual(result["error"]["code"], "excluded_observation")

    def test_stage_is_durable_global_lease_and_abort_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            window = harness.window("2026-W36")
            observation = harness.observation("private staged text\n")

            draft = harness.documents("2026-W36", status="draft", window=window)
            result = harness.run(
                harness.stage_request("review-draft", "2026-W36", draft, [observation]),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "report_not_confirmed")
            self.assertFalse((harness.storage / "state-v1.json").exists())

            confirmed = harness.documents("2026-W36", window=window)
            result = harness.run(
                harness.stage_request(
                    "review-unconfirmed",
                    "2026-W36",
                    confirmed,
                    [observation],
                    confirmed=False,
                ),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "confirmation_required")

            staged = harness.stage(
                "review-36", "2026-W36", confirmed, [observation], window=window
            )
            self.assertEqual(staged["state_revision"], 1)
            self.assertFalse((harness.vault / confirmed["report_relative"]).exists())
            self.assertFalse((harness.vault / confirmed["index_relative"]).exists())
            status = harness.run(harness.request("maintenance.status"))
            self.assertTrue(status["pending_review_active"])
            self.assertEqual(status["pending_review"]["review_id"], "review-36")
            self.assertEqual(status["pending_review"]["window"], window)
            self.assertEqual(status["pending_review"]["coverage"]["mail"], "declined")
            self.assertIn("+00:00", status["pending_review"]["staged_at"])

            state_text = (harness.storage / "state-v1.json").read_text(encoding="utf-8")
            self.assertNotIn(str(harness.vault), state_text)
            self.assertNotIn("mail-account-stable-id", state_text)
            self.assertNotIn("private staged text", state_text)

            unconfirmed_promote = harness.run(
                harness.promote_request(
                    "review-36",
                    confirmed,
                    expected_state_revision=1,
                    confirmed=False,
                ),
                expected_exit=2,
            )
            self.assertEqual(
                unconfirmed_promote["error"]["code"], "confirmation_required"
            )
            self.assertFalse((harness.vault / confirmed["report_relative"]).exists())

            next_documents = harness.documents("2026-W37")
            result = harness.run(
                harness.stage_request(
                    "review-37",
                    "2026-W37",
                    next_documents,
                    [],
                    expected_state_revision=1,
                ),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "pending_review_active")

            config_blocked = harness.set_config(expected_revision=1, expected_exit=2)
            self.assertEqual(config_blocked["error"]["code"], "pending_review_active")
            for operation in ("snapshots.purge", "baseline.reset"):
                result = harness.run(
                    harness.request(
                        operation,
                        confirmed=True,
                        expected_config_revision=1,
                        expected_state_revision=1,
                    ),
                    expected_exit=2,
                )
                self.assertEqual(result["error"]["code"], "pending_review_active")

            result = harness.run(
                harness.request(
                    "review.abort",
                    confirmed=False,
                    expected_config_revision=1,
                    expected_state_revision=1,
                    review_id="review-36",
                ),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "confirmation_required")
            aborted = harness.run(
                harness.request(
                    "review.abort",
                    confirmed=True,
                    expected_config_revision=1,
                    expected_state_revision=1,
                    review_id="review-36",
                )
            )
            self.assertTrue(aborted["aborted"])
            self.assertEqual(aborted["state_revision"], 2)
            self.assertFalse(
                harness.run(harness.request("maintenance.status"))["pending_review_active"]
            )
            self.assertEqual(list((harness.storage / "snapshots").iterdir()), [])

    def test_report_window_collected_through_and_generated_markers_fail_closed(self) -> None:
        cases = [
            {"frontmatter_collected": "2026-09-02T12:00:00+08:00"},
            {"timezone": "UTC"},
            {"period_start": "2026-08-30"},
            {"period_end_exclusive": "2026-09-08"},
            {"generated": "missing"},
            {"generated": "duplicate"},
            {"generated": "reversed"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as temporary:
                harness = IsolatedHarness(Path(temporary))
                harness.set_config()
                documents = harness.documents("2026-W36", **overrides)
                result = harness.run(
                    harness.stage_request(
                        "invalid-review",
                        "2026-W36",
                        documents,
                        [harness.observation("candidate\n")],
                    ),
                    expected_exit=2,
                )
                self.assertEqual(result["error"]["code"], "invalid_report")
                self.assertFalse((harness.storage / "state-v1.json").exists())

        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            start = dt.datetime.fromisoformat(harness.window("2026-W36")["start"])
            cutoff = start + dt.timedelta(days=2, hours=12)
            window = harness.window("2026-W36", cutoff)
            documents = harness.documents("2026-W36", window=window)
            staged = harness.stage(
                "wtd-review", "2026-W36", documents, [], window=window
            )
            self.assertEqual(staged["state_revision"], 1)

    def test_cas_conflict_never_overwrites_external_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            documents = harness.documents("2026-W36")
            staged = harness.stage(
                "cas-review",
                "2026-W36",
                documents,
                [harness.observation("stage source\n")],
            )
            request = harness.promote_request(
                "cas-review",
                documents,
                expected_state_revision=staged["state_revision"],
            )
            report_path = harness.vault / documents["report_relative"]
            report_path.parent.mkdir(parents=True)
            report_path.write_text("external editor content\n", encoding="utf-8")
            result = harness.run(request, expected_exit=2)
            self.assertEqual(result["error"]["code"], "document_conflict")
            self.assertEqual(report_path.read_text(encoding="utf-8"), "external editor content\n")
            status = harness.run(harness.request("maintenance.status"))
            self.assertTrue(status["pending_review_active"])
            self.assertIsNone(status["checkpoint_week_id"])

    def test_stage_freezes_preimages_and_promote_cannot_rebind_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            documents = harness.documents("2026-W36")
            staged = harness.stage("frozen-preimage", "2026-W36", documents, [])
            report_path = harness.vault / documents["report_relative"]
            report_path.parent.mkdir(parents=True)
            external = b"editor content after preview\n"
            report_path.write_bytes(external)

            rebound = harness.promote_request(
                "frozen-preimage",
                documents,
                expected_state_revision=staged["state_revision"],
            )
            rebound["report"]["expected_preimage"] = {
                "sha256": sha256_bytes(external),
                "state": "sha256",
            }
            result = harness.run(rebound, expected_exit=2)
            self.assertEqual(result["error"]["code"], "validation_error")
            self.assertEqual(
                harness.run(harness.request("maintenance.status"))["pending_review"][
                    "phase"
                ],
                "staged",
            )

            result = harness.run(
                harness.promote_request(
                    "frozen-preimage",
                    documents,
                    expected_state_revision=staged["state_revision"],
                ),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "document_conflict")
            self.assertEqual(report_path.read_bytes(), external)
            self.assertEqual(
                harness.run(harness.request("maintenance.status"))["pending_review"][
                    "phase"
                ],
                "writing",
            )

    def test_write_ahead_phase_blocks_abort_and_recovers_after_target_edit_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            documents = harness.documents("2026-W36")
            staged = harness.stage("wal-before-io", "2026-W36", documents, [])
            request = harness.promote_request(
                "wal-before-io",
                documents,
                expected_state_revision=staged["state_revision"],
            )
            failed = harness.run(
                request, expected_exit=2, failpoint="after_write_ahead"
            )
            self.assertEqual(failed["error"]["code"], "test_injected_failure")
            status = harness.run(harness.request("maintenance.status"))
            self.assertEqual(status["pending_review"]["phase"], "writing")
            self.assertFalse((harness.vault / "Reviews").exists())
            abort = harness.run(
                harness.request(
                    "review.abort",
                    confirmed=True,
                    expected_config_revision=1,
                    expected_state_revision=status["state_revision"],
                    review_id="wal-before-io",
                ),
                expected_exit=2,
            )
            self.assertEqual(abort["error"]["code"], "write_recovery_required")
            self.assertTrue(harness.run(request)["promoted"])

        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            documents = harness.documents("2026-W36")
            staged = harness.stage("wal-review", "2026-W36", documents, [])
            request = harness.promote_request(
                "wal-review", documents, expected_state_revision=staged["state_revision"]
            )
            failed = harness.run(
                request, expected_exit=2, failpoint="after_report_write"
            )
            self.assertEqual(failed["error"]["code"], "test_injected_failure")
            status = harness.run(harness.request("maintenance.status"))
            self.assertEqual(status["pending_review"]["phase"], "writing")

            report_path = harness.vault / documents["report_relative"]
            report_path.write_text("concurrent editor replacement\n", encoding="utf-8")
            abort = harness.run(
                harness.request(
                    "review.abort",
                    confirmed=True,
                    expected_config_revision=1,
                    expected_state_revision=status["state_revision"],
                    review_id="wal-review",
                ),
                expected_exit=2,
            )
            self.assertEqual(abort["error"]["code"], "write_recovery_required")
            conflict = harness.run(request, expected_exit=2)
            self.assertEqual(conflict["error"]["code"], "document_conflict")
            self.assertEqual(
                report_path.read_text(encoding="utf-8"),
                "concurrent editor replacement\n",
            )

            report_path.unlink()
            abort = harness.run(
                harness.request(
                    "review.abort",
                    confirmed=True,
                    expected_config_revision=1,
                    expected_state_revision=status["state_revision"],
                    review_id="wal-review",
                ),
                expected_exit=2,
            )
            self.assertEqual(abort["error"]["code"], "write_recovery_required")
            recovered = harness.run(request)
            self.assertTrue(recovered["promoted"])
            self.assertEqual(report_path.read_text(encoding="utf-8"), documents["report_text"])

    def test_claimed_preimage_crash_recovers_from_deterministic_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            old_report = b"previous report body\n"
            old_index = b"previous index body\n"
            report_path = harness.vault / "Reviews/Weekly/2026/2026-W36.md"
            index_path = harness.vault / "Reviews/Weekly/Weekly Reviews.md"
            report_path.parent.mkdir(parents=True)
            report_path.write_bytes(old_report)
            index_path.write_bytes(old_index)
            documents = harness.documents("2026-W36")
            staged = harness.stage("claimed-recovery", "2026-W36", documents, [])
            request = harness.promote_request(
                "claimed-recovery",
                documents,
                expected_state_revision=staged["state_revision"],
            )
            failed = harness.run(
                request, expected_exit=2, failpoint="after_report_claim"
            )
            self.assertEqual(failed["error"]["code"], "test_injected_failure")
            self.assertFalse(report_path.exists())
            artifacts = list(report_path.parent.glob(".weekly-review-cas-v1-*-report.*"))
            self.assertEqual(len(artifacts), 2)
            self.assertIn(old_report, [path.read_bytes() for path in artifacts])

            promoted = harness.run(request)
            self.assertTrue(promoted["promoted"])
            self.assertEqual(report_path.read_text(encoding="utf-8"), documents["report_text"])
            self.assertEqual(
                list(report_path.parent.glob(".weekly-review-cas-v1-*-report.*")), []
            )

    @unittest.skipUnless(sys.platform == "darwin", "Darwin renameatx_np contract")
    def test_real_rename_excl_interleaving_preserves_concurrent_target(self) -> None:
        module = load_state_module()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target_name = "document.md"
            old = b"confirmed preimage\n"
            desired = b"confirmed target\n"
            external = b"concurrent editor target\n"
            (directory / target_name).write_bytes(old)
            descriptor = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            request_digest = "a" * 64
            staged_name, backup_name = module._attempt_artifact_names(
                request_digest, "report"
            )
            original_rename = module._rename_noreplace
            injected = False

            def interleaving_rename(
                source_fd: int, source: str, target_fd: int, target: str
            ) -> None:
                nonlocal injected
                if source == staged_name and target == target_name and not injected:
                    injected = True
                    racer = os.open(
                        target_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=target_fd,
                    )
                    try:
                        os.write(racer, external)
                        os.fsync(racer)
                    finally:
                        os.close(racer)
                original_rename(source_fd, source, target_fd, target)

            module._rename_noreplace = interleaving_rename
            try:
                with self.assertRaises(module.ContractError) as raised:
                    module._conditional_install_at(
                        descriptor,
                        target_name,
                        desired,
                        {"sha256": sha256_bytes(old), "state": "sha256"},
                        request_digest,
                        "report",
                        4096,
                        "Interleaving report",
                        False,
                    )
                self.assertEqual(raised.exception.code, "document_conflict")
                self.assertEqual((directory / target_name).read_bytes(), external)
                self.assertEqual((directory / backup_name).read_bytes(), old)
                self.assertEqual((directory / staged_name).read_bytes(), desired)
            finally:
                module._rename_noreplace = original_rename
                os.close(descriptor)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin renameatx_np contract")
    def test_atomic_editor_swap_during_claim_is_restored_not_overwritten(self) -> None:
        module = load_state_module()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target_name = "document.md"
            old = b"confirmed preimage\n"
            desired = b"confirmed target\n"
            editor = b"atomic editor replacement\n"
            (directory / target_name).write_bytes(old)
            descriptor = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            request_digest = "b" * 64
            staged_name, backup_name = module._attempt_artifact_names(
                request_digest, "report"
            )
            original_rename = module._rename_noreplace
            injected = False

            def interleaving_rename(
                source_fd: int, source: str, target_fd: int, target: str
            ) -> None:
                nonlocal injected
                if source == target_name and target == backup_name and not injected:
                    injected = True
                    editor_name = ".editor-replacement"
                    writer = os.open(
                        editor_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=source_fd,
                    )
                    try:
                        os.write(writer, editor)
                        os.fsync(writer)
                    finally:
                        os.close(writer)
                    os.replace(
                        directory / editor_name,
                        directory / target_name,
                    )
                original_rename(source_fd, source, target_fd, target)

            module._rename_noreplace = interleaving_rename
            try:
                with self.assertRaises(module.ContractError) as raised:
                    module._conditional_install_at(
                        descriptor,
                        target_name,
                        desired,
                        {"sha256": sha256_bytes(old), "state": "sha256"},
                        request_digest,
                        "report",
                        4096,
                        "Editor-swap report",
                        False,
                    )
                self.assertEqual(
                    raised.exception.code, "document_conflict_preserved"
                )
                self.assertEqual((directory / target_name).read_bytes(), editor)
                self.assertFalse((directory / backup_name).exists())
                self.assertEqual((directory / staged_name).read_bytes(), desired)
            finally:
                module._rename_noreplace = original_rename
                os.close(descriptor)

    def test_bound_directory_redirect_never_writes_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            documents = harness.documents("2026-W36")
            staged = harness.stage("redirect-review", "2026-W36", documents, [])
            request = harness.promote_request(
                "redirect-review",
                documents,
                expected_state_revision=staged["state_revision"],
            )
            failed = harness.run(
                request, expected_exit=2, failpoint="after_directory_bind"
            )
            self.assertEqual(failed["error"]["code"], "test_injected_failure")

            output = harness.vault / "Reviews" / "Weekly"
            bound = harness.vault / "Reviews" / "Weekly-bound"
            outside = harness.base / "redirect-target"
            outside.mkdir()
            output.rename(bound)
            output.symlink_to(outside, target_is_directory=True)
            result = harness.run(request, expected_exit=2)
            self.assertIn(result["error"]["code"], {"unsafe_path", "unsafe_report"})
            self.assertEqual(list(outside.iterdir()), [])

            output.unlink()
            bound.rename(output)
            promoted = harness.run(request)
            self.assertTrue(promoted["promoted"])
            self.assertEqual(list(outside.iterdir()), [])

    def test_partial_write_failures_recover_same_stage_and_retry_is_idempotent(self) -> None:
        for failpoint in ("after_report_write", "after_index_write", "before_state_promote"):
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as temporary:
                harness = IsolatedHarness(Path(temporary))
                harness.set_config()
                documents = harness.documents("2026-W36")
                staged = harness.stage(
                    "recover-review",
                    "2026-W36",
                    documents,
                    [harness.observation("staged source\n")],
                )
                request = harness.promote_request(
                    "recover-review",
                    documents,
                    expected_state_revision=staged["state_revision"],
                )
                failed = harness.run(request, expected_exit=2, failpoint=failpoint)
                self.assertEqual(failed["error"]["code"], "test_injected_failure")
                self.assertTrue(
                    harness.run(harness.request("maintenance.status"))[
                        "pending_review_active"
                    ]
                )

                if failpoint == "after_report_write":
                    abort = harness.run(
                        harness.request(
                            "review.abort",
                            confirmed=True,
                            expected_config_revision=1,
                            expected_state_revision=1,
                            review_id="recover-review",
                        ),
                        expected_exit=2,
                    )
                    self.assertEqual(abort["error"]["code"], "write_recovery_required")

                promoted = harness.run(request)
                self.assertTrue(promoted["promoted"])
                self.assertEqual(promoted["state_revision"], 4)
                self.assertEqual(
                    (harness.vault / documents["report_relative"]).read_text(
                        encoding="utf-8"
                    ),
                    documents["report_text"],
                )
                self.assertEqual(
                    (harness.vault / documents["index_relative"]).read_text(
                        encoding="utf-8"
                    ),
                    documents["index_text"],
                )
                retried = harness.run(request)
                self.assertTrue(retried["already_promoted"])
                self.assertEqual(retried["state_revision"], 4)

    def test_source_change_after_stage_promotes_staged_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            old_text = "source at preview time\n"
            new_text = "source changed after preview\n"
            documents = harness.documents("2026-W36")
            staged = harness.stage(
                "stable-stage",
                "2026-W36",
                documents,
                [harness.observation(old_text)],
            )
            promoted = harness.run(
                harness.promote_request(
                    "stable-stage",
                    documents,
                    expected_state_revision=staged["state_revision"],
                )
            )
            self.assertTrue(promoted["promoted"])
            compared = harness.run(
                harness.request(
                    "baseline.compare", observations=[harness.observation(new_text)]
                )
            )
            comparison = compared["comparisons"][0]
            self.assertEqual(comparison["change"], "modified")
            self.assertEqual(comparison["content_diff"]["status"], "computed")
            self.assertIn("-source at preview time", comparison["content_diff"]["lines"])
            self.assertIn("+source changed after preview", comparison["content_diff"]["lines"])

    def test_incremental_merge_retains_quiet_sources_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            first = "course paper v1\n"
            _, _, first_promote = harness.commit_review(
                "review-36", "2026-W36", [harness.observation(first)]
            )
            self.assertEqual(first_promote["state_revision"], 4)

            second = harness.observation(
                "different file this week\n",
                item_id="F002",
                locator="Notes/different-file.md",
            )
            _, _, second_promote = harness.commit_review(
                "review-37",
                "2026-W37",
                [second],
                expected_state_revision=4,
                coverage={"files": "partial", "mail": "unavailable"},
            )
            self.assertEqual(second_promote["baseline_count"], 2)
            self.assertEqual(second_promote["snapshot_count"], 2)

            state_path = harness.storage / "state-v1.json"
            snapshot_paths = list((harness.storage / "snapshots").iterdir())
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
            self.assertTrue(snapshot_paths)
            self.assertTrue(
                all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in snapshot_paths)
            )
            persisted = state_path.read_text(encoding="utf-8")
            self.assertNotIn("course paper v1", persisted)
            self.assertNotIn(str(harness.vault), persisted)

            later = harness.run(
                harness.request(
                    "baseline.compare",
                    observations=[harness.observation("course paper v2\n")],
                )
            )["comparisons"][0]
            self.assertEqual(later["change"], "modified")
            self.assertIn("-course paper v1", later["content_diff"]["lines"])
            self.assertEqual(len(list((harness.storage / "snapshots").iterdir())), 2)

    def test_receipt_is_immutable_nonreusable_and_survives_config_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config(harness.config(snapshot_text=False))
            documents, staged, promoted = harness.commit_review(
                "receipt-review",
                "2026-W36",
                [harness.observation(None, digest_text="receipt baseline")],
            )
            original_request = harness.promote_request(
                "receipt-review",
                documents,
                expected_state_revision=staged["state_revision"],
            )
            self.assertEqual(promoted["state_revision"], 4)

            harness.set_config(
                harness.config(snapshot_text=False), expected_revision=1
            )
            reset = harness.run(
                harness.request(
                    "baseline.reset",
                    confirmed=True,
                    expected_config_revision=2,
                    expected_state_revision=4,
                )
            )
            self.assertEqual(reset["state_revision"], 5)
            replay = harness.run(original_request)
            self.assertTrue(replay["already_promoted"])
            self.assertEqual(replay["state_revision"], 5)

            moved_vault = harness.vault.with_name("SylviaVault-temporarily-moved")
            harness.vault.rename(moved_vault)
            unavailable_replay = harness.run(original_request)
            self.assertTrue(unavailable_replay["already_promoted"])
            self.assertEqual(unavailable_replay["state_revision"], 5)
            moved_vault.rename(harness.vault)

            status = harness.run(harness.request("maintenance.status"))
            self.assertEqual(status["receipt_count"], 1)
            self.assertEqual(
                status["latest_receipt"]["review_id"], "receipt-review"
            )
            self.assertEqual(
                status["latest_receipt"]["request_digest"],
                promoted["request_digest"],
            )

            next_documents = harness.documents("2026-W37")
            reused = harness.run(
                harness.stage_request(
                    "receipt-review",
                    "2026-W37",
                    next_documents,
                    [],
                    expected_config_revision=2,
                    expected_state_revision=5,
                ),
                expected_exit=2,
            )
            self.assertEqual(reused["error"]["code"], "review_id_conflict")

            changed_request = json.loads(json.dumps(original_request))
            changed_request["report"]["target_text"] += "\nchanged replay\n"
            changed = harness.run(changed_request, expected_exit=2)
            self.assertEqual(changed["error"]["code"], "review_id_conflict")

    def test_eventkit_container_membership_and_state_privacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            config = harness.config(snapshot_text=False)
            config["eventkit"]["calendar_ids"].append("calendar-second-id")
            harness.set_config(config)

            missing = harness.observation(
                None,
                digest_text="calendar hash",
                kind="calendar",
                locator="event-stable-id",
                scope_id="eventkit",
            )
            result = harness.run(
                harness.request("baseline.compare", observations=[missing]),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "validation_error")

            empty = json.loads(json.dumps(missing))
            empty["source"]["container_id"] = ""
            result = harness.run(
                harness.request("baseline.compare", observations=[empty]),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "validation_error")

            mismatch = json.loads(json.dumps(missing))
            mismatch["source"]["container_id"] = "unconfigured-calendar"
            result = harness.run(
                harness.request("baseline.compare", observations=[mismatch]),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "container_not_configured")

            first = harness.observation(
                None,
                digest_text="calendar hash one",
                item_id="C001",
                kind="calendar",
                locator="event-stable-id",
                scope_id="eventkit",
                container_id="calendar-stable-id",
            )
            second = harness.observation(
                None,
                digest_text="calendar hash two",
                item_id="C002",
                kind="calendar",
                locator="event-stable-id",
                scope_id="eventkit",
                container_id="calendar-second-id",
            )
            reminder = harness.observation(
                None,
                digest_text="reminder hash",
                item_id="R001",
                kind="reminder",
                locator="reminder-stable-id",
                scope_id="eventkit",
                container_id="reminders-stable-id",
            )
            _, _, promoted = harness.commit_review(
                "eventkit-review", "2026-W36", [first, second, reminder]
            )
            self.assertEqual(promoted["baseline_count"], 3)
            persisted = (harness.storage / "state-v1.json").read_text(
                encoding="utf-8"
            )
            for secret in (
                "calendar-stable-id",
                "calendar-second-id",
                "reminders-stable-id",
                "event-stable-id",
                "reminder-stable-id",
            ):
                self.assertNotIn(secret, persisted)

    def test_eventkit_identifiers_use_exact_utf8_byte_limits(self) -> None:
        calendar_id = "日" * 1365 + "c"
        reminder_id = "列" * 1365 + "r"
        item_locator = "事" * 1365 + "i"
        self.assertEqual(len(calendar_id.encode("utf-8")), 4096)
        self.assertEqual(len(reminder_id.encode("utf-8")), 4096)
        self.assertEqual(len(item_locator.encode("utf-8")), 4096)

        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            config = harness.config(snapshot_text=False)
            config["eventkit"]["calendar_ids"] = [calendar_id]
            config["eventkit"]["reminder_list_ids"] = [reminder_id]
            harness.set_config(config)
            compared = harness.run(
                harness.request(
                    "baseline.compare",
                    observations=[
                        harness.observation(
                            None,
                            digest_text="calendar boundary",
                            item_id="C-boundary",
                            kind="calendar",
                            locator=item_locator,
                            scope_id="eventkit",
                            container_id=calendar_id,
                        ),
                        harness.observation(
                            None,
                            digest_text="reminder boundary",
                            item_id="R-boundary",
                            kind="reminder",
                            locator=item_locator,
                            scope_id="eventkit",
                            container_id=reminder_id,
                        ),
                    ],
                )
            )
            self.assertEqual(
                [item["change"] for item in compared["comparisons"]],
                ["new", "new"],
            )

            oversized_locator = harness.observation(
                None,
                digest_text="oversized locator",
                kind="calendar",
                locator="事" * 1366,
                scope_id="eventkit",
                container_id=calendar_id,
            )
            rejected = harness.run(
                harness.request(
                    "baseline.compare", observations=[oversized_locator]
                ),
                expected_exit=2,
            )
            self.assertEqual(rejected["error"]["code"], "validation_error")

        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            for invalid_id in ("日" * 1366, "calendar\x00id", "calendar\nid"):
                with self.subTest(invalid_id_bytes=len(invalid_id.encode("utf-8"))):
                    config = harness.config()
                    config["eventkit"]["calendar_ids"] = [invalid_id]
                    rejected = harness.run(
                        harness.request("config.validate", config=config),
                        expected_exit=2,
                    )
                    self.assertEqual(
                        rejected["error"]["code"], "validation_error"
                    )

    def test_index_confirmation_token_grammar_is_exact(self) -> None:
        variants = (
            "unconfirmed",
            "not-confirmed",
            "preconfirmed",
            "confirmed-suffix",
            "confirmed prefix",
        )
        for token in variants:
            with self.subTest(token=token), tempfile.TemporaryDirectory() as temporary:
                harness = IsolatedHarness(Path(temporary))
                harness.set_config()
                documents = harness.documents("2026-W36")
                documents["index_text"] = documents["index_text"].replace(
                    " · confirmed · ", f" · {token} · "
                )
                documents["index_sha256"] = sha256_text(documents["index_text"])
                result = harness.run(
                    harness.stage_request(
                        "bad-index", "2026-W36", documents, []
                    ),
                    expected_exit=2,
                )
                self.assertEqual(result["error"]["code"], "invalid_index")

    def test_config_cap_lowering_requires_explicit_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            config = harness.config(snapshot_text=False)
            config["limits"]["max_baseline_entries"] = 2
            harness.set_config(config)
            observations = [
                harness.observation(None, digest_text="one"),
                harness.observation(
                    None,
                    digest_text="two",
                    item_id="F002",
                    locator="Notes/two.md",
                ),
            ]
            harness.commit_review("cap-baseline", "2026-W36", observations)
            lowered = harness.config(snapshot_text=False)
            lowered["limits"]["max_baseline_entries"] = 1
            result = harness.set_config(
                lowered, expected_revision=1, expected_exit=2
            )
            self.assertEqual(result["error"]["code"], "baseline_reset_required")
            self.assertEqual(result["error"]["details"]["retained_baseline_entries"], 2)

        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            harness.commit_review(
                "cap-snapshot",
                "2026-W36",
                [harness.observation("s" * 300)],
            )
            lowered = harness.config()
            lowered["limits"]["snapshot_max_file_bytes"] = 256
            lowered["limits"]["snapshot_max_total_bytes"] = 256
            result = harness.set_config(
                lowered, expected_revision=1, expected_exit=2
            )
            self.assertEqual(result["error"]["code"], "snapshot_purge_required")
            self.assertEqual(result["error"]["details"]["largest_retained_snapshot_bytes"], 300)

    def test_baseline_and_snapshot_caps_fail_without_silent_discard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            config = harness.config()
            config["limits"]["max_baseline_entries"] = 1
            harness.set_config(config)
            harness.commit_review(
                "review-36", "2026-W36", [harness.observation("first\n")]
            )
            before = (harness.storage / "state-v1.json").read_bytes()
            documents = harness.documents("2026-W37")
            second = harness.observation(
                "second\n", item_id="F002", locator="Notes/second.md"
            )
            result = harness.run(
                harness.stage_request(
                    "review-37",
                    "2026-W37",
                    documents,
                    [second],
                    expected_state_revision=4,
                ),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "baseline_limit")
            self.assertEqual((harness.storage / "state-v1.json").read_bytes(), before)
            self.assertEqual(len(list((harness.storage / "snapshots").iterdir())), 1)

        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            config = harness.config()
            config["limits"]["snapshot_max_file_bytes"] = 256
            config["limits"]["snapshot_max_total_bytes"] = 256
            harness.set_config(config)
            harness.commit_review(
                "review-36", "2026-W36", [harness.observation("a" * 150)]
            )
            before = (harness.storage / "state-v1.json").read_bytes()
            documents = harness.documents("2026-W37")
            second = harness.observation(
                "b" * 150, item_id="F002", locator="Notes/second.md"
            )
            result = harness.run(
                harness.stage_request(
                    "review-37",
                    "2026-W37",
                    documents,
                    [second],
                    expected_state_revision=4,
                ),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "snapshot_limit")
            self.assertEqual((harness.storage / "state-v1.json").read_bytes(), before)
            self.assertEqual(len(list((harness.storage / "snapshots").iterdir())), 1)

    def test_snapshot_opt_out_requires_confirmed_purge_and_keeps_hash_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            original = "opt-in source body\n"
            harness.commit_review(
                "review-36", "2026-W36", [harness.observation(original)]
            )
            disabled = harness.config(snapshot_text=False)
            blocked = harness.set_config(
                disabled, expected_revision=1, expected_exit=2
            )
            self.assertEqual(blocked["error"]["code"], "snapshot_purge_required")
            status = harness.run(harness.request("maintenance.status"))
            self.assertEqual(status["snapshot_count"], 1)
            self.assertEqual(status["baseline_count"], 1)

            unconfirmed = harness.run(
                harness.request(
                    "snapshots.purge",
                    confirmed=False,
                    expected_config_revision=1,
                    expected_state_revision=4,
                ),
                expected_exit=2,
            )
            self.assertEqual(unconfirmed["error"]["code"], "confirmation_required")
            purged = harness.run(
                harness.request(
                    "snapshots.purge",
                    confirmed=True,
                    expected_config_revision=1,
                    expected_state_revision=4,
                )
            )
            self.assertTrue(purged["hash_baseline_preserved"])
            self.assertEqual(purged["state_revision"], 5)
            self.assertEqual(list((harness.storage / "snapshots").iterdir()), [])
            self.assertNotIn(
                "snapshot_name",
                (harness.storage / "state-v1.json").read_text(encoding="utf-8"),
            )

            harness.set_config(disabled, expected_revision=1)
            compared = harness.run(
                harness.request(
                    "baseline.compare",
                    observations=[harness.observation(None, digest_text=original)],
                )
            )
            self.assertEqual(compared["comparisons"][0]["change"], "unchanged")

    def test_scope_change_requires_explicit_baseline_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config(harness.config(snapshot_text=False))
            original = "hash-only baseline\n"
            harness.commit_review(
                "review-36",
                "2026-W36",
                [harness.observation(None, digest_text=original)],
            )
            alternate = harness.home / "Documents" / "DifferentCourseRoot"
            alternate.mkdir(parents=True)
            changed = harness.config(snapshot_text=False, content_root=alternate)
            harness.set_config(changed, expected_revision=1)
            status = harness.run(harness.request("maintenance.status"))
            self.assertTrue(status["reset_required"])
            result = harness.run(
                harness.request(
                    "baseline.compare",
                    observations=[harness.observation(None, digest_text=original)],
                ),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "baseline_incompatible")

            documents = harness.documents("2026-W37")
            result = harness.run(
                harness.stage_request(
                    "review-37",
                    "2026-W37",
                    documents,
                    [],
                    expected_config_revision=2,
                    expected_state_revision=4,
                ),
                expected_exit=2,
            )
            self.assertEqual(result["error"]["code"], "baseline_incompatible")
            reset = harness.run(
                harness.request(
                    "baseline.reset",
                    confirmed=True,
                    expected_config_revision=2,
                    expected_state_revision=4,
                )
            )
            self.assertEqual(reset["baseline_count_cleared"], 1)
            self.assertEqual(reset["state_revision"], 5)
            self.assertFalse(
                harness.run(harness.request("maintenance.status"))["reset_required"]
            )

    def test_goals_read_authorization_is_part_of_scope_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config(harness.config(snapshot_text=False, goals_read=False))
            harness.commit_review(
                "review-36",
                "2026-W36",
                [harness.observation(None, digest_text="baseline")],
            )
            harness.set_config(
                harness.config(snapshot_text=False, goals_read=True),
                expected_revision=1,
            )
            status = harness.run(harness.request("maintenance.status"))
            self.assertEqual(
                status["baseline_compatibility"]["status"], "incompatible"
            )
            self.assertTrue(status["reset_required"])

    def test_state_size_is_checked_before_snapshots_or_documents_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            documents = harness.documents("2026-W36")
            result = harness.run(
                harness.stage_request(
                    "oversize-review",
                    "2026-W36",
                    documents,
                    [harness.observation("snapshot body\n")],
                ),
                expected_exit=2,
                extra_env={"WEEKLY_REVIEW_STATE_TEST_MAX_STATE_BYTES": "512"},
            )
            self.assertEqual(result["error"]["code"], "state_too_large")
            self.assertFalse((harness.storage / "state-v1.json").exists())
            self.assertFalse((harness.storage / "snapshots").exists())
            self.assertFalse((harness.vault / documents["report_relative"]).exists())
            self.assertFalse((harness.vault / documents["index_relative"]).exists())

    def test_promotion_size_preflight_stays_staged_before_wal_and_vault_io(self) -> None:
        module = load_state_module()
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            documents = harness.documents("2026-W36")
            staged = harness.stage("promotion-preflight", "2026-W36", documents, [])
            state_path = harness.storage / "state-v1.json"
            staged_bytes = state_path.read_bytes()
            state = json.loads(staged_bytes)
            pending = dict(state["pending_review"])
            started_at = state["updated_at"]
            request_digest = module._promotion_request_digest(
                "promotion-preflight",
                documents["report_relative"],
                documents["report_text"],
                documents["index_relative"],
                documents["index_text"],
            )
            attempt = {
                "output_identity": None,
                "report_parent_identity": None,
                "request_digest": request_digest,
                "started_at": started_at,
                "starting_state_revision": state["revision"],
            }
            pending["attempt"] = attempt
            pending["phase"] = "writing"
            wal_state = {
                "checkpoint": state["checkpoint"],
                "pending_review": pending,
                "receipts": state["receipts"],
                "revision": state["revision"] + 1,
                "schema_version": module.SCHEMA_VERSION,
                "updated_at": started_at,
            }
            wal_size = len(
                module._encode_json_document(
                    wal_state, module.MAX_STATE_BYTES, module.STATE_NAME
                )
            )
            bound_attempt = dict(attempt)
            bound_attempt["output_identity"] = "0" * 64
            bound_attempt["report_parent_identity"] = "0" * 64
            bound_pending = dict(pending)
            bound_pending["attempt"] = bound_attempt
            bound_state = {
                "checkpoint": state["checkpoint"],
                "pending_review": bound_pending,
                "receipts": state["receipts"],
                "revision": wal_state["revision"] + 1,
                "schema_version": module.SCHEMA_VERSION,
                "updated_at": started_at,
            }
            bound_size = len(
                module._encode_json_document(
                    bound_state, module.MAX_STATE_BYTES, module.STATE_NAME
                )
            )
            self.assertLessEqual(len(staged_bytes), wal_size)
            self.assertLess(wal_size, bound_size)

            rejected = harness.run(
                harness.promote_request(
                    "promotion-preflight",
                    documents,
                    expected_state_revision=staged["state_revision"],
                ),
                expected_exit=2,
                extra_env={
                    "WEEKLY_REVIEW_STATE_TEST_MAX_STATE_BYTES": str(wal_size)
                },
            )
            self.assertEqual(rejected["error"]["code"], "state_too_large")
            self.assertEqual(rejected["error"]["details"]["encoded_bytes"], bound_size)
            self.assertEqual(state_path.read_bytes(), staged_bytes)
            status = harness.run(harness.request("maintenance.status"))
            self.assertEqual(status["pending_review"]["phase"], "staged")
            self.assertEqual(status["state_revision"], staged["state_revision"])
            self.assertFalse((harness.vault / "Reviews").exists())

    def test_private_atomic_failure_recovers_orphan_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            documents = harness.documents("2026-W36")
            request = harness.stage_request(
                "atomic-review",
                "2026-W36",
                documents,
                [harness.observation("orphan candidate\n")],
            )
            failed = harness.run(
                request,
                expected_exit=2,
                failpoint="before_stage_state_replace",
            )
            self.assertEqual(failed["error"]["code"], "test_injected_failure")
            self.assertFalse((harness.storage / "state-v1.json").exists())
            self.assertEqual(len(list((harness.storage / "snapshots").iterdir())), 1)
            compared = harness.run(
                harness.request(
                    "baseline.compare",
                    observations=[harness.observation("orphan candidate\n")],
                )
            )
            self.assertEqual(compared["comparisons"][0]["change"], "new")
            self.assertEqual(list((harness.storage / "snapshots").iterdir()), [])
            staged = harness.run(request)
            self.assertEqual(staged["state_revision"], 1)

    def test_unknown_state_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = IsolatedHarness(Path(temporary))
            harness.set_config()
            state_path = harness.storage / "state-v1.json"
            state_path.write_text(
                json.dumps(
                    {
                        "checkpoint": None,
                        "pending_review": None,
                        "revision": 1,
                        "schema_version": 999,
                        "updated_at": "2026-09-02T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            state_path.chmod(0o600)
            result = harness.run(
                harness.request("baseline.compare", observations=[]), expected_exit=2
            )
            self.assertEqual(result["error"]["code"], "unsupported_schema")


if __name__ == "__main__":
    unittest.main(verbosity=2)
