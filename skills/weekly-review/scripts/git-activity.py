#!/usr/bin/env python3
"""Bounded, read-only Git evidence collector for weekly-review."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
from typing import Any


VERSION = "1.3.0"
MAX_INPUT_BYTES = 1024 * 1024
MAX_REPOSITORIES = 32
MAX_COMMITS = 200
MAX_PATCH_BYTES = 200_000
MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_INVOCATION_SECONDS = 30.0
MAX_SUBPROCESSES = 256
MAX_TOTAL_GIT_BYTES = 512 * 1024
MAX_TOTAL_COMMITS = 200
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REPOSITORY_CONFIG_BYTES = 1024 * 1024
MAX_REPOSITORY_METADATA_ENTRIES = 200_000
MAX_REPOSITORY_METADATA_DEPTH = 64
MAX_SUBJECT_BYTES = 1024
MAX_PATH_BYTES = 4096
MAX_CHANGES_PER_COMMIT = 1000
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RequestError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class BudgetExhausted(RequestError):
    def __init__(self, reason: str):
        super().__init__(
            "invocation_budget_exhausted",
            "The bounded Git collection budget was exhausted.",
            {"reason": reason},
        )


class InvocationBudget:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + MAX_INVOCATION_SECONDS
        self.processes = 0
        self.bytes_read = 0
        self.commits_detailed = 0

    def remaining_seconds(self) -> float:
        return self.deadline - time.monotonic()

    def start_process(self) -> None:
        if self.remaining_seconds() <= 0:
            raise BudgetExhausted("deadline")
        if self.processes >= MAX_SUBPROCESSES:
            raise BudgetExhausted("subprocess_count")
        self.processes += 1

    def consume_bytes(self, count: int) -> None:
        if count < 0 or self.bytes_read + count > MAX_TOTAL_GIT_BYTES:
            raise BudgetExhausted("git_output_bytes")
        self.bytes_read += count

    def reserve_commit_detail(self) -> None:
        if self.commits_detailed >= MAX_TOTAL_COMMITS:
            raise BudgetExhausted("commit_detail_count")
        self.commits_detailed += 1


def emit(payload: dict[str, Any], status: int = 0) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_RESPONSE_BYTES:
        payload = {
            "ok": False,
            "error": {
                "code": "output_too_large",
                "message": "The bounded Git response exceeded its output limit.",
                "details": {},
            },
        }
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        status = 2
    sys.stdout.buffer.write(encoded)
    raise SystemExit(status)


def fail(error: RequestError) -> None:
    emit(
        {
            "ok": False,
            "error": {
                "code": error.code,
                "message": error.message,
                "details": error.details,
            },
        },
        2,
    )


def read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise RequestError("input_too_large", "JSON input exceeds 1 MiB.")
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RequestError("invalid_json", "Input must be one JSON object.") from exc
    if not isinstance(value, dict):
        raise RequestError("invalid_request", "Input must be one JSON object.")
    return value


def parse_timestamp(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise RequestError("invalid_time_window", f"{field} must be an RFC 3339 timestamp.")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RequestError("invalid_time_window", f"{field} must be an RFC 3339 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RequestError("invalid_time_window", f"{field} must include an explicit offset.")
    return parsed


def int_field(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RequestError("invalid_request", f"{field} must be an integer from {minimum} to {maximum}.")
    return value


def bounded_text(value: str, maximum_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", "replace")
    if len(encoded) <= maximum_bytes:
        return value, False
    clipped = encoded[:maximum_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "", True


def decode_utf8(value: bytes) -> tuple[str, bool]:
    """Decode untrusted Git bytes and report any lossy replacement."""
    try:
        return value.decode("utf-8"), False
    except UnicodeDecodeError:
        return value.decode("utf-8", "replace"), True


def bounded_git_text(
    value: bytes, maximum_bytes: int
) -> tuple[str, bool, bool]:
    decoded, encoding_lossy = decode_utf8(value)
    bounded, truncated = bounded_text(decoded, maximum_bytes)
    return bounded, truncated, encoding_lossy


def decode_ascii_protocol(value: bytes, field: str) -> str:
    try:
        return value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RequestError(
            "git_protocol_error",
            f"Git returned non-ASCII bytes in {field}.",
        ) from exc


def path_has_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def validate_repository_path(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw:
        raise RequestError("invalid_repository", "Each repository path must be a non-empty absolute path.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise RequestError("invalid_repository", "Each repository path must be absolute.")
    lexical = Path(os.path.abspath(path))
    if path_has_symlink(lexical):
        raise RequestError("symlink_repository_refused", "Repository paths containing symlinks are refused.")
    if not lexical.is_dir():
        raise RequestError("repository_unavailable", "An approved repository is not an accessible directory.")
    resolved = lexical.resolve(strict=True)
    home = Path.home().resolve()
    if resolved == Path(resolved.anchor) or resolved == home:
        raise RequestError("repository_scope_too_broad", "Filesystem root and the entire home directory are not valid repository scopes.")
    return resolved


def safe_git_env() -> dict[str, str]:
    # Do not inherit caller-controlled GIT_* variables: alternates, object
    # directories, replace refs, config injection and lazy fetch can all turn a
    # nominal read into out-of-scope I/O or side effects.
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def run_git_process(
    repo: Path,
    args: list[str],
    budget: InvocationBudget,
    *,
    timeout: int = 20,
    maximum_bytes: int = MAX_GIT_OUTPUT_BYTES,
    truncate: bool = False,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> tuple[bytes, bool]:
    command = [
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"core.worktree={repo}",
        "-c",
        "core.bare=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "diff.external=",
        "-c",
        "core.excludesFile=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "diff.orderFile=/dev/null",
        "-c",
        "log.showSignature=false",
        "--no-pager",
        "-C",
        str(repo),
        *args,
    ]
    budget.start_process()
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=safe_git_env(),
    )
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    deadline = min(time.monotonic() + timeout, budget.deadline)
    data = bytearray()
    was_truncated = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                if budget.remaining_seconds() <= 0:
                    raise BudgetExhausted("deadline")
                raise RequestError("git_timeout", "A bounded Git read timed out.")
            events = selector.select(min(remaining, 0.25))
            if events:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                try:
                    budget.consume_bytes(len(chunk))
                except BudgetExhausted:
                    process.kill()
                    process.wait()
                    raise
                data.extend(chunk)
                if len(data) > maximum_bytes:
                    process.kill()
                    process.wait()
                    if truncate:
                        was_truncated = True
                        break
                    raise RequestError("git_output_too_large", "A bounded Git read exceeded its output limit.")
            elif process.poll() is not None:
                # Drain any final bytes after process exit.
                while True:
                    chunk = os.read(descriptor, 65_536)
                    if not chunk:
                        break
                    try:
                        budget.consume_bytes(len(chunk))
                    except BudgetExhausted:
                        process.kill()
                        process.wait()
                        raise
                    data.extend(chunk)
                    if len(data) > maximum_bytes:
                        if truncate:
                            was_truncated = True
                            break
                        raise RequestError("git_output_too_large", "A bounded Git read exceeded its output limit.")
                break
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        selector.close()
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise RequestError("git_timeout", "A bounded Git read timed out.") from exc
    if not was_truncated and process.returncode not in allowed_returncodes:
        raise RequestError(
            "git_read_failed",
            "A bounded Git read failed.",
            {"exit_code": process.returncode},
        )
    return bytes(data[:maximum_bytes]), was_truncated


def run_git(
    repo: Path,
    args: list[str],
    budget: InvocationBudget,
    timeout: int = 20,
    maximum_bytes: int = MAX_GIT_OUTPUT_BYTES,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    output, _ = run_git_process(
        repo,
        args,
        budget,
        timeout=timeout,
        maximum_bytes=maximum_bytes,
        allowed_returncodes=allowed_returncodes,
    )
    return output


def run_anchored_config(
    config_fd: int,
    args: list[str],
    budget: InvocationBudget,
    *,
    maximum_bytes: int = 64 * 1024,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    """Read one already-open repository config without repository discovery."""
    stat_result = os.fstat(config_fd)
    if not stat_result.st_size <= MAX_REPOSITORY_CONFIG_BYTES:
        raise RequestError(
            "repository_config_too_large",
            "A repository-owned config file exceeded its fixed size limit.",
        )
    os.lseek(config_fd, 0, os.SEEK_SET)
    command = [
        "/usr/bin/git",
        "--no-pager",
        "config",
        "--file",
        f"/dev/fd/{config_fd}",
        "--no-includes",
        *args,
    ]
    budget.start_process()
    timeout = min(5.0, budget.remaining_seconds())
    if timeout <= 0:
        raise BudgetExhausted("deadline")
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=safe_git_env(),
            pass_fds=(config_fd,),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if budget.remaining_seconds() <= 0:
            raise BudgetExhausted("deadline") from exc
        raise RequestError(
            "git_timeout", "A bounded repository-config read timed out."
        ) from exc
    budget.consume_bytes(len(completed.stdout))
    if len(completed.stdout) > maximum_bytes:
        raise RequestError(
            "git_output_too_large",
            "A bounded repository-config read exceeded its output limit.",
        )
    if completed.returncode not in allowed_returncodes:
        raise RequestError(
            "git_read_failed",
            "A bounded repository-config read failed.",
            {"exit_code": completed.returncode},
        )
    return completed.stdout


def open_anchored_config(git_fd: int, name: str, *, required: bool) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=git_fd)
    except FileNotFoundError:
        if not required:
            return None
        raise RequestError(
            "repository_layout_refused",
            "The selected repository has no readable anchored config.",
        )
    except OSError as exc:
        raise RequestError(
            "repository_layout_refused",
            "The selected repository config layout is not safely readable.",
        ) from exc
    stat_result = os.fstat(descriptor)
    if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_size > MAX_REPOSITORY_CONFIG_BYTES:
        os.close(descriptor)
        raise RequestError(
            "repository_layout_refused",
            "The selected repository config is not a bounded regular file.",
        )
    return descriptor


def anchored_stat(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RequestError(
            "repository_layout_refused",
            "Repository metadata could not be inspected safely.",
        ) from exc


def open_anchored_directory(parent_fd: int, name: str, *, required: bool) -> int | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not required:
            return None
        raise RequestError(
            "repository_layout_refused",
            "Required repository metadata is missing.",
        )
    except OSError as exc:
        raise RequestError(
            "repository_layout_refused",
            "Repository metadata contains an unsafe directory layout.",
        ) from exc


def validate_anchored_tree(
    root_fd: int, budget: InvocationBudget, counter: list[int], depth: int = 0
) -> None:
    if depth > MAX_REPOSITORY_METADATA_DEPTH:
        raise RequestError(
            "repository_layout_refused",
            "Repository metadata exceeded the fixed directory-depth limit.",
        )
    try:
        iterator = os.scandir(root_fd)
    except OSError as exc:
        raise RequestError(
            "repository_layout_refused",
            "Repository metadata could not be enumerated safely.",
        ) from exc
    with iterator:
        for entry in iterator:
            if budget.remaining_seconds() <= 0:
                raise BudgetExhausted("deadline")
            counter[0] += 1
            if counter[0] > MAX_REPOSITORY_METADATA_ENTRIES:
                raise RequestError(
                    "repository_layout_refused",
                    "Repository metadata exceeded the fixed entry limit.",
                )
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RequestError(
                    "repository_layout_refused",
                    "Repository metadata changed during safe inspection.",
                ) from exc
            if stat.S_ISREG(entry_stat.st_mode):
                continue
            if not stat.S_ISDIR(entry_stat.st_mode):
                raise RequestError(
                    "repository_layout_refused",
                    "Repository metadata contains a symlink or special file.",
                )
            child_fd = open_anchored_directory(root_fd, entry.name, required=True)
            assert child_fd is not None
            try:
                opened_stat = os.fstat(child_fd)
                if (opened_stat.st_dev, opened_stat.st_ino) != (
                    entry_stat.st_dev,
                    entry_stat.st_ino,
                ):
                    raise RequestError(
                        "repository_layout_refused",
                        "Repository metadata changed during safe inspection.",
                    )
                validate_anchored_tree(child_fd, budget, counter, depth + 1)
            finally:
                os.close(child_fd)


def validate_anchored_level(
    root_fd: int, budget: InvocationBudget, counter: list[int]
) -> None:
    try:
        iterator = os.scandir(root_fd)
    except OSError as exc:
        raise RequestError(
            "repository_layout_refused",
            "Repository metadata could not be enumerated safely.",
        ) from exc
    with iterator:
        for entry in iterator:
            if budget.remaining_seconds() <= 0:
                raise BudgetExhausted("deadline")
            counter[0] += 1
            if counter[0] > MAX_REPOSITORY_METADATA_ENTRIES:
                raise RequestError(
                    "repository_layout_refused",
                    "Repository metadata exceeded the fixed entry limit.",
                )
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RequestError(
                    "repository_layout_refused",
                    "Repository metadata changed during safe inspection.",
                ) from exc
            if stat.S_ISREG(entry_stat.st_mode):
                continue
            if not stat.S_ISDIR(entry_stat.st_mode):
                raise RequestError(
                    "repository_layout_refused",
                    "Repository metadata contains a symlink or special file.",
                )
            child_fd = open_anchored_directory(root_fd, entry.name, required=True)
            assert child_fd is not None
            try:
                opened_stat = os.fstat(child_fd)
                if (opened_stat.st_dev, opened_stat.st_ino) != (
                    entry_stat.st_dev,
                    entry_stat.st_ino,
                ):
                    raise RequestError(
                        "repository_layout_refused",
                        "Repository metadata changed during safe inspection.",
                    )
            finally:
                os.close(child_fd)


def anchored_path_exists(parent_fd: int, components: tuple[str, ...]) -> bool:
    current_fd = os.dup(parent_fd)
    try:
        for component in components[:-1]:
            next_fd = open_anchored_directory(current_fd, component, required=False)
            if next_fd is None:
                return False
            os.close(current_fd)
            current_fd = next_fd
        return anchored_stat(current_fd, components[-1]) is not None
    finally:
        os.close(current_fd)


def require_repo_root(repo: Path, budget: InvocationBudget) -> None:
    top_raw = run_git(repo, ["rev-parse", "--show-toplevel"], budget)
    top, top_encoding_lossy = decode_utf8(top_raw)
    if top_encoding_lossy:
        raise RequestError(
            "invalid_repository",
            "Git returned a repository root that is not valid UTF-8.",
        )
    top = top.strip()
    try:
        actual = Path(top).resolve(strict=True)
    except OSError as exc:
        raise RequestError("invalid_repository", "Git returned an unavailable repository root.") from exc
    if actual != repo:
        raise RequestError("repository_root_required", "Configure the Git worktree root, not a subdirectory.")


def refuse_promisor_repository(repo: Path, budget: InvocationBudget) -> None:
    def inspect_config(config_fd: int) -> None:
        includes = run_anchored_config(
            config_fd,
            ["--get-regexp", r"^(include\.path|includeif\..*\.path)$"],
            budget,
            allowed_returncodes=(0, 1),
        )
        if includes.strip():
            raise RequestError(
                "repository_config_include_refused",
                "Repository-owned config includes are refused because later reads could load out-of-scope or promisor settings.",
            )
        raw = run_anchored_config(
            config_fd,
            ["--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$"],
            budget,
            allowed_returncodes=(0, 1),
        )
        config_text, config_encoding_lossy = decode_utf8(raw)
        if config_encoding_lossy:
            raise RequestError(
                "repository_config_invalid_encoding",
                "Relevant repository-owned config is not valid UTF-8.",
            )
        for line in config_text.splitlines():
            key = line.partition(" ")[0]
            normalized_key = key.casefold()
            if normalized_key == "extensions.partialclone":
                raise RequestError(
                    "promisor_repository_refused",
                    "Partial/promisor repositories are refused because reads may fetch objects.",
                )
            if normalized_key.endswith(".promisor"):
                raise RequestError(
                    "promisor_repository_refused",
                    "Partial/promisor repositories are refused because reads may fetch objects.",
                )

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    repo_fd: int | None = None
    git_fd: int | None = None
    config_fd: int | None = None
    worktree_fd: int | None = None
    try:
        repo_fd = os.open(repo, directory_flags)
        git_fd = os.open(".git", directory_flags, dir_fd=repo_fd)
        config_fd = open_anchored_config(git_fd, "config", required=True)
        assert config_fd is not None
        inspect_config(config_fd)
        worktree_enabled = run_anchored_config(
            config_fd,
            ["--type=bool", "--get", "extensions.worktreeConfig"],
            budget,
            maximum_bytes=64,
            allowed_returncodes=(0, 1),
        ).strip()
        if worktree_enabled not in {b"", b"true", b"false"}:
            raise RequestError(
                "repository_config_invalid",
                "extensions.worktreeConfig must be a valid boolean.",
            )
        if worktree_enabled == b"true":
            worktree_fd = open_anchored_config(
                git_fd, "config.worktree", required=False
            )
            if worktree_fd is not None:
                inspect_config(worktree_fd)

        metadata_counter = [0]
        validate_anchored_level(git_fd, budget, metadata_counter)
        for leaf_name, required in (
            ("HEAD", True),
            ("index", False),
            ("packed-refs", False),
        ):
            leaf_stat = anchored_stat(git_fd, leaf_name)
            if leaf_stat is None:
                if required:
                    raise RequestError(
                        "repository_layout_refused",
                        "Required repository metadata is missing.",
                    )
                continue
            if not stat.S_ISREG(leaf_stat.st_mode):
                raise RequestError(
                    "repository_layout_refused",
                    "Repository control files must be regular non-symlink files.",
                )
        if anchored_stat(git_fd, "worktrees") is not None:
            raise RequestError(
                "repository_layout_refused",
                "Repositories managing linked worktrees are not supported by this first cut.",
            )
        if anchored_stat(git_fd, "shallow") is not None:
            raise RequestError(
                "shallow_repository_refused",
                "Shallow repositories are refused because their available history is incomplete.",
            )
        if anchored_path_exists(git_fd, ("info", "grafts")):
            raise RequestError(
                "repository_history_override_refused",
                "Legacy Git grafts are refused because they rewrite available history.",
            )
        objects_fd = open_anchored_directory(git_fd, "objects", required=True)
        assert objects_fd is not None
        try:
            if anchored_path_exists(objects_fd, ("info", "alternates")) or anchored_path_exists(
                objects_fd, ("info", "http-alternates")
            ):
                raise RequestError(
                    "repository_alternates_refused",
                    "Repositories with alternate object stores are refused because reads can escape the approved repository.",
                )
            validate_anchored_tree(objects_fd, budget, metadata_counter)
        finally:
            os.close(objects_fd)

        refs_fd = open_anchored_directory(git_fd, "refs", required=True)
        assert refs_fd is not None
        try:
            validate_anchored_tree(refs_fd, budget, metadata_counter)
        finally:
            os.close(refs_fd)

        info_fd = open_anchored_directory(git_fd, "info", required=False)
        if info_fd is not None:
            try:
                validate_anchored_tree(info_fd, budget, metadata_counter)
            finally:
                os.close(info_fd)
    except RequestError:
        raise
    except OSError as exc:
        raise RequestError(
            "repository_layout_refused",
            "Only a non-symlink repository with an anchored .git directory is supported.",
        ) from exc
    finally:
        for descriptor in (worktree_fd, config_fd, git_fd, repo_fd):
            if descriptor is not None:
                os.close(descriptor)

    # Only after repository config, object storage, refs, index and history
    # metadata are anchored and symlink-free may an ordinary Git command run.


def parse_log(
    raw: bytes,
    authors: set[str],
    limit: int,
    start: dt.datetime,
    end: dt.datetime,
) -> tuple[list[dict[str, Any]], bool, bool]:
    commits: list[dict[str, Any]] = []
    field_truncated = False
    for raw_record in raw.split(b"\x1e"):
        if not raw_record.strip():
            continue
        fields = raw_record.strip(b"\n").split(b"\x1f")
        if len(fields) != 6:
            raise RequestError("git_protocol_error", "Git returned a malformed commit record.")
        sha = decode_ascii_protocol(fields[0], "commit hash")
        authored_at = decode_ascii_protocol(fields[1], "authored_at")
        committed_at = decode_ascii_protocol(fields[2], "committed_at")
        author_email, author_encoding_lossy = decode_utf8(fields[3])
        subject, subject_encoding_lossy = decode_utf8(fields[4])
        parents = decode_ascii_protocol(fields[5], "parent hashes")
        field_truncated = (
            field_truncated or author_encoding_lossy or subject_encoding_lossy
        )
        if authors and author_email.casefold() not in authors:
            continue
        committed = parse_timestamp(committed_at, "git committed_at")
        if not start <= committed < end:
            continue
        bounded_subject, subject_truncated = bounded_text(subject, MAX_SUBJECT_BYTES)
        field_truncated = field_truncated or subject_truncated
        commit = {
            "commit": sha,
            "authored_at": authored_at,
            "committed_at": committed_at,
            "subject": bounded_subject,
            "subject_truncated": subject_truncated,
            "parent_count": len(parents.split()) if parents else 0,
        }
        if subject_encoding_lossy:
            commit["subject_encoding_lossy"] = True
        if author_encoding_lossy:
            commit["author_encoding_lossy"] = True
        commits.append(commit)
        if len(commits) > limit:
            return commits[:limit], True, field_truncated
    return commits, False, field_truncated


def commit_changes(
    repo: Path, sha: str, budget: InvocationBudget
) -> tuple[list[dict[str, Any]], bool]:
    raw = run_git(
        repo,
        ["diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-z", "--no-renames", sha, "--"],
        budget,
        maximum_bytes=2 * 1024 * 1024,
    )
    tokens = raw.split(b"\0")
    changes: list[dict[str, Any]] = []
    incomplete = False
    index = 0
    while index + 1 < len(tokens) and tokens[index]:
        status = decode_ascii_protocol(tokens[index], "change status")
        path, path_truncated, path_encoding_lossy = bounded_git_text(
            tokens[index + 1], MAX_PATH_BYTES
        )
        change: dict[str, Any] = {"status": status, "path": path}
        if path_truncated:
            change["path_truncated"] = True
        if path_encoding_lossy:
            change["path_encoding_lossy"] = True
        incomplete = incomplete or path_truncated or path_encoding_lossy
        changes.append(change)
        if len(changes) >= MAX_CHANGES_PER_COMMIT:
            more_changes = index + 2 < len(tokens) and bool(tokens[index + 2])
            return changes, incomplete or more_changes
        index += 2
    return changes, incomplete


def bounded_patch(
    repo: Path, sha: str, maximum: int, budget: InvocationBudget
) -> tuple[str, bool, bool]:
    if maximum == 0:
        return "", False, False
    output, truncated = run_git_process(
        repo,
        [
        "show",
        "--format=",
        "--no-show-signature",
        "--patch",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=3",
        sha,
        "--",
        ],
        budget,
        maximum_bytes=maximum,
        truncate=True,
    )
    decoded, encoding_lossy = decode_utf8(output)
    return decoded, truncated or encoding_lossy, encoding_lossy


def worktree_status(
    repo: Path, budget: InvocationBudget, limit: int = 200
) -> dict[str, Any]:
    raw = run_git(
        repo,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
            "--ignore-submodules=all",
            "--",
        ],
        budget,
    )
    entries: list[str] = []
    field_truncated = False
    encoding_lossy = False
    for item in raw.split(b"\0"):
        if not item:
            continue
        value, was_truncated, item_encoding_lossy = bounded_git_text(
            item, MAX_PATH_BYTES + 8
        )
        entries.append(value)
        field_truncated = field_truncated or was_truncated
        encoding_lossy = encoding_lossy or item_encoding_lossy
    return {
        "observed_only": True,
        "period_membership": "unknown",
        "entries": entries[:limit],
        "truncated": len(entries) > limit or field_truncated or encoding_lossy,
        "encoding_lossy": encoding_lossy,
    }


def collect(request: dict[str, Any]) -> dict[str, Any]:
    allowed = {"start_at", "end_at", "repositories", "max_commits", "patch_bytes_per_commit", "include_worktree"}
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise RequestError("unknown_fields", "Request contains unsupported fields.", {"fields": unknown})
    start = parse_timestamp(request.get("start_at"), "start_at")
    end = parse_timestamp(request.get("end_at"), "end_at")
    if start >= end:
        raise RequestError("invalid_time_window", "end_at must be later than start_at.")
    if start.microsecond or end.microsecond:
        raise RequestError(
            "invalid_time_window",
            "Git windows must use whole-second boundaries.",
        )
    repositories = request.get("repositories")
    if not isinstance(repositories, list) or not 1 <= len(repositories) <= MAX_REPOSITORIES:
        raise RequestError("invalid_request", f"repositories must contain 1 to {MAX_REPOSITORIES} entries.")
    max_commits = int_field(request.get("max_commits", 100), "max_commits", 1, MAX_COMMITS)
    patch_bytes = int_field(request.get("patch_bytes_per_commit", 0), "patch_bytes_per_commit", 0, MAX_PATCH_BYTES)
    include_worktree = request.get("include_worktree", False)
    if not isinstance(include_worktree, bool):
        raise RequestError("invalid_request", "include_worktree must be boolean.")

    parsed_repositories: list[dict[str, Any]] = []
    aliases: set[str] = set()
    for item in repositories:
        if not isinstance(item, dict):
            raise RequestError("invalid_repository", "Each repositories entry must be an object.")
        unknown_repo = sorted(set(item) - {"alias", "path", "author_emails"})
        if unknown_repo:
            raise RequestError("unknown_fields", "A repository entry contains unsupported fields.", {"fields": unknown_repo})
        alias = item.get("alias")
        if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
            raise RequestError("invalid_repository", "Repository alias must be 1-64 safe characters.")
        if alias in aliases:
            raise RequestError("duplicate_alias", "Repository aliases must be unique.")
        aliases.add(alias)
        repo = validate_repository_path(item.get("path"))
        raw_authors = item.get("author_emails", [])
        if (
            not isinstance(raw_authors, list)
            or len(raw_authors) > 16
            or any(
                not isinstance(v, str)
                or not v
                or not v.isascii()
                or len(v) > 320
                or any(character in v for character in "<>\r\n\x00")
                for v in raw_authors
            )
        ):
            raise RequestError(
                "invalid_repository",
                "author_emails must be a list of at most 16 non-empty ASCII strings.",
            )
        parsed_repositories.append(
            {
                "alias": alias,
                "authors": {value.casefold() for value in raw_authors},
                "raw_authors": list(raw_authors),
                "repo": repo,
            }
        )

    budget = InvocationBudget()
    output: list[dict[str, Any]] = []
    invocation_truncated = False
    budget_reason: str | None = None
    query_end = end - dt.timedelta(seconds=1)

    for repository_index, specification in enumerate(parsed_repositories):
        alias = specification["alias"]
        repo = specification["repo"]
        authors = specification["authors"]
        raw_authors = specification["raw_authors"]
        detailed_commits: list[dict[str, Any]] = []
        try:
            refuse_promisor_repository(repo, budget)
            require_repo_root(repo, budget)
            remaining_commit_slots = MAX_TOTAL_COMMITS - budget.commits_detailed
            if remaining_commit_slots <= 0:
                raise BudgetExhausted("commit_detail_count")
            repository_limit = min(max_commits, remaining_commit_slots)
            author_args: list[str] = []
            if raw_authors:
                author_pattern = "<(" + "|".join(
                    re.escape(value) for value in raw_authors
                ) + ")>"
                author_args = [
                    "--extended-regexp",
                    "--regexp-ignore-case",
                    f"--author={author_pattern}",
                ]
            log_raw = run_git(
                repo,
                [
                    "log",
                    "--all",
                    "--no-show-signature",
                    f"--since-as-filter={start.isoformat()}",
                    f"--until={query_end.isoformat()}",
                    f"--max-count={repository_limit + 1}",
                    *author_args,
                    "--date=iso-strict",
                    "--format=%H%x1f%aI%x1f%cI%x1f%ae%x1f%s%x1f%P%x1e",
                ],
                budget,
            )
            commits, truncated, field_truncated = parse_log(
                log_raw, authors, repository_limit, start, end
            )
            patch_budget_exhausted = False
            changes_truncated = False
            for commit in commits:
                budget.reserve_commit_detail()
                changes, commit_changes_truncated = commit_changes(
                    repo, commit["commit"], budget
                )
                commit["changes"] = changes
                commit["changes_truncated"] = commit_changes_truncated
                changes_truncated = changes_truncated or commit_changes_truncated
                if patch_bytes:
                    patch, patch_truncated, patch_encoding_lossy = bounded_patch(
                        repo, commit["commit"], patch_bytes, budget
                    )
                    commit["patch_excerpt"] = patch
                    commit["patch_truncated"] = patch_truncated
                    if patch_encoding_lossy:
                        commit["patch_encoding_lossy"] = True
                    patch_budget_exhausted = (
                        patch_budget_exhausted or patch_truncated
                    )
                detailed_commits.append(commit)
            is_partial = (
                truncated
                or patch_budget_exhausted
                or changes_truncated
                or field_truncated
            )
            result: dict[str, Any] = {
                "alias": alias,
                "status": "partial" if is_partial else "complete",
                "membership_basis": "git_committer_time",
                "author_filter": "configured" if authors else "all_authors",
                "commits": detailed_commits,
                "truncated": is_partial,
            }
            if include_worktree:
                result["working_tree"] = worktree_status(repo, budget)
                if result["working_tree"]["truncated"]:
                    result["status"] = "partial"
                    result["truncated"] = True
            output.append(result)
        except BudgetExhausted as exc:
            invocation_truncated = True
            budget_reason = str(exc.details.get("reason", "budget"))
            output.append(
                {
                    "alias": alias,
                    "status": "partial",
                    "membership_basis": "git_committer_time",
                    "author_filter": "configured" if authors else "all_authors",
                    "commits": detailed_commits,
                    "truncated": True,
                    "reason": "invocation_budget_exhausted",
                }
            )
            for remaining in parsed_repositories[repository_index + 1 :]:
                output.append(
                    {
                        "alias": remaining["alias"],
                        "status": "partial",
                        "membership_basis": "git_committer_time",
                        "author_filter": (
                            "configured"
                            if remaining["authors"]
                            else "all_authors"
                        ),
                        "commits": [],
                        "truncated": True,
                        "reason": "not_read_after_invocation_budget",
                    }
                )
            break

    overall_partial = invocation_truncated or any(
        entry.get("status") != "complete" for entry in output
    )
    return {
        "ok": True,
        "command": "collect",
        "version": VERSION,
        "status": "partial" if overall_partial else "complete",
        "invocation_truncated": invocation_truncated,
        "budget_exhausted_reason": budget_reason,
        "budget": {
            "git_output_bytes": budget.bytes_read,
            "subprocesses": budget.processes,
            "commit_details": budget.commits_detailed,
        },
        "window": {"start_at": start.isoformat(), "end_at": end.isoformat(), "semantics": "half_open"},
        "repositories": output,
        "privacy": {
            "absolute_paths_returned": False,
            "author_emails_returned": False,
            "network_access": False,
            "repository_mutation": False,
        },
    }


def self_test() -> dict[str, Any]:
    probe_start = parse_timestamp("2026-08-31T00:00:00+08:00", "start_at")
    probe_end = parse_timestamp("2026-09-07T00:00:00+08:00", "end_at")
    assert probe_start < probe_end
    assert ALIAS_RE.fullmatch("course-project")
    return {
        "ok": True,
        "command": "self-test",
        "version": VERSION,
        "git_available": Path("/usr/bin/git").is_file(),
        "accessed_repositories": False,
        "network_access": False,
    }


def main() -> None:
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"self-test", "collect"}:
            raise RequestError("usage", "Usage: git-activity.py self-test|collect")
        request = read_request()
        if sys.argv[1] == "self-test":
            if request:
                raise RequestError("invalid_request", "self-test accepts an empty JSON object.")
            emit(self_test())
        emit(collect(request))
    except RequestError as exc:
        fail(exc)
    except Exception as exc:  # fail closed without reflecting private inputs
        fail(RequestError("internal_error", "The Git collector encountered an unexpected error.", {"type": type(exc).__name__}))


if __name__ == "__main__":
    main()
