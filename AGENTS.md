# Repository Guidance

## Scope

Maintain this repository as a portable, multi-skill collection that follows the Agent Skills specification at <https://agentskills.io/specification>.

## Layout

- Store each skill at `skills/<skill-name>/`.
- Require `skills/<skill-name>/SKILL.md`.
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

## Validation

After changing any skill, run both checks from the repository root:

```bash
gh skill publish --dry-run
skills-ref validate skills/<skill-name>
```

If `skills-ref` is not installed, use the official one-shot command documented by the Agent Skills project. Resolve all validation errors before committing.
