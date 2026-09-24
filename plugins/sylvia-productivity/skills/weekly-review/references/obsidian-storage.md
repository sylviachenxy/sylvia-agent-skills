# Obsidian Weekly Review 存储规范

定位 vault、创建、更新或恢复 weekly review 时读取本文件。

## Vault 定位

按以下顺序确定本机 vault：

1. 使用用户在当前请求明确给出的 vault；若与已存 config 不同，进入 `Change scope`，展示旧→新并重新确认，不能静默沿用旧值或直接写新位置；
2. 复用 weekly-review 本机 config 中仍存在且用户已批准的 vault；
3. 当前工作目录位于含 `.obsidian/` 的目录树中时，使用最近的该祖先目录；
4. 必要时读取 Obsidian 的本机 vault 配置，只接受仍存在且含 `.obsidian/` 的路径；
5. 只有一个候选时使用；多个候选时让用户选择。

确认 vault 与 `.obsidian/` 都存在，且目标位置可写。不要新建 vault，不为寻找 vault 全盘扫描，也不根据 Google Drive/iCloud 文件夹名称猜测。绝对路径仅用于本机操作，不写入周文档、Apple 对象或 skill 仓库。

## 固定目录与命名

```text
<Vault>/
└── Reviews/
    └── Weekly/
        ├── Weekly Reviews.md
        └── 2026/
            └── 2026-W36.md
```

- 文件名使用 ISO week-year 的 `YYYY-Www.md`；
- 年目录也使用 ISO week-year，不是自然日期所在年份；
- 同一 `week_id` 永远只有一份文件，不按标题、状态或重跑次数改名；
- 所有持久化链接使用 vault-relative path；
- `Weekly Reviews.md` 是可重建导航页，不是周报事实源。

## Frontmatter

以 [Weekly Review 模板](../assets/weekly-review-template.md) 为基础，至少包含：

```yaml
schema_version: 1
type: weekly-review
week_id: 2026-W36
period_start: 2026-08-31
period_end_exclusive: 2026-09-07
collected_through: 2026-09-06T20:00:00+08:00
timezone: Asia/Shanghai
status: confirmed
created_at: 2026-09-06T20:00:00+08:00
updated_at: 2026-09-06T20:00:00+08:00
tags:
  - weekly-review
```

- `status` 只允许 `draft | confirmed`；
- 日期使用 ISO `YYYY-MM-DD`，具体时刻使用带 offset 的 ISO 8601；
- `collected_through` 使用带 offset、在采集前冻结的实际证据截止时刻；week-to-date 文档仍使用完整 ISO 周的 `period_end_exclusive`，但所有 evidence 查询只到 `min(collected_through, period_end_exclusive)` 的半开上界，并在正文概览重复显示截止时刻，不能让读者误以为已覆盖未来日期；完整周使用该周 `period_end_exclusive` 对应的边界时刻；
- Goal links 放正文，不把大量 Goal IDs 或 evidence locator 塞进 frontmatter；
- 不写 absolute path、账号、邮箱、provider ID、message ID 或原始正文。

## Managed block 与用户内容

Codex 只维护以下区块：

```markdown
<!-- weekly-review:generated:start -->
...经用户确认或明确保存为 draft 的内容...
<!-- weekly-review:generated:end -->
```

区块外保留给 Sylvia 自由编辑，例如模板中的 `## Sylvia 的补充`。更新前读取完整文件；开始/结束标记缺失、重复、反序或嵌套时停止覆盖并报告冲突。不得重新生成整份文档来覆盖用户文字。

frontmatter 由 weekly-review 管理，但必须保留未知字段和用户新增 tags；只更新本规范字段。若 frontmatter 不合法、week ID 与路径冲突或 type 不匹配，停止并对账。

## 内容结构

generated block 按实际情况保留：

1. 本周概览；
2. Confirmed achievements；
3. Meaningful progress；
4. Learnings and decisions；
5. Challenges and blockers；
6. Goal 关联与轨迹观察；
7. 下周重点；
8. Evidence 摘要与 source coverage。

不要保留空标题，不粘贴文件清单、邮件/笔记原文或完整 source output。证据引用使用本周局部 ID `E001` 等，并以最小摘要说明；真实 locator 和 hash 只放私有 state。Vault 内文件可以使用 Obsidian relative link；Vault 外文件只使用配置中的逻辑 root alias 加相对路径，不写 home absolute path。

## 写入与恢复顺序

用户只要求草稿时，可以在唯一周文件内保存 `status: draft`，只 patch managed block 并回读；不更新 confirmed 索引、不建立 durable lease、不推进 baseline。

confirmed 周报必须使用 state manager transaction：

1. 读取现有 canonical report/index（不存在也要记录），保留未知 frontmatter、用户区块与索引区块外文字；
2. 在对话中展示完整的最终 report 与 index 目标文本，连同 coverage gap，让用户确认；记录每个目标的 preimage 为 `absent` 或当前 bytes 的 SHA-256；
3. 生成全局不复用的 `review_id`，以精确 target texts、对应 hashes、两个 preimages、窗口、coverage、observations 和当前 config/state revisions 调用 `review.stage`；stage 成功前不得写 confirmed 目标，preimages 从此被冻结；
4. 使用 manager 返回的 canonical relative paths、同一精确 target texts、同一 `review_id` 和 stage 后 revision 调用 `review.write-promote`；promote request 不再接受 preimage；
5. manager 先做 worst-case state 容量预检并原子持久化 `phase: writing` / request-digest WAL；随后从 `/` 逐 component 绑定、必要时创建 output/report parent，并先持久化实际 directory identities，之后才通过 Darwin `renameatx_np(..., RENAME_EXCL)` 的 destination-must-be-absent 操作创建 artifacts、安装 report/index；逐字节回读、重新 bind、验证唯一周文件和 exact confirmed 索引行后，才把 staged observations merge 到 checkpoint；
6. checkpoint 与不可变 promotion receipt 同次写入；只有返回 `promoted: true` 或同一 `review_id` / request digest 的 `already_promoted: true`，才回执 report/index verified 且 baseline committed。

不要在 `review.stage` 与 `review.write-promote` 之间重新采集 sources、重新渲染文本或换 preimage。若用户在确认后又改内容，只有 `phase: staged` 时可以先经单独确认 abort；进入 `writing` 后必须完成或对账当前恢复，再生成新预览，不能把变化后的文本塞进旧 transaction。

若中断发生在 report 或 index 写入后，路径仍由同一个 `writing` lease 占用。恢复时从 `maintenance.status` 找到 pending review，用原 `review_id`、原 target texts 与 stage 返回的 revisions 重试；manager 会核对 durable request digest，并接受精确目标或安全恢复 artifacts 后补完步骤。不要创建 `-2`、`final` 或日期后缀副本。目标、backup/staged artifact、preimage 或 directory identity 漂移时停止并展示冲突，不用旧 hash 静默推进，也不要手工删除 `.weekly-review-cas-v1-*` artifacts。

existing target 的更新是可恢复的多 syscall namespace claim，不是一个内核 hash-CAS：target 在 claim 到 install 之间可能短暂 absent；同 UID 恶意进程无法被完全序列化。manager 通过 frozen preimage、no-replace rename、held inode/hash、fsync、最终路径 rebind 和 byte-for-byte verification 避免正常编辑器并发下的静默覆盖；first cut 不防御同 UID 恶意程序在 cleanup 的 verify→unlink 窗口换入保留名称，因此不要让其他程序操纵 `.weekly-review-cas-v1-*`。系统或 volume 不支持 `renameatx_np` 时 fail closed，不能改用普通 `os.replace`。本地成功只证明 Vault 文件已验收，不证明 Google Drive 已完成云端或手机同步。

## 索引

`Reviews/Weekly/Weekly Reviews.md` 只维护：

```markdown
<!-- weekly-review:index:start -->
- [[Reviews/Weekly/2026/2026-W36|2026-W36]] · confirmed · 2026-08-31—2026-09-06
<!-- weekly-review:index:end -->
```

按 `week_id` 降序，每周恰好一行。周窗口显示 inclusive 的自然结束日，但机器边界仍以前端 `period_end_exclusive` 为准。区块外用户内容必须保留；标记损坏时停止覆盖。索引损坏不改变周文件事实，只修复索引后再推进 baseline。

## 回读验收

写入后确认：

- 路径 year/week 与 frontmatter `week_id` 一致；
- `period_start`、`period_end_exclusive`、timezone 构成同一 ISO 周；
- status 合法，confirmed 内容与用户确认预览一致；
- generated block 恰好一对且完整；
- 用户区块和未知 frontmatter 未丢失；
- 索引恰好一条指向该 vault-relative path；
- 周文档不包含绝对 home path、账号邮箱、raw Notes/Mail 正文或 provider locator；
- 只有上述检查通过后才推进 baseline。
