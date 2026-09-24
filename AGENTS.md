# Repository Guidance

## Scope

Maintain this repository as a portable, multi-skill collection that follows the Agent Skills specification at <https://agentskills.io/specification>.

## Layout

- Store each skill at `plugins/<plugin-name>/skills/<skill-name>/`.
- Require `plugins/<plugin-name>/skills/<skill-name>/SKILL.md`.
- Group skills by domain: `sylvia-learning` and `sylvia-productivity`; do not create a plugin per skill.
- Before changing discovery, manifests, layout, versions, or installation, read `docs/plugin-development.md`.
- Keep one physical copy of each skill; do not use symlinks in distributed plugin packages.
- Keep the directory name identical to the `name` frontmatter value.
- Use only lowercase letters, numbers, and hyphens in skill names.
- Keep product-specific UI metadata in `agents/` and reusable resources in `scripts/`, `references/`, or `assets/`.
- Do not add a README, changelog, installation guide, or other process documentation inside an individual skill.

## Skill authoring

- Keep `SKILL.md` focused, imperative, and under 500 lines.
- Put triggering contexts and keywords in the `description` frontmatter field.
- Keep detailed material in directly linked reference files; avoid reference chains deeper than one level.
- Add scripts only for repeatable deterministic work, and test every added or changed script.
- Do not commit secrets, credentials, private URLs, personal reading records, or generated user data.
- Keep `agents/openai.yaml` synchronized with `SKILL.md`; its `default_prompt` must explicitly mention `$<skill-name>`.

## Lifecycle and versioning

- Before creating, editing, deprecating, archiving, deleting, renaming, moving, restoring, or versioning a skill/plugin, read the approved rules in `docs/maintenance-policy.md`. Policy approval does not authorize individual destructive or external operations.
- Archive/delete only with an explicit target and authorization, remove active discovery entries, and preserve a non-executable retirement record with a verified historical SHA. Do not leave archived skills in a discoverable directory or delete user installations/data.
- Plugin manifests are the sole version authority. Assess public-contract compatibility, including persistent data and authorization; apply the documented 0.x/stable rules, not a version bump per commit.
- Treat changes pushed to an already published installable main/ref as distribution. Do not reuse released versions/tags with changed contents; synchronize changelog, migration notes, consumers, and the expected discovery tests.
- Repository-only policy edits do not bump plugin versions when packages and installation contracts are unchanged. Human compatibility judgment is not replaced by a syntax check.

## Validation

After changing any skill, run both checks from the repository root:

```bash
gh skill publish --dry-run
skills-ref validate plugins/<plugin-name>/skills/<skill-name>
```

If `skills-ref` is not installed, use the official one-shot command documented by the Agent Skills project. Resolve all validation errors before committing.

For distribution changes also run `python3 scripts/validate_marketplace.py`, `python3 -m unittest discover -s tests -v`, and the isolated native install smoke documented in `docs/plugin-development.md`. Never equate a catalog entry with a complete installed plugin. Do not install into a user's live profile, commit, push, or release without authorization.
