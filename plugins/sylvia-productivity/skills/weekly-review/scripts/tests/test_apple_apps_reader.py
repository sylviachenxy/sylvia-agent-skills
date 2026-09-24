#!/usr/bin/env python3
"""Offline tests for the Apple Notes/Mail read-only adapter.

The production launcher is never executed here. JavaScript behavior runs in a
Node-only fixture seam, and launcher behavior uses a copied script whose
absolute osascript path is replaced with a synthetic local child.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
JXA_SCRIPT = SCRIPTS_DIR / "apple-apps-reader.js"
LAUNCHER = SCRIPTS_DIR / "apple-apps-reader.sh"
NODE = shutil.which("node")

NODE_HARNESS = r"""
const fs = require("fs");
const adapter = require(process.argv[1]);
const request = JSON.parse(fs.readFileSync(0, "utf8"));
let output;
if (request.action === "fixture") {
    output = adapter.__test.runWithFixture(request.arguments || [], request.fixture || {});
} else if (request.action === "serialize") {
    output = adapter.__test.serializeForTest(
        request.response,
        request.command || "unknown",
        request.app_contacted === true
    );
} else {
    output = adapter.run(request.arguments || []);
}
process.stdout.write(output + "\n");
"""


def base_fixture(*, notes: list[dict] | None = None, messages: list[dict] | None = None) -> dict:
    return {
        "Notes": {
            "accounts": [
                {
                    "id": "notes-account",
                    "name": "Notes Account",
                    "folders": [
                        {
                            "id": "notes-folder",
                            "name": "Weekly Review",
                            "shared": False,
                            "folders": [],
                            "notes": notes or [],
                        }
                    ],
                }
            ]
        },
        "Mail": {
            "accounts": [
                {
                    "id": "mail-account",
                    "name": "Mail Account",
                    "emailAddresses": ["owner@example.test"],
                    "mailboxes": [
                        {
                            "name": "Sent",
                            "mailboxes": [],
                            "messages": messages or [],
                        }
                    ],
                }
            ]
        },
    }


@unittest.skipUnless(NODE, "Node is required for the offline JavaScript fixture seam")
class OfflineCommandTests(unittest.TestCase):
    def run_adapter(
        self,
        *arguments: str,
        fixture: dict | None = None,
        action: str = "run",
        response: dict | None = None,
        command: str = "unknown",
        app_contacted: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        request = {
            "action": action,
            "arguments": list(arguments),
            "fixture": fixture,
            "response": response,
            "command": command,
            "app_contacted": app_contacted,
        }
        completed = subprocess.run(
            [str(NODE), "-e", NODE_HARNESS, str(JXA_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            input=json.dumps(request),
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(completed.stderr, "")
        lines = completed.stdout.splitlines()
        self.assertEqual(
            len(lines),
            1,
            msg=f"stdout must contain exactly one JSON line; got {completed.stdout!r}",
        )
        payload = json.loads(lines[0])
        self.assertIsInstance(payload, dict)
        return completed, payload

    def test_capabilities_is_offline_and_describes_narrow_surface(self) -> None:
        completed, payload = self.run_adapter("capabilities")

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["app_contacted"])
        self.assertIn("notes-list", payload["data"]["live_commands"])
        self.assertIn("mail-list-sent", payload["data"]["live_commands"])
        self.assertIn("mail-list-selected", payload["data"]["live_commands"])
        self.assertTrue(payload["data"]["notes"]["locked_notes_excluded"])
        self.assertTrue(payload["data"]["notes"]["shared_notes_excluded"])
        self.assertTrue(payload["data"]["mail"]["no_mutations"])

    def test_fixture_self_test_checks_half_open_window_and_bounded_text(self) -> None:
        completed, payload = self.run_adapter("self-test")

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(payload["data"]["passed"])
        self.assertTrue(payload["data"]["fixture_only"])
        self.assertFalse(payload["data"]["live_apps_contacted"])
        checks = {item["name"]: item["passed"] for item in payload["data"]["checks"]}
        self.assertTrue(checks["half-open-window"])
        self.assertTrue(checks["bounded-untrusted-text"])
        self.assertTrue(checks["account-scoped-mailbox-selector"])
        self.assertTrue(checks["explicit-live-gate-parser"])

    def test_help_is_offline(self) -> None:
        completed, payload = self.run_adapter("help")

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "help")
        self.assertFalse(payload["app_contacted"])

    def test_live_command_without_confirmation_fails_before_app_contact(self) -> None:
        completed, payload = self.run_adapter("notes-accounts", "--limit", "5")

        self.assertEqual(completed.returncode, 0)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["app_contacted"])
        self.assertEqual(
            payload["error"]["code"],
            "AUTOMATION_CONFIRMATION_REQUIRED",
        )
        self.assertNotIn("Notes", completed.stderr)
        self.assertNotIn("Mail", completed.stderr)

    def test_unknown_command_returns_sanitized_error(self) -> None:
        completed, payload = self.run_adapter("not-a-command", "--private", "secret")

        self.assertEqual(completed.returncode, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "unknown")
        self.assertFalse(payload["app_contacted"])
        self.assertEqual(payload["error"]["code"], "UNKNOWN_COMMAND")
        self.assertNotIn("secret", completed.stdout)
        self.assertNotIn("secret", completed.stderr)

    def test_selected_mailbox_gate_fails_before_mail_contact(self) -> None:
        completed, payload = self.run_adapter(
            "mail-list-selected",
            "--confirm-automation",
            "--account-id", "not-contacted",
            "--mailbox-id", "not-contacted",
            "--date-field", "received",
            "--start", "2026-08-31T00:00:00+08:00",
            "--end", "2026-09-07T00:00:00+08:00",
            "--limit", "10",
        )
        self.assertEqual(completed.returncode, 0)
        self.assertFalse(payload["app_contacted"])
        self.assertEqual(payload["error"]["code"], "SELECTED_MAILBOX_CONFIRMATION_REQUIRED")

    def test_content_gate_fails_before_fixture_or_application_access(self) -> None:
        completed, payload = self.run_adapter(
            "notes-get-plaintext",
            "--confirm-automation",
            "--account-id", "notes-account",
            "--folder-id", "notes-folder",
            "--note-id", "note-1",
            "--max-chars", "10",
            fixture=base_fixture(),
            action="fixture",
        )

        self.assertFalse(payload["ok"])
        self.assertFalse(payload["app_contacted"])
        self.assertEqual(payload["error"]["code"], "CONTENT_READ_CONFIRMATION_REQUIRED")
        self.assertNotIn("note-1", completed.stdout)

    def test_notes_fixture_is_half_open_top_n_and_metadata_only(self) -> None:
        notes = [
            {
                "id": "locked",
                "name": "Locked",
                "passwordProtected": True,
                "shared": False,
                "creationDate": "2026-08-31T00:00:00+08:00",
                "modificationDate": "2026-08-31T00:00:00+08:00",
                "plaintext": "LOCKED_PLAINTEXT_SECRET",
                "attachments": ["LOCKED_ATTACHMENT_SECRET"],
            },
            {
                "id": "shared",
                "name": "Shared",
                "passwordProtected": False,
                "shared": True,
                "creationDate": "2026-08-31T00:00:00+08:00",
                "modificationDate": "2026-08-31T00:00:00+08:00",
                "plaintext": "SHARED_PLAINTEXT_SECRET",
                "attachments": ["SHARED_ATTACHMENT_SECRET"],
            },
            {
                "id": "at-start",
                "name": "At start",
                "passwordProtected": False,
                "shared": False,
                "creationDate": "2026-08-31T00:00:00+08:00",
                "modificationDate": "2026-08-31T00:00:00+08:00",
                "plaintext": "UNSELECTED_PLAINTEXT_SECRET",
                "attachments": ["UNSELECTED_ATTACHMENT_SECRET"],
            },
            {
                "id": "before-end",
                "name": "Before end",
                "passwordProtected": False,
                "shared": False,
                "creationDate": "2026-09-06T23:59:59.999+08:00",
                "modificationDate": "2026-09-06T23:59:59.999+08:00",
                "plaintext": "LATEST_PLAINTEXT_SECRET",
                "attachments": ["LATEST_ATTACHMENT_SECRET"],
            },
            {
                "id": "at-end",
                "name": "At end",
                "passwordProtected": False,
                "shared": False,
                "creationDate": "2026-09-07T00:00:00+08:00",
                "modificationDate": "2026-09-07T00:00:00+08:00",
                "plaintext": "END_PLAINTEXT_SECRET",
                "attachments": ["END_ATTACHMENT_SECRET"],
            },
        ]
        completed, payload = self.run_adapter(
            "notes-list",
            "--confirm-automation",
            "--account-id", "notes-account",
            "--folder-id", "notes-folder",
            "--start", "2026-08-31T00:00:00+08:00",
            "--end", "2026-09-07T00:00:00+08:00",
            "--limit", "1",
            fixture=base_fixture(notes=notes),
            action="fixture",
        )

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["app_contacted"])
        self.assertEqual([item["note_id"] for item in payload["data"]["notes"]], ["before-end"])
        self.assertTrue(payload["data"]["truncated"])
        self.assertEqual(payload["data"]["excluded_locked_notes"], 1)
        self.assertEqual(payload["data"]["excluded_shared_notes"], 1)
        self.assertFalse(payload["data"]["attachments_inspected"])
        self.assertNotIn("PLAINTEXT_SECRET", completed.stdout)
        self.assertNotIn("ATTACHMENT_SECRET", completed.stdout)

    def test_locked_and_shared_notes_never_return_plaintext(self) -> None:
        notes = [
            {
                "id": "locked",
                "name": "Locked",
                "passwordProtected": True,
                "shared": False,
                "plaintext": "LOCKED_PRIVATE_TEXT",
            },
            {
                "id": "shared",
                "name": "Shared",
                "passwordProtected": False,
                "shared": True,
                "plaintext": "SHARED_PRIVATE_TEXT",
            },
        ]
        for note_id, expected_code in (
            ("locked", "LOCKED_NOTE_EXCLUDED"),
            ("shared", "SHARED_NOTE_EXCLUDED"),
        ):
            with self.subTest(note_id=note_id):
                completed, payload = self.run_adapter(
                    "notes-get-plaintext",
                    "--confirm-automation",
                    "--confirm-content-read",
                    "--account-id", "notes-account",
                    "--folder-id", "notes-folder",
                    "--note-id", note_id,
                    "--max-chars", "10",
                    fixture=base_fixture(notes=notes),
                    action="fixture",
                )
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["code"], expected_code)
                self.assertNotIn("PRIVATE_TEXT", completed.stdout)

    def test_selected_note_content_and_metadata_have_explicit_caps(self) -> None:
        note = {
            "id": "selected",
            "name": "T" * 700,
            "passwordProtected": False,
            "shared": False,
            "creationDate": "2026-09-01T00:00:00+08:00",
            "modificationDate": "2026-09-01T01:00:00+08:00",
            "plaintext": "abcdef",
            "attachments": ["NEVER_INSPECT_THIS_ATTACHMENT"],
        }
        completed, payload = self.run_adapter(
            "notes-get-plaintext",
            "--confirm-automation",
            "--confirm-content-read",
            "--account-id", "notes-account",
            "--folder-id", "notes-folder",
            "--note-id", "selected",
            "--max-chars", "3",
            fixture=base_fixture(notes=[note]),
            action="fixture",
        )

        result = payload["data"]["note"]
        self.assertEqual(result["plaintext"]["text"], "abc")
        self.assertTrue(result["plaintext"]["truncated"])
        self.assertEqual(result["plaintext"]["truncation_marker"], "...[truncated]")
        self.assertLessEqual(len(result["title"]), 512)
        self.assertTrue(result["metadata_truncated"])
        self.assertIn("title", result["metadata_truncated_fields"])
        self.assertTrue(result["title"].endswith("...[truncated]"))
        self.assertFalse(result["attachments_inspected"])
        self.assertNotIn("NEVER_INSPECT_THIS_ATTACHMENT", completed.stdout)

    def test_selected_plaintext_preserves_json_safe_layout_characters(self) -> None:
        content = "first\nsecond\titem\rthird"
        note = {
            "id": "selected",
            "name": "Layout",
            "passwordProtected": False,
            "shared": False,
            "creationDate": "2026-09-01T00:00:00+08:00",
            "modificationDate": "2026-09-01T01:00:00+08:00",
            "plaintext": content,
        }
        _, payload = self.run_adapter(
            "notes-get-plaintext",
            "--confirm-automation",
            "--confirm-content-read",
            "--account-id", "notes-account",
            "--folder-id", "notes-folder",
            "--note-id", "selected",
            "--max-chars", "100",
            fixture=base_fixture(notes=[note]),
            action="fixture",
        )
        plaintext = payload["data"]["note"]["plaintext"]
        self.assertEqual(plaintext["text"], content)
        self.assertFalse(plaintext["truncated"])
        self.assertFalse(plaintext["encoding_lossy"])

    def test_metadata_clip_never_splits_a_surrogate_pair(self) -> None:
        title = ("a" * 497) + "😀" + ("b" * 30)
        note = {
            "id": "selected",
            "name": title,
            "passwordProtected": False,
            "shared": False,
            "creationDate": "2026-09-01T00:00:00+08:00",
            "modificationDate": "2026-09-01T01:00:00+08:00",
            "plaintext": "body",
        }
        _, payload = self.run_adapter(
            "notes-get-plaintext",
            "--confirm-automation",
            "--confirm-content-read",
            "--account-id", "notes-account",
            "--folder-id", "notes-folder",
            "--note-id", "selected",
            "--max-chars", "100",
            fixture=base_fixture(notes=[note]),
            action="fixture",
        )
        clipped = payload["data"]["note"]["title"]
        self.assertTrue(clipped.endswith("...[truncated]"))
        self.assertFalse(
            any(0xD800 <= ord(character) <= 0xDFFF for character in clipped)
        )

    def test_mail_fixture_caps_arrays_and_never_inspects_attachments(self) -> None:
        fixture = base_fixture(
            messages=[
                {
                    "id": "inside",
                    "messageId": "internet-inside",
                    "dateSent": "2026-08-31T00:00:00+08:00",
                    "dateReceived": "2026-08-31T00:00:01+08:00",
                    "sender": "sender@example.test",
                    "subject": "Inside",
                    "messageSize": 42,
                    "content": "MAIL_BODY_SECRET",
                    "mailAttachments": ["MAIL_ATTACHMENT_SECRET"],
                },
                {
                    "id": "at-end",
                    "messageId": "internet-end",
                    "dateSent": "2026-09-07T00:00:00+08:00",
                    "dateReceived": "2026-09-07T00:00:00+08:00",
                    "sender": "sender@example.test",
                    "subject": "At end",
                    "messageSize": 1,
                    "content": "END_MAIL_BODY_SECRET",
                    "mailAttachments": ["END_MAIL_ATTACHMENT_SECRET"],
                },
            ]
        )
        fixture["Mail"]["accounts"][0]["emailAddresses"] = [
            f"address-{index}@example.test" for index in range(25)
        ]
        _, accounts_payload = self.run_adapter(
            "mail-accounts",
            "--confirm-automation",
            "--limit", "5",
            fixture=fixture,
            action="fixture",
        )
        account = accounts_payload["data"]["accounts"][0]
        self.assertEqual(len(account["email_addresses"]), 20)
        self.assertTrue(account["email_addresses_truncated"])
        self.assertIn("email_addresses", account["metadata_truncated_fields"])

        completed, list_payload = self.run_adapter(
            "mail-list-sent",
            "--confirm-automation",
            "--confirm-sent-mailbox",
            "--account-id", "mail-account",
            "--mailbox-id", "mailbox-path:Sent",
            "--start", "2026-08-31T00:00:00+08:00",
            "--end", "2026-09-07T00:00:00+08:00",
            "--limit", "10",
            fixture=fixture,
            action="fixture",
        )
        self.assertEqual([item["message_id"] for item in list_payload["data"]["messages"]], ["inside"])
        self.assertFalse(list_payload["data"]["attachments_inspected"])
        self.assertNotIn("MAIL_BODY_SECRET", completed.stdout)
        self.assertNotIn("MAIL_ATTACHMENT_SECRET", completed.stdout)

    def test_selected_mail_body_is_bounded_and_attachment_free(self) -> None:
        fixture = base_fixture(
            messages=[
                {
                    "id": "selected-message",
                    "messageId": "internet-selected",
                    "dateSent": "2026-09-01T00:00:00+08:00",
                    "dateReceived": "2026-09-01T00:00:01+08:00",
                    "sender": "sender@example.test",
                    "subject": "S" * 700,
                    "messageSize": 42,
                    "content": "abcdef",
                    "mailAttachments": ["NEVER_INSPECT_MAIL_ATTACHMENT"],
                }
            ]
        )
        completed, payload = self.run_adapter(
            "mail-get-body",
            "--confirm-automation",
            "--confirm-sent-mailbox",
            "--confirm-content-read",
            "--account-id", "mail-account",
            "--mailbox-id", "mailbox-path:Sent",
            "--message-id", "selected-message",
            "--max-chars", "3",
            fixture=fixture,
            action="fixture",
        )

        message = payload["data"]["message"]
        self.assertEqual(message["body"]["text"], "abc")
        self.assertTrue(message["body"]["truncated"])
        self.assertEqual(message["body"]["truncation_marker"], "...[truncated]")
        self.assertTrue(message["metadata_truncated"])
        self.assertIn("subject", message["metadata_truncated_fields"])
        self.assertFalse(message["attachments_inspected"])
        self.assertNotIn("NEVER_INSPECT_MAIL_ATTACHMENT", completed.stdout)

    def test_total_output_cap_returns_only_fixed_sanitized_error(self) -> None:
        completed, payload = self.run_adapter(
            action="serialize",
            response={"ok": True, "schema_version": 1, "private": "😀" * 270_000},
            command="notes-list",
            app_contacted=True,
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "notes-list")
        self.assertTrue(payload["app_contacted"])
        self.assertEqual(payload["error"]["code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assertLess(len(completed.stdout), 512)
        self.assertNotIn("😀", completed.stdout)

    def test_oversized_identifier_fails_with_fixed_error_without_echo(self) -> None:
        fixture = base_fixture()
        fixture["Notes"]["accounts"][0]["id"] = "PRIVATE_IDENTIFIER_" + "x" * 600
        completed, payload = self.run_adapter(
            "notes-accounts",
            "--confirm-automation",
            "--limit", "5",
            fixture=fixture,
            action="fixture",
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "METADATA_LIMIT_EXCEEDED")
        self.assertNotIn("PRIVATE_IDENTIFIER", completed.stdout)


class OfflineLauncherHarnessTests(unittest.TestCase):
    def run_copied_launcher(
        self,
        mode: str,
        *,
        include_jxa_script: bool = True,
        poison_perl_environment: bool = False,
        watchdog_seconds: int | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        with tempfile.TemporaryDirectory(prefix="apple-apps-reader-offline-") as temp_dir:
            root = Path(temp_dir)
            stub = root / "synthetic-child.py"
            stub.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import sys

                    mode = os.environ.get("APPLE_APPS_READER_STUB_MODE", "success")
                    if mode == "success":
                        sys.stdout.write('{"ok":true,"schema_version":1,"command":"fixture","app_contacted":false,"data":{"fixture_only":true}}\\n')
                    elif mode == "request-failure":
                        sys.stdout.write('{"ok":false,"schema_version":1,"command":"fixture","app_contacted":false,"error":{"code":"FIXTURE_REJECTION","message":"fixed","retryable":false}}\\n')
                    elif mode == "child-failure":
                        sys.stdout.write("PRIVATE_PARTIAL_STDOUT")
                        sys.stderr.write("PRIVATE_CHILD_DIAGNOSTIC")
                        raise SystemExit(9)
                    elif mode == "oversized":
                        sys.stdout.write("PRIVATE_OVERSIZED_OUTPUT" + "x" * 1_048_578)
                    elif mode == "sleep":
                        import time
                        sys.stdout.write("PRIVATE_PARTIAL_BEFORE_TIMEOUT")
                        sys.stdout.flush()
                        time.sleep(5)
                    else:
                        sys.stdout.write("PRIVATE_MALFORMED_OUTPUT")
                    """
                ),
                encoding="utf-8",
            )
            stub.chmod(0o700)

            launcher_source = LAUNCHER.read_text(encoding="utf-8")
            self.assertEqual(launcher_source.count("/usr/bin/osascript"), 1)
            launcher_source = launcher_source.replace("/usr/bin/osascript", str(stub), 1)
            self.assertNotIn("/usr/bin/osascript", launcher_source)
            if watchdog_seconds is not None:
                self.assertGreaterEqual(watchdog_seconds, 1)
                self.assertEqual(launcher_source.count("alarm 60;"), 1)
                launcher_source = launcher_source.replace(
                    "alarm 60;", f"alarm {watchdog_seconds};", 1
                )
            copied_launcher = root / "apple-apps-reader.sh"
            copied_launcher.write_text(launcher_source, encoding="utf-8")
            copied_launcher.chmod(0o700)
            if include_jxa_script:
                (root / "apple-apps-reader.js").write_text("// offline fixture only\n", encoding="utf-8")

            environment = os.environ.copy()
            environment["APPLE_APPS_READER_STUB_MODE"] = mode
            if poison_perl_environment:
                (root / "Early.pm").write_text(
                    textwrap.dedent(
                        """\
                        package Early;
                        BEGIN {
                            print '{"ok":true,"schema_version":1,"private":"' . ('x' x 1049000) . '"}';
                            exit 0;
                        }
                        1;
                        """
                    ),
                    encoding="utf-8",
                )
                environment["PERL5OPT"] = "-MEarly"
                environment["PERL5LIB"] = str(root)
                environment["PERLLIB"] = str(root)
            completed = subprocess.run(
                [str(copied_launcher), "capabilities"],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
            )

        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 1, msg=completed.stdout)
        return completed, json.loads(lines[0])

    def test_synthetic_success_and_request_failure_preserve_protocol(self) -> None:
        completed, payload = self.run_copied_launcher("success")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["fixture_only"])

        completed, payload = self.run_copied_launcher("request-failure")
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "FIXTURE_REJECTION")
        self.assertNotIn("PRIVATE", completed.stderr)

    def test_prelaunch_failure_reports_definite_no_contact(self) -> None:
        completed, payload = self.run_copied_launcher("success", include_jxa_script=False)
        self.assertEqual(completed.returncode, 70)
        self.assertFalse(payload["ok"])
        self.assertIs(payload["app_contacted"], False)
        self.assertNotIn("PRIVATE", completed.stderr)

    def test_child_failure_discards_private_streams_and_reports_unknown_contact(self) -> None:
        completed, payload = self.run_copied_launcher("child-failure")
        self.assertEqual(completed.returncode, 70)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["app_contacted"], "unknown")
        self.assertTrue(payload["app_contact_possible"])
        self.assertEqual(payload["error"]["code"], "LAUNCHER_ERROR")
        self.assertNotIn("PRIVATE", completed.stdout)
        self.assertNotIn("PRIVATE", completed.stderr)

    def test_oversized_child_output_is_replaced_with_fixed_error(self) -> None:
        completed, payload = self.run_copied_launcher("oversized")
        self.assertEqual(completed.returncode, 70)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["app_contacted"], "unknown")
        self.assertTrue(payload["app_contact_possible"])
        self.assertEqual(payload["error"]["code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assertLess(len(completed.stdout), 512)
        self.assertNotIn("PRIVATE_OVERSIZED_OUTPUT", completed.stdout)

    def test_inherited_perl_environment_cannot_bypass_bounded_helper(self) -> None:
        completed, payload = self.run_copied_launcher(
            "success", poison_perl_environment=True
        )
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["fixture_only"])
        self.assertLess(len(completed.stdout.encode("utf-8")), 512)
        self.assertNotIn("private", completed.stdout)

    def test_watchdog_terminates_child_and_sanitizes_partial_output(self) -> None:
        completed, payload = self.run_copied_launcher(
            "sleep", watchdog_seconds=1
        )
        self.assertEqual(completed.returncode, 70)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["app_contacted"], "unknown")
        self.assertTrue(payload["app_contact_possible"])
        self.assertEqual(payload["error"]["code"], "LAUNCHER_ERROR")
        self.assertNotIn("PRIVATE_PARTIAL_BEFORE_TIMEOUT", completed.stdout)
        self.assertNotIn("PRIVATE_PARTIAL_BEFORE_TIMEOUT", completed.stderr)


class StaticSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = JXA_SCRIPT.read_text(encoding="utf-8")
        cls.launcher = LAUNCHER.read_text(encoding="utf-8")

    def test_only_expected_application_proxies_exist(self) -> None:
        applications = re.findall(r'Application\("([^"]+)"\)', self.source)
        self.assertEqual(applications, ["Notes", "Mail"])

    def test_no_apple_app_mutation_or_network_refresh_calls(self) -> None:
        forbidden_calls = (
            "send",
            "synchronize",
            "checkForNewMail",
            "save",
            "delete",
            "move",
            "duplicate",
            "make",
            "activate",
        )
        for method in forbidden_calls:
            with self.subTest(method=method):
                self.assertIsNone(
                    re.search(rf"\.{method}\s*\(", self.source),
                    msg=f"forbidden Apple app method call found: {method}",
                )

        mutable_mail_properties = (
            "readStatus",
            "flaggedStatus",
            "deletedStatus",
            "junkMailStatus",
            "mailbox",
        )
        for property_name in mutable_mail_properties:
            with self.subTest(property_name=property_name):
                self.assertIsNone(
                    re.search(rf"\.{property_name}\s*=", self.source),
                    msg=f"forbidden Mail property mutation found: {property_name}",
                )

    def test_attachments_and_notes_html_are_never_read(self) -> None:
        self.assertIsNone(re.search(r"\.(?:mailAttachments|attachments)\s*\(", self.source))
        self.assertNotIn('readProperty(note, "body")', self.source)
        self.assertEqual(self.source.count('readProperty(note, "plaintext")'), 1)
        self.assertEqual(self.source.count('readProperty(message, "content")'), 2)

    def test_live_dispatch_has_mandatory_consent_gate(self) -> None:
        dispatch_start = self.source.index("function dispatch(command, options)")
        dispatch_end = self.source.index("function mapUnexpectedError", dispatch_start)
        dispatch = self.source[dispatch_start:dispatch_end]

        consent_position = dispatch.index("requireAutomationConsent(options);")
        first_live_handler_position = dispatch.index("notesAccounts(options)")
        self.assertLess(consent_position, first_live_handler_position)
        for command in (
            "notes-accounts",
            "notes-folders",
            "notes-list",
            "notes-get-plaintext",
            "mail-accounts",
            "mail-mailboxes",
            "mail-list-sent",
            "mail-get-body",
            "mail-list-selected",
            "mail-get-selected-body",
        ):
            self.assertIn(f'"{command}"', dispatch)

    def test_sent_operations_require_separate_user_selection_gate(self) -> None:
        for function_name in ("mailListSent", "mailGetBody"):
            start = self.source.index(f"function {function_name}(options)")
            end = self.source.index("\nfunction ", start + 1)
            function_source = self.source[start:end]
            self.assertIn("requireSentSelection(options);", function_source)
            self.assertLess(
                function_source.index("requireSentSelection(options);"),
                function_source.index("mailApplication()"),
            )

    def test_content_reads_require_post_metadata_confirmation(self) -> None:
        for function_name, app_call in (
            ("notesGetPlaintext", "notesApplication()"),
            ("mailGetBody", "mailApplication()"),
            ("mailGetSelectedBody", "mailApplication()"),
        ):
            start = self.source.index(f"function {function_name}(options)")
            end = self.source.index("\nfunction ", start + 1)
            function_source = self.source[start:end]
            self.assertIn("requireContentSelection(options);", function_source)
            self.assertLess(
                function_source.index("requireContentSelection(options);"),
                function_source.index(app_call),
            )

    def test_selected_mailbox_reads_require_dedicated_scope_gate(self) -> None:
        for function_name in ("mailListSelected", "mailGetSelectedBody"):
            start = self.source.index(f"function {function_name}(options)")
            end = self.source.index("\nfunction ", start + 1)
            function_source = self.source[start:end]
            self.assertIn("requireSelectedMailbox(options);", function_source)
            self.assertLess(
                function_source.index("requireSelectedMailbox(options);"),
                function_source.index("mailApplication()"),
            )

    def test_script_has_no_console_logging(self) -> None:
        self.assertNotIn("console.", self.source)
        self.assertIn("serialized = JSON.stringify(response);", self.source)
        self.assertIn("return serializeBounded(response);", self.source)

    def test_candidates_and_untrusted_metadata_are_explicitly_bounded(self) -> None:
        self.assertGreaterEqual(self.source.count("offerTopCandidate(candidates"), 3)
        self.assertNotIn("candidates.slice(0, limit)", self.source)
        self.assertIn("MAX_METADATA_CHARS", self.source)
        self.assertIn("MAX_EMAIL_ADDRESSES", self.source)
        self.assertIn("metadata_truncated_fields", self.source)
        self.assertIn("TRUNCATION_MARKER", self.source)
        self.assertNotIn("function safeString", self.source)

    def test_final_javascript_output_has_fixed_utf8_byte_cap(self) -> None:
        self.assertIn("MAX_OUTPUT_BYTES = 1024 * 1024", self.source)
        self.assertIn("utf8ByteLength(serialized) > MAX_OUTPUT_BYTES", self.source)
        self.assertIn('"OUTPUT_LIMIT_EXCEEDED"', self.source)

    def test_launcher_suppresses_raw_osascript_diagnostics(self) -> None:
        self.assertIn("/usr/bin/osascript", self.launcher)
        self.assertIn("2>/dev/null", self.launcher)
        self.assertIn('open(STDERR, ">", "/dev/null")', self.launcher)
        self.assertNotIn("2>&1", self.launcher)
        self.assertNotIn("mktemp", self.launcher)
        self.assertNotIn("ERROR_FILE", self.launcher)
        self.assertNotRegex(self.launcher, r"\b(?:cat|tee)\b")

    def test_launcher_bounds_apple_event_runtime(self) -> None:
        self.assertIn("MAX_TRANSPORT_BYTES=1048577", self.launcher)
        self.assertIn("alarm 60;", self.launcher)
        self.assertIn("length($buffer) > $limit", self.launcher)
        self.assertIn("emit_output_limit_error", self.launcher)

    def test_launcher_post_execution_failures_do_not_claim_no_contact(self) -> None:
        self.assertIn('"app_contacted":"unknown"', self.launcher)
        self.assertIn('"app_contact_possible":true', self.launcher)


if __name__ == "__main__":
    unittest.main()
