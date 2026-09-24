#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "git-activity.py"


def invoke(command: str, payload: dict) -> tuple[int, dict]:
    result = subprocess.run(
        ["/usr/bin/python3", str(SCRIPT), command],
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
    )
    return result.returncode, json.loads(result.stdout)


class GitActivityTests(unittest.TestCase):
    def make_repo(self, base: Path) -> Path:
        repo = base / "repo"
        repo.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "user.name", "Weekly Test"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "user.email", "weekly@example.invalid"], check=True)
        return repo

    def commit(self, repo: Path, name: str, body: str, when: str) -> None:
        (repo / name).write_text(body, encoding="utf-8")
        subprocess.run(["/usr/bin/git", "-C", str(repo), "add", "--", name], check=True)
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
        subprocess.run(["/usr/bin/git", "-C", str(repo), "commit", "-q", "-m", f"update {name}"], env=env, check=True)

    def synthetic_tree_commit(
        self, repo: Path, components: list[bytes], when: str
    ) -> str:
        """Create a commit containing a path that need not materialize on disk."""
        blob = subprocess.run(
            ["/usr/bin/git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=b"synthetic\n",
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        entry = b"100644 " + components[-1] + b"\0" + bytes.fromhex(blob.decode("ascii"))
        tree = subprocess.run(
            ["/usr/bin/git", "-C", str(repo), "hash-object", "-t", "tree", "-w", "--stdin"],
            input=entry,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        for component in reversed(components[:-1]):
            entry = b"40000 " + component + b"\0" + bytes.fromhex(tree.decode("ascii"))
            tree = subprocess.run(
                ["/usr/bin/git", "-C", str(repo), "hash-object", "-t", "tree", "-w", "--stdin"],
                input=entry,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
        commit = subprocess.run(
            ["/usr/bin/git", "-C", str(repo), "commit-tree", tree.decode("ascii"), "-m", "synthetic tree"],
            env=env,
            stdout=subprocess.PIPE,
            check=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["/usr/bin/git", "-C", str(repo), "update-ref", "HEAD", commit],
            check=True,
        )
        return commit

    def test_self_test_accesses_no_repository(self) -> None:
        code, body = invoke("self-test", {})
        self.assertEqual(code, 0)
        self.assertTrue(body["ok"])
        self.assertFalse(body["accessed_repositories"])

    def test_collect_is_bounded_and_redacts_private_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "notes.md", "first\n", "2026-09-01T10:00:00+08:00")
            self.commit(repo, "notes.md", "first\nsecond\n", "2026-09-02T10:00:00+08:00")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [
                        {
                            "alias": "course",
                            "path": str(repo),
                            "author_emails": ["weekly@example.invalid"],
                        }
                    ],
                    "patch_bytes_per_commit": 4096,
                    "include_worktree": True,
                },
            )
            self.assertEqual(code, 0, body)
            self.assertEqual(len(body["repositories"][0]["commits"]), 2)
            encoded = json.dumps(body)
            self.assertNotIn(str(repo), encoded)
            self.assertNotIn("weekly@example.invalid", encoded)
            self.assertIn("notes.md", encoded)
            self.assertIn("second", encoded)
            self.assertEqual(body["repositories"][0]["working_tree"]["period_membership"], "unknown")

    def test_author_filter_does_not_fall_back_to_all_authors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo), "author_emails": ["other@example.invalid"]}],
                },
            )
            self.assertEqual(code, 0, body)
            self.assertEqual(body["repositories"][0]["commits"], [])

    def test_author_filter_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [
                        {
                            "alias": "course",
                            "path": str(repo),
                            "author_emails": ["WEEKLY@EXAMPLE.INVALID"],
                        }
                    ],
                },
            )
            self.assertEqual(code, 0, body)
            self.assertEqual(len(body["repositories"][0]["commits"]), 1)

    def test_window_is_half_open_at_exact_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "start.md", "start", "2026-08-31T00:00:00+08:00")
            self.commit(repo, "middle.md", "middle", "2026-09-01T10:00:00+08:00")
            self.commit(repo, "end.md", "end", "2026-09-07T00:00:00+08:00")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertEqual(code, 0, body)
            commits = body["repositories"][0]["commits"]
            committed_at = {entry["committed_at"] for entry in commits}
            self.assertIn("2026-08-31T00:00:00+08:00", committed_at)
            self.assertIn("2026-09-01T10:00:00+08:00", committed_at)
            self.assertNotIn("2026-09-07T00:00:00+08:00", committed_at)

    def test_skewed_child_date_does_not_hide_in_window_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "parent.md", "parent", "2026-09-02T10:00:00+08:00")
            self.commit(repo, "child.md", "child", "2026-08-01T10:00:00+08:00")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertEqual(code, 0, body)
            commits = body["repositories"][0]["commits"]
            self.assertEqual(len(commits), 1)
            self.assertEqual(commits[0]["committed_at"], "2026-09-02T10:00:00+08:00")

    def test_non_ascii_author_filter_is_rejected_instead_of_false_complete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [
                        {
                            "alias": "course",
                            "path": str(repo),
                            "author_emails": ["Üser@example.invalid"],
                        }
                    ],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "invalid_repository")

    def test_fsmonitor_is_disabled_and_index_is_not_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            repo = self.make_repo(base)
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            sentinel = base / "fsmonitor-ran"
            hook = base / "fsmonitor.sh"
            hook.write_text(
                "#!/bin/sh\n/usr/bin/touch '" + str(sentinel) + "'\nexit 0\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "core.fsmonitor",
                    str(hook),
                ],
                check=True,
            )
            index = repo / ".git" / "index"
            before = hashlib.sha256(index.read_bytes()).hexdigest()
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                    "include_worktree": True,
                },
            )
            after = hashlib.sha256(index.read_bytes()).hexdigest()
            self.assertEqual(code, 0, body)
            self.assertFalse(sentinel.exists())
            self.assertEqual(before, after)

    def test_signature_verifier_is_never_launched(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            repo = self.make_repo(base)
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            original = subprocess.run(
                ["/usr/bin/git", "-C", str(repo), "cat-file", "commit", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            headers, message = original.split(b"\n\n", 1)
            signed = (
                headers
                + b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n fake\n -----END PGP SIGNATURE-----\n\n"
                + message
            )
            signed_oid = subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "hash-object",
                    "-t",
                    "commit",
                    "-w",
                    "--stdin",
                ],
                input=signed,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.decode("ascii").strip()
            subprocess.run(
                ["/usr/bin/git", "-C", str(repo), "update-ref", "HEAD", signed_oid],
                check=True,
            )
            sentinel = base / "signature-verifier-ran"
            verifier = base / "fake-gpg.sh"
            verifier.write_text(
                "#!/bin/sh\n/usr/bin/touch '" + str(sentinel) + "'\nexit 1\n",
                encoding="utf-8",
            )
            verifier.chmod(0o700)
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "log.showSignature",
                    "true",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "gpg.program",
                    str(verifier),
                ],
                check=True,
            )
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertEqual(code, 0, body)
            self.assertFalse(sentinel.exists())

    def test_external_excludes_file_is_never_opened(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            repo = self.make_repo(base)
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            fifo = base / "outside-excludes.fifo"
            os.mkfifo(fifo)
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "core.excludesFile",
                    str(fifo),
                ],
                check=True,
            )
            opened = threading.Event()
            stop = threading.Event()

            def offer_fifo_writer() -> None:
                while not stop.is_set():
                    try:
                        descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError as exc:
                        if exc.errno != errno.ENXIO:
                            return
                        time.sleep(0.005)
                        continue
                    opened.set()
                    try:
                        os.write(descriptor, b"\n")
                    finally:
                        os.close(descriptor)
                    return

            writer = threading.Thread(target=offer_fifo_writer, daemon=True)
            writer.start()
            try:
                code, body = invoke(
                    "collect",
                    {
                        "start_at": "2026-08-31T00:00:00+08:00",
                        "end_at": "2026-09-07T00:00:00+08:00",
                        "repositories": [{"alias": "course", "path": str(repo)}],
                        "include_worktree": True,
                    },
                )
            finally:
                stop.set()
                writer.join(timeout=1)
            self.assertEqual(code, 0, body)
            self.assertFalse(opened.is_set())

    def test_promisor_repository_is_refused_before_object_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "remote.origin.promisor",
                    "true",
                ],
                check=True,
            )
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "promisor_repository_refused")

    def test_partialclone_extension_is_refused_before_object_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "extensions.partialClone",
                    "origin",
                ],
                check=True,
            )
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "promisor_repository_refused")

    def test_symlinked_object_store_is_refused_before_object_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            repo = self.make_repo(base)
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            object_store = repo / ".git" / "objects"
            outside = base / "outside-objects"
            shutil.move(str(object_store), str(outside))
            object_store.symlink_to(outside, target_is_directory=True)
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "repository_layout_refused")

    def test_alternate_object_store_is_refused_before_object_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            repo = self.make_repo(base)
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            outside_objects = base / "outside-objects"
            outside_objects.mkdir()
            alternates = repo / ".git" / "objects" / "info" / "alternates"
            alternates.write_text(str(outside_objects) + "\n", encoding="utf-8")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "repository_alternates_refused")

    def test_shallow_repository_is_refused_as_incomplete_history(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            head = subprocess.run(
                ["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (repo / ".git" / "shallow").write_text(head + "\n", encoding="ascii")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "shallow_repository_refused")

    def test_legacy_grafts_are_refused_as_history_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            self.commit(repo, "b.md", "b", "2026-09-02T10:00:00+08:00")
            head = subprocess.run(
                ["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (repo / ".git" / "info" / "grafts").write_text(
                head + "\n", encoding="ascii"
            )
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(
                body["error"]["code"], "repository_history_override_refused"
            )

    def test_repo_local_include_cannot_hide_promisor_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            included = repo / ".git" / "promisor.inc"
            included.write_text(
                "[remote \"origin\"]\n\tpromisor = true\n"
                "[extensions]\n\tpartialClone = origin\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "include.path",
                    "promisor.inc",
                ],
                check=True,
            )
            effective = subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--get",
                    "remote.origin.promisor",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(effective.stdout.strip(), "true")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(
                body["error"]["code"], "repository_config_include_refused"
            )

    def test_external_malformed_include_is_refused_before_ordinary_git_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            repo = self.make_repo(base)
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            malformed = base / "outside-malformed.inc"
            malformed.write_text("[malformed\n", encoding="utf-8")
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "include.path",
                    str(malformed),
                ],
                check=True,
            )
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(
                body["error"]["code"], "repository_config_include_refused"
            )

    def test_repo_local_includeif_cannot_hide_promisor_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            included = repo / ".git" / "promisor.inc"
            included.write_text(
                "[remote \"origin\"]\n\tpromisor = true\n",
                encoding="utf-8",
            )
            gitdir_pattern = f"gitdir:{repo}/"
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    f"includeIf.{gitdir_pattern}.path",
                    "promisor.inc",
                ],
                check=True,
            )
            effective = subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--get",
                    "remote.origin.promisor",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(effective.stdout.strip(), "true")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(
                body["error"]["code"], "repository_config_include_refused"
            )

    def test_worktree_config_cannot_hide_promisor_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "extensions.worktreeConfig",
                    "true",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--worktree",
                    "remote.origin.promisor",
                    "true",
                ],
                check=True,
            )
            effective = subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--get",
                    "remote.origin.promisor",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(effective.stdout.strip(), "true")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "promisor_repository_refused")

    def test_patch_output_is_bounded_and_marked_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.commit(repo, "large.md", "line\n" * 5000, "2026-09-01T10:00:00+08:00")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                    "patch_bytes_per_commit": 100,
                },
            )
            self.assertEqual(code, 0, body)
            result = body["repositories"][0]
            self.assertEqual(result["status"], "partial")
            commit = result["commits"][0]
            self.assertTrue(commit["patch_truncated"])
            self.assertLessEqual(len(commit["patch_excerpt"].encode("utf-8")), 100)

    def test_long_tree_path_marks_repository_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            components = [b"a" * 200 for _ in range(22)] + [b"leaf.md"]
            self.synthetic_tree_commit(
                repo, components, "2026-09-01T10:00:00+08:00"
            )
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertEqual(code, 0, body)
            result = body["repositories"][0]
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["truncated"])
            self.assertTrue(result["commits"][0]["changes_truncated"])
            self.assertTrue(
                result["commits"][0]["changes"][0]["path_truncated"]
            )

    def test_non_utf8_tree_path_marks_repository_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw).resolve())
            self.synthetic_tree_commit(
                repo, [b"bad-\xff.md"], "2026-09-01T10:00:00+08:00"
            )
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(repo)}],
                },
            )
            self.assertEqual(code, 0, body)
            result = body["repositories"][0]
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["truncated"])
            change = result["commits"][0]["changes"][0]
            self.assertTrue(change["path_encoding_lossy"])
            self.assertTrue(result["commits"][0]["changes_truncated"])

    def test_invocation_budget_is_shared_across_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            first_base = base / "first"
            second_base = base / "second"
            first_base.mkdir()
            second_base.mkdir()
            first = self.make_repo(first_base)
            second = self.make_repo(second_base)
            large = "x" * 400_000
            self.commit(first, "large.md", large, "2026-09-01T10:00:00+08:00")
            self.commit(second, "large.md", large, "2026-09-01T10:00:00+08:00")
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [
                        {"alias": "first", "path": str(first)},
                        {"alias": "second", "path": str(second)},
                    ],
                    "max_commits": 1,
                    "patch_bytes_per_commit": 200_000,
                },
            )
            self.assertEqual(code, 0, body)
            self.assertEqual(body["status"], "partial")
            self.assertTrue(body["invocation_truncated"])
            self.assertEqual(
                body["budget_exhausted_reason"], "git_output_bytes"
            )
            self.assertLess(
                len(json.dumps(body, ensure_ascii=False).encode("utf-8")),
                4 * 1024 * 1024,
            )

    def test_subdirectory_and_symlink_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            repo = self.make_repo(base)
            child = repo / "child"
            child.mkdir()
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(child)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "repository_layout_refused")

            link = base / "repo-link"
            link.symlink_to(repo, target_is_directory=True)
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(link)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "symlink_repository_refused")

    def test_linked_worktree_gitfile_layout_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            repo = self.make_repo(base)
            self.commit(repo, "a.md", "a", "2026-09-01T10:00:00+08:00")
            linked = base / "linked"
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "worktree",
                    "add",
                    "-q",
                    "-b",
                    "linked-test",
                    str(linked),
                ],
                check=True,
            )
            code, body = invoke(
                "collect",
                {
                    "start_at": "2026-08-31T00:00:00+08:00",
                    "end_at": "2026-09-07T00:00:00+08:00",
                    "repositories": [{"alias": "course", "path": str(linked)}],
                },
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(body["error"]["code"], "repository_layout_refused")

    def test_invalid_window_fails_closed(self) -> None:
        code, body = invoke(
            "collect",
            {
                "start_at": "2026-09-07T00:00:00+08:00",
                "end_at": "2026-08-31T00:00:00+08:00",
                "repositories": [{"alias": "course", "path": "/tmp/not-used"}],
            },
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(body["error"]["code"], "invalid_time_window")


if __name__ == "__main__":
    unittest.main()
