#!/usr/bin/env python3
"""Check this repository's deliberately narrow, Codex-only distribution contract.

Standard-library only. This is not a replacement for full manifest/frontmatter
validation or a real install: run the documented checks and smoke test as well.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SEMVER = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def identifier(value):
    require(isinstance(value, str) and len(value) <= 64 and NAME.fullmatch(value),
            f"invalid identifier: {value!r}")
    return value


def nonempty(value, label):
    require(isinstance(value, str) and value.strip(), f"missing/empty {label}")


def no_symlinks(path):
    # The tested native installer silently skipped directory symlinks. Even an
    # in-repository target is not safe to distribute as part of this contract.
    require(not path.is_symlink(), f"symlink is not install-safe: {path}")
    for child in path.rglob("*"):
        require(not child.is_symlink(), f"symlink is not install-safe: {child}")


def validate(root):
    root = Path(root).resolve()
    market_path = root / ".agents/plugins/marketplace.json"
    no_symlinks(root / ".agents")
    no_symlinks(root / "plugins")
    market = read_json(market_path)
    identifier(market.get("name"))
    require(isinstance(market.get("interface"), dict), "marketplace interface required")
    nonempty(market["interface"].get("displayName"), "marketplace displayName")
    entries = market.get("plugins")
    require(isinstance(entries, list) and entries, "nonempty plugins array required")
    require(not (root / "skills").exists(), "legacy root skills/ would create a second source")
    require(not (root / ".claude-plugin").exists(), "unexpected second marketplace")
    root_readme = (root / "README.md").read_text(encoding="utf-8")
    license_bytes = (root / "LICENSE").read_bytes()
    plugins, skill_names, plugin_names = [], set(), set()
    for entry in entries:
        require(isinstance(entry, dict), "plugin entry must be an object")
        name = identifier(entry.get("name"))
        require(name not in plugin_names, f"duplicate plugin: {name}")
        plugin_names.add(name)
        require("version" not in entry, f"duplicate version authority: {name}")
        require(entry.get("source") == {"source": "local", "path": f"./plugins/{name}"},
                f"source must be the contained ./plugins/{name} directory")
        require(entry.get("policy") == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                f"unexpected installation/authentication policy: {name}")
        nonempty(entry.get("category"), f"{name} category")
        plugin = root / "plugins" / name
        require(plugin.is_dir(), f"missing plugin directory: {name}")
        require(not (plugin / "plugin.json").exists() and not (plugin / ".claude-plugin").exists(),
                f"second manifest authority is not supported: {name}")
        manifest = read_json(plugin / ".codex-plugin/plugin.json")
        require(manifest.get("name") == name, f"manifest name mismatch: {name}")
        version = manifest.get("version")
        require(isinstance(version, str) and SEMVER.fullmatch(version), f"invalid semver: {name}")
        nonempty(manifest.get("description"), f"{name} description")
        require(isinstance(manifest.get("author"), dict), f"{name} author required")
        nonempty(manifest["author"].get("name"), f"{name} author.name")
        require(manifest.get("skills") == "./skills/", f"unexpected skills discovery: {name}")
        require(not {"apps", "mcpServers", "hooks"} & manifest.keys(),
                f"new external components need a separate review: {name}")
        require(not any((plugin / p).exists() for p in ("hooks", ".app.json", ".mcp.json", "mcp.json")),
                f"unexpected external components: {name}")
        require((plugin / "LICENSE").read_bytes() == license_bytes, f"license changed in {name}")
        require("license" not in manifest, f"do not introduce a new license grant: {name}")
        interface = manifest.get("interface")
        require(isinstance(interface, dict), f"missing interface: {name}")
        for field in ("displayName", "shortDescription", "longDescription", "developerName"):
            nonempty(interface.get(field), f"{name} interface.{field}")
        require(interface.get("category") == entry["category"], f"category mismatch: {name}")
        prompts = interface.get("defaultPrompt")
        require(isinstance(prompts, list) and 1 <= len(prompts) <= 3
                and all(isinstance(p, str) and 0 < len(p) <= 128 for p in prompts),
                f"invalid starter prompts: {name}")
        require(interface.get("capabilities") == [], f"unexpected tool capabilities: {name}")
        plugin_readme = (plugin / "README.md").read_text(encoding="utf-8")
        require(name in root_readme, f"missing README plugin entry: {name}")
        skills = []
        for skill in sorted((plugin / "skills").iterdir()):
            require(skill.is_dir(), f"unexpected file in skills/: {skill}")
            identifier(skill.name)
            require(skill.name not in skill_names, f"duplicate skill: {skill.name}")
            skill_names.add(skill.name)
            body = (skill / "SKILL.md").read_text(encoding="utf-8")
            header = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", body, re.S)
            require(header, f"missing frontmatter: {skill}")
            names = re.findall(r"^name:\s*([^\r\n]+)$", header[1], re.M)
            require(len(names) == 1 and names[0].strip("\"'") == skill.name,
                    f"skill frontmatter name mismatch: {skill}")
            require(re.search(r"^description:\s*\S", header[1], re.M), f"missing description: {skill}")
            require(len(body.splitlines()) < 500, f"SKILL.md too long: {skill}")
            require(skill.name in root_readme and skill.name in plugin_readme,
                    f"missing README skill entry: {skill.name}")
            ui_path = skill / "agents/openai.yaml"
            if ui_path.exists():
                require(f"${skill.name}" in ui_path.read_text(encoding="utf-8"),
                        f"missing default_prompt invocation: {skill.name}")
            skills.append({"name": skill.name, "path": skill.relative_to(root).as_posix()})
        require(skills, f"plugin contains no skills: {name}")
        for prompt in prompts:
            calls = re.findall(r"\$([a-z0-9-]+)", prompt)
            require(calls and set(calls) <= {s["name"] for s in skills},
                    f"starter prompt calls an unbundled skill: {name}")
        plugins.append({"name": name, "version": version,
                        "path": plugin.relative_to(root).as_posix(), "skills": skills})
    actual = {p.name for p in (root / "plugins").iterdir() if p.is_dir()}
    require(actual == plugin_names, f"unregistered/missing plugins: {actual ^ plugin_names}")
    return {"marketplace": market["name"], "plugins": plugins, "skill_count": len(skill_names)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.root), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as error:
        print(f"Marketplace validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
