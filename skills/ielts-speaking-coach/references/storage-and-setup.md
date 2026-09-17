# 设置与持久化

## 第一次由 Codex 引导

用户要求持续训练时先解释：会在选定 Vault 保存偏好、必要原话 / 观察、复测与回执；Vault 原有同步会传播这些文字。原始音频默认不保存、不复制、不上传。用户可关闭原话存储。一次明确的持久训练设置授权适用于以后同范围归档，不每次重复询问。

1. 定位随附 `scripts/speaking_store.py`，检查 Python 3.10+；它只使用标准库。运行 `profiles`。已有唯一档案可直接继续，多人档案要选学习者，不能根据当前 Mac 的拥有者推断是 Sylvia。
2. 复用用户明确指定的 Vault；否则只查当前目录的 `.obsidian` 祖先或 Obsidian 本机 vault 配置。多个候选请用户选择，不全盘搜索、不新建 Vault、不猜 Google Drive 路径。
3. 确定稳定 profile ID（如 `sylvia`）、时区、默认 8.0 的口语目标、日常时间、反馈语言。考试日期可以未知；不把本次开发者机器的参数当作 Sylvia 已确认配置。
4. 用 `init` 保存并读回，打开返回的 `Practice.md` 检查。偏好不同于默认值时运行 `configure`。不把范例偏好、范例 session 当用户真实数据。
5. 在用户实际客户端测试语音输入、播放、轮流发言和可见的音频证据；严格模考另验独立计时。无语音则做文字降级，不虚报发音能力。
6. 完成一小段真实练习，归档；再次调用 `context`，确认能恢复条件、重点与复测。新任务中再调用一次能证明跨 context 续接。无需重新配置手机。

开发完成不等于 Sylvia 已做完以上真实 setup。

## 位置与所有权

机器 registry 默认：用户主目录下 `Library/Application Support/ielts-speaking-coach/registry.json`。它只保存 profile→Vault locator 和 archive identity；绝对路径不写入训练 Markdown、Goal 或外部任务。

```text
<Vault>/Learning/IELTS-Speaking/<profile-id>/
├── profile.json                    # 当前偏好与 revision
├── profile-history/000001.json     # 每次偏好版本，保留历史
├── Practice.md                     # 日常复习入口，可重建
└── sessions/
    ├── IS-<unique-id>.json         # 不可覆盖的原始证据 + 配置快照
    └── IS-<unique-id>.md           # 可读回执 + 用户补充
```

目录不随目标分数、考试日期或标题变化。所有共享引用为 vault-relative path。配置与 session JSON 是本 skill 的事实源，Markdown 受管区是显示副本；用户可以在“我的补充”或受管区之外自由写笔记。不会把用户补充反推成已观察到的口语表现。

此版本每个档案由一个 Mac 写入，Vault 同步负责分发；不支持两台设备同时修改档案。同步出现 conflict 副本、数据损坏或同名异内容时停止相关写入，先对账，不合并为两次训练。

## 准确命令

以下路径为占位符；Codex 将其替换为实际安装位置和用户位置并正确引用。全局参数在子命令之前。

```bash
python3 "/installed/ielts-speaking-coach/scripts/speaking_store.py" profiles
python3 "/installed/ielts-speaking-coach/scripts/speaking_store.py" --profile sylvia init --vault "/selected/Vault" --timezone Asia/Shanghai
python3 "/installed/ielts-speaking-coach/scripts/speaking_store.py" --profile sylvia show
python3 "/installed/ielts-speaking-coach/scripts/speaking_store.py" --profile sylvia context
python3 "/installed/ielts-speaking-coach/scripts/speaking_store.py" --profile sylvia configure --input "/private/preferences-patch.json" --expected-revision 1
python3 "/installed/ielts-speaking-coach/scripts/speaking_store.py" --profile sylvia archive --input "/private/session.json"
python3 "/installed/ielts-speaking-coach/scripts/speaking_store.py" --profile sylvia rebuild
```

只有一个 profile 时可省略 `--profile`，多个时不可猜。`profiles` 在未初始化时只返回空列表。`context --on YYYY-MM-DD` 查看截至某个本地日期的记录；默认今天。它返回最近六条有效回执、最初基线、最近三次严格模考、两周语音 Part 覆盖、已见题、复测队列与活动统计，不读取整个 Vault。context 本身不写展示页；开始训练时 rebuild 刷新复习台的日期和到期项目。

Codex 在私有临时目录生成 JSON 并执行命令，用户只用自然语言。执行完回读返回路径；不要让学习者自行维护 JSON。

### 改偏好与迁移

`configure` 输入是 [偏好 patch 示例](../assets/preferences.example.json) 这样的部分更新；不传字段保持不变。支持 `target_band / timezone / normal_minutes / minimum_minutes / feedback_language / interests / daily_trigger / exam_date / store_quotes / goal_id`。默认值可从 `show` 查看。时区必须为 IANA 名称，日期为 ISO；目标支持半分步长。

先 show 读取 revision，再 configure；旧 revision 被拒绝时重新读取并合并用户实际要求，不盲目重放。想恢复旧设置时读取目标历史版本的 settings，再以当前 revision configure，形成新版本；不要倒退 revision 或覆写历史。

每次练习开始冻结 profile revision。归档可引用已有历史版本，保留练习发生时的目标与时区；最新设置若关闭存原话，即使练习开始时允许，也只能保存观察摘要。关闭原话只影响新归档，不会自动删除旧记录；用户要求删除历史时单独定位确切文件与副本。

`goal_id` 仅在用户明确绑定且已核对正式 Goal 后填写，不能在 setup 猜号。改偏好不会自动更新 Goal Contract，也不会改变 Calendar/Reminders。

Vault 移动或在本机更换定位时，用 `--profile <id> rebind --vault "/new/Vault"`；目标须已有相同 archive identity，脚本只更新 locator，不复制数据。新 Mac 可用 init 绑定已同步档案，保留其中原有偏好；这不是创建第二份档案。

## Session 契约

以 [合成格式示例](../assets/session.example.json) 为结构参考；它故意使用 text 模态示范降级。替换全部示例事实，不将示例作为真实 session 导入。完整字段校验由脚本执行。

必需字段：

- `schema_version: 1`；`session_id` 为唯一、只含字母数字与短横线的 ID，例如 `IS-` 加 UUID；首次归档前生成，重试复用。
- `profile_revision` 为本次使用的已持久版本；`occurred_at` 为实际开始时间，必须带 offset；`duration_minutes` 是实际训练时长，不是预约时长。
- `mode: baseline | mock | daily | drill | recording_review | review_update`；`status: completed | partial`。
- `conditions`：是否完整测试、是否未中断、计时是否核实、timekeeper（无则 null）、实际分段 timings（未测留 `{}`）、来源说明。timings 的秒数字段为 `part1 / part2 / part3 / part2_preparation / part2_speech`。source 写原创训练、用户录音等真实来源，不假称官方。
- `attempts`：每个实际回答一条，含 id、part（1/2/3 或 micro）、kind（cold/retry/transfer/drill）、稳定 prompt_id、topic、题干、familiarity（unseen/seen/unknown）、support（none/intent/structure/model）、modality（live_audio/recording/transcript/text）、evidence 与 evidence_kind（quote/observation）。
- `criteria`：FC/LR/GRA/P 各含 status、evidence_ids、note。可选 band_estimate 为二元区间；校验只允许具有全部严格条件的全套样本，且每维引用覆盖三部分。脚本不能验证教练听音或专业判断是否正确。
- `focus_next` 最多两项，`limitations` 写有意义的限制；无额外限制可为空，但不能省略字段。
- `review_items`：review_id、criterion、task、due_date；`review_results`：review_id、outcome、reason，实际复测另含 attempt_id；没有则空列表。

可选：self_reflection、repairs（attempt_id/original/alternative/reason）、已经存在的 goal_id/action_id、supersedes。一个 action_id 必须属于同条 goal_id。

attempt 可含 `audio_ref`，必须指向 Vault 内已存在的用户文件，可带 `#t=秒数` 等定位；不填私有外部 URL 或机器绝对路径。没有长期音频时保存现场观察并说明无法回放即可，不要求为归档而录音。`store_quotes:false` 时 evidence_kind 只能 observation，不保存 repairs 原句。

`review_update` 只用于用户明确停止排队等纯队列修改：零时长、无 attempts、四维未观察，不能算一次练习。普通进展查询只读 context，不新建此类记录。

## 恢复与更正

归档先写不可变 JSON，再生成 Markdown；成功必须读到 `views_verified:true`。如果 JSON 已存而展示页更新失败，返回 `archived:true, views_verified:false`；修复受管标记冲突后使用同一档案 rebuild，不产生新 session。

同 ID、同内容归档幂等；同 ID、不同内容拒绝。事实更正使用新的 session_id 加 `supersedes:<原 active ID>`，完整表达更正后的本次事实；旧记录保留并标为已替代，统计只计有效版本。更正保留仍被后续复测引用的 review_id；破坏这些关系会被拒绝。手写纠错内容放用户区不能改变受管证据。

脚本用本机文件锁、原子写和回读防止同机重复 / 部分写；这些不保证 Google Drive 已同步到另一设备。受管标记重复、损坏或缺失时不覆盖用户文件。迁移身份或同步冲突无法确认时停止档案写入，但可以继续不落盘练习。
