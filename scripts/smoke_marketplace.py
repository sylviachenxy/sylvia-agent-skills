#!/usr/bin/env python3
"""Install a clean local snapshot in a temporary Codex profile; never use live data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from validate_marketplace import no_symlinks, require, validate

STANDALONE_SUITES = {
    "skills/personal-scheduler/scripts/apple-eventkit-bridge/tests": (
        "test_executor.py", "Executor state-machine tests passed. EventKit was not called."),
    "skills/weekly-review/scripts/apple-eventkit-reader/tests": (
        "test_offline.py", "Offline EventKit reader contract checks passed; no EventKit data was accessed."),
}


def run(command, cwd, env, timeout=60):
    proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    require(proc.returncode == 0, f"Command failed: {command}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def clean_snapshot(root, destination):
    # Include authored unstaged/untracked files, but never ignored development
    # data. This also works during a directory move before git add is requested.
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=root, capture_output=True, check=True)
    for relative in sorted(set(os.fsdecode(result.stdout).split("\0")) - {""}):
        source = root / relative
        require(source.resolve().is_relative_to(root), f"source outside repository: {relative}")
        require(not source.is_symlink(), f"cannot snapshot symlink: {relative}")
        if not source.is_file():  # Deleted paths still in the original index.
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def fingerprint(root):
    require(root.is_dir(), f"missing installed directory: {root}")
    no_symlinks(root)
    return {p.relative_to(root).as_posix():
            (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode & 0o111)
            for p in root.rglob("*") if p.is_file()}


def compare_package(source, installed, *, gh_metadata=False, mode_warnings=None):
    expected, actual = fingerprint(source), fingerprint(installed)
    require(expected.keys() == actual.keys(),
            f"missing/extra installed files: {expected.keys() ^ actual.keys()}")
    for relative, value in expected.items():
        if gh_metadata and relative == "SKILL.md":
            # gh legitimately injects source-tracking frontmatter, but it must
            # not change the skill body or any supporting resource.
            bodies = []
            for root in (source, installed):
                match = re.match(r"\A---\r?\n.*?\r?\n---\r?\n(.*)\Z",
                                 (root / relative).read_text(encoding="utf-8"), re.S)
                require(match, "invalid installed skill frontmatter")
                bodies.append(match[1].lstrip("\r\n"))
            require(bodies[0] == bodies[1], "gh changed the skill body")
        else:
            require(actual[relative][0] == value[0], f"installed content/mode changed: {relative}")
            if actual[relative][1] != value[1]:
                # An explicitly supplied collector records gh's known mode loss.
                # Native plugin installs must always preserve executable modes.
                require(gh_metadata and mode_warnings is not None,
                        f"installed content/mode changed: {relative}")
                mode_warnings.append(relative)
    return len(expected)


def discover_skills(codex, cwd, env):
    """Read-only app-server RPC; no thread, turn, model call or account connection."""
    with tempfile.TemporaryFile(mode="w+") as stderr:
        process = subprocess.Popen([codex, "app-server", "--stdio"], cwd=cwd, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                   text=True, bufsize=1)
        messages = queue.Queue()

        def read_lines():
            for line in process.stdout:
                messages.put(line)
            messages.put(None)

        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()

        def send(payload):
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()

        def response(identifier):
            deadline = time.monotonic() + 30
            while True:
                remaining = deadline - time.monotonic()
                require(remaining > 0, "app-server response timed out")
                try:
                    line = messages.get(timeout=remaining)
                except queue.Empty as error:
                    raise ValueError("app-server response timed out") from error
                require(line is not None, "app-server closed before its response")
                message = json.loads(line)
                if message.get("id") == identifier:
                    require("error" not in message, f"app-server error: {message.get('error')}")
                    return message["result"]

        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "sylvia_distribution_test", "version": "0.1.0"}}})
            response(1)
            send({"method": "initialized", "params": {}})
            send({"id": 2, "method": "skills/list",
                  "params": {"cwds": [str(cwd)], "forceReload": True}})
            return response(2)
        finally:
            process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdout.close()


def smoke(root, codex, gh, test_skills=False):
    validate(root)
    with tempfile.TemporaryDirectory(prefix="sylvia-distribution-") as temporary:
        scratch = Path(temporary).resolve()
        source = scratch / "clean-source"
        source.mkdir()
        clean_snapshot(root, source)
        catalog = validate(source)
        profile, cwd = scratch / "codex-profile", scratch / "unrelated-cwd"
        profile.mkdir()
        cwd.mkdir()
        env = os.environ.copy()
        # CODEX_HOME is used only as Codex's documented child-process config
        # root; the parent's environment and actual user profile are unchanged.
        env["CODEX_HOME"] = str(profile)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_AUTH_TOKEN", "CODEX_THREAD_ID"):
            env.pop(key, None)
        cli_version = run([codex, "--version"], cwd, env).strip()
        run([codex, "plugin", "--help"], cwd, env)
        run([codex, "plugin", "marketplace", "add", str(source)], cwd, env)
        installed_plugins, expected_skills = [], set()
        for plugin in catalog["plugins"]:
            result = json.loads(run([codex, "plugin", "add",
                                     f"{plugin['name']}@{catalog['marketplace']}", "--json"], cwd, env))
            installed = Path(result["installedPath"]).resolve()
            require(installed.is_relative_to(profile), "installation escaped isolated profile")
            require(result["version"] == plugin["version"], "installed version mismatch")
            count = compare_package(source / plugin["path"], installed)
            expected_skills.update(s["name"] for s in plugin["skills"])
            installed_plugins.append({"name": plugin["name"], "version": result["version"],
                                      "files": count, "root": installed})
        listing = json.loads(run([codex, "plugin", "list", "--marketplace", catalog["marketplace"],
                                  "--json"], cwd, env))["installed"]
        require({p["name"] for p in listing} == {p["name"] for p in catalog["plugins"]},
                "installed plugin set differs from catalog")
        require(all(p["enabled"] and p["installed"] for p in listing), "plugin is disabled/uninstalled")
        runtime = discover_skills(codex, cwd, env)
        loaded = []
        for entry in runtime["data"]:
            require(not entry.get("errors"), f"runtime discovery errors: {entry.get('errors')}")
            for skill in entry["skills"]:
                path = Path(skill.get("path", "/missing"))
                if any(path.is_relative_to(p["root"]) for p in installed_plugins):
                    require(skill.get("enabled") is True, f"disabled skill: {skill['name']}")
                    loaded.append(skill["name"].split(":")[-1])
        require(set(loaded) == expected_skills and len(loaded) == len(expected_skills),
                f"native skill discovery mismatch: expected {sorted(expected_skills)}, got {loaded}")
        print(f"Native install + runtime discovery: {len(installed_plugins)} plugins, {len(loaded)} skills",
              flush=True)
        gh_destination = scratch / "independent-skills"
        gh_version = run([gh, "--version"], cwd, env).splitlines()[0]
        gh_mode_warnings = []
        for plugin in catalog["plugins"]:
            for skill in plugin["skills"]:
                run([gh, "skill", "install", str(source), skill["name"], "--from-local",
                     "--dir", str(gh_destination), "--agent", "codex"], cwd, env)
                modes = []
                compare_package(source / skill["path"], gh_destination / skill["name"],
                                gh_metadata=True, mode_warnings=modes)
                if modes:
                    gh_mode_warnings.append({"skill": skill["name"], "paths": modes})
        print(f"gh name discovery + installed content: {len(expected_skills)} skills; "
              f"executable-mode warnings: {len(gh_mode_warnings)} skills", flush=True)
        test_results = []
        if test_skills:
            for plugin in installed_plugins:
                directories = sorted({p.parent for p in (plugin["root"] / "skills").rglob("test_*.py")})
                for directory in directories:
                    suite = directory.relative_to(plugin["root"]).as_posix()
                    if suite in STANDALONE_SUITES:
                        filename, marker = STANDALONE_SUITES[suite]
                        output = run([sys.executable, str(directory / filename)], cwd, env)
                        require(marker in output, f"standalone checks did not finish: {suite}")
                        test_results.append({"suite": suite, "runner": "standalone", "status": "passed"})
                        print(f"Installed standalone checks: {suite} (passed)", flush=True)
                        continue
                    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(directory)],
                                          cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
                    require(proc.returncode == 0, f"Installed tests failed: {directory}\n{proc.stdout}\n{proc.stderr}")
                    count = re.search(r"Ran (\d+) tests?", proc.stderr)
                    require(count and int(count[1]) > 0, f"no tests executed in {directory}")
                    require(not re.search(r"skipped=\d+", proc.stderr), f"tests were skipped: {directory}")
                    summary = {"suite": suite, "runner": "unittest",
                               "tests": int(count[1])}
                    test_results.append(summary)
                    print(f"Installed test suite: {summary['suite']} ({summary['tests']} passed)", flush=True)
        return {"codex": cli_version, "gh": gh_version, "source": "clean local snapshot (not GitHub)",
                "plugins": [{k: v for k, v in p.items() if k != "root"} for p in installed_plugins],
                "runtime_skills": sorted(loaded), "independent_installs": len(expected_skills),
                "gh_executable_mode_warnings": gh_mode_warnings,
                "gh_runtime_verified": False,
                "installed_test_suites": test_results, "live_user_profile_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--codex", required=True, help="Verified executable path, not a broken wrapper")
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--test-skills", action="store_true", help="Run offline test suites from installed packages")
    args = parser.parse_args()
    try:
        result = smoke(args.root.resolve(), args.codex, args.gh, args.test_skills)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Marketplace smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
