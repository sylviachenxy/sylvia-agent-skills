# Apple 执行投影规范

仅在读取、创建、更新或对账 Apple Reminders 与 Apple Calendar 投影时读取本文件。完整 Goal Contract 只保存在 Obsidian；Apple 原生对象只承载近期执行信息和最小关联 metadata。

## 架构与事实边界

固定使用以下架构：

```text
Obsidian Goal（事实源）
        ↓
本机结构化 EventKit bridge
        ↓
iCloud Reminders / iCloud Calendar（执行投影）
        ↓
Apple 负责 Mac 与 iPhone 同步
```

- EventKit 是唯一正式结构化执行层；iCloud 只是系统同步通道，不是本 skill 的远程 API。
- EventKit 保存并回读成功只证明本机 EventKit store 已提交，不能证明 iCloud 服务器或另一台设备已经同步。
- 不使用 iCloud.com UI、CloudKit、私有 iCloud endpoint、CalDAV 或 app-specific password 实现本闭环。
- AppleScript 和 Computer Use 只可用于经用户同意的一次性设置指导或可视核验，不可作为 managed projection 的默认读写、对账或重试通道。实测中 Calendar AppleScript 可能把新日历建到本地 `Default` source，Reminders AppleScript 读取也可能长期不返回。

## EventKit 能力闸门

正式投影只使用本 skill 随附的 `scripts/apple-eventkit-bridge.sh`。不要从某个 connector 或 MCP 的名称推断能力，也不要在一次 Goal 写入过程中临时拼装 Swift/AppleScript 程序或换成其他执行通道。

正式执行层至少必须能以结构化输入输出完成：

- 分别读取 Calendar 与 Reminders 的授权状态；
- 枚举 source、calendar/list、稳定容器标识及 `allowsContentModifications`；
- 在指定容器内查询、读取、创建和 patch event/reminder；
- 完成普通 reminder；
- 对 EventKit 写入结果按 ID 和 metadata 回读；
- 返回可区分的权限、校验、冲突和不确定结果；
- 检测 event/reminder 是否带 recurrence，并对 managed recurring item fail closed。

Calendar 和 Reminders 都要求 `fullAccess` 才能读取、对账和修改；Calendar `writeOnly` 不足以完成闭环。首次授权由用户在 macOS 系统提示中决定，不替用户静默修改 Privacy & Security。bridge 使用固定 bundle ID 和 `.app` 主 executable；源码交付在没有 Apple Development/Developer ID certificate 时只能 ad-hoc 签名，重建后仍可能需要重新授权。`doctor` 必须报告签名类型与 `tcc_identity_stable_across_rebuild`，不得把固定 bundle ID 夸大为永久授权。

任一必要能力、权限或结构化错误语义缺失时，不降级为 UI 点击或 AppleScript 批量写入；保持 Obsidian Goal 有效，把对应投影标为 `pending` 并说明缺失能力。

### Bundled bridge 契约

先把 `<skill-root>` 解析为 `SKILL.md` 所在目录，再通过稳定入口调用；不直接运行 `.build` 中的 binary：

```bash
bridge_path="<skill-root>/scripts/apple-eventkit-bridge.sh"
"$bridge_path" doctor
"$bridge_path" self-test
```

`doctor` 和 `self-test` 不读取 Calendar / Reminders 内容，也不请求权限；入口在必要时会先构建本地 `.app`。只有用户已经明确授权权限请求后，才能调用可能显示 macOS 系统弹窗的 `authorize`：

```bash
printf '%s\n' '{"entity":"event"}' | "$bridge_path" authorize
printf '%s\n' '{"entity":"reminder"}' | "$bridge_path" authorize
```

其余命令为 `sources list`、`containers list|create`、`availability`、`items find|get|create|patch|delete` 和 `reminders complete`。它们都从 stdin 只接收一个 JSON object，stdout 只返回一个 JSON object；未知字段会 fail closed。创建或修改前先使用 `dry_run: true` 验证结构化请求，再按用户确认的同一 diff 执行真实写入。

当前版本层次互相独立：JSON command protocol 为 `1`，Apple 原生对象 managed metadata 和 marker 为 `2` / `[goal-planner:v2]`，Obsidian 同步账本 schema 为 `3`。必须核对 `doctor` 返回的 protocol 与 managed schema；不要把这三个版本当成同一个字段。命令的必需字段以 `<skill-root>/scripts/apple-eventkit-bridge/protocol-v1.json` 为准，不根据记忆猜测 payload。任何 `ok: false`、非零退出或超时都不得被解释为成功。

## iCloud source 与专用容器

默认使用两个独立、非共享的 iCloud 容器：

- Apple Reminders list：`目标与计划`；
- Apple Calendar：`目标与计划`。

容器名称只是人类标签。正式定位必须同时使用 entity type、iCloud source 和容器标识：

- Calendar 与 Reminders 可能由两个不同的 iCloud EventKit source 提供，不假设它们共用 `sourceIdentifier`；`.calDAV` 类型本身也不能证明 source 就是 iCloud，首次必须由用户确认；
- 不使用 `defaultCalendarForNewEvents`，也不选择 `Default`、`Local`、Google 或其他账号 source；
- `Scheduled Reminders` 是 Calendar 对定时提醒事项的系统视图，不是可写目标 Calendar；
- 不向共享 calendar/list 投影个人 Goal；无法确认是否共享时先让用户选择或建立专用容器；
- 同名容器有多个时停止，不按第一个结果猜测。

第一次使用时先展示将创建或复用的 source、Calendar 和 Reminders list。只有用户确认后才建立缺失容器；创建后立即回读 source、container ID、名称和可写状态。后续把容器标识保存在每个 Goal 的同步账本中。容器标识在完整同步后也可能改变；原标识失效时按 entity type、iCloud source 和准确名称重新发现，并在唯一匹配且用户既有选择不变时更新账本，不盲目新建同名容器。

只读取这两个专用容器和与当前 Goal 相关的最小日期窗口，不扫描或记录无关个人日程、提醒事项或账号名称。

## 统一 metadata block

所有由本 skill 管理的 Reminder notes 和 Calendar event notes 都必须包含以下 machine-managed block。Codex 向 bridge 传结构化 metadata，由 bridge 严格校验字段并负责序列化或替换 block；不要让模型手拼并直接覆盖原生 notes：

```text
[goal-planner:v2]
goal_id=G-2026-001
projection_id=G-2026-001-R001
action_id=G-2026-001-A001
role=action
goal_path=Goals/G-2026-001/G-2026-001.md
obsidian_url=obsidian://open?vault=<encoded-vault>&file=<encoded-file>
[/goal-planner]
```

规则：

- `projection_id` 是幂等、去重和对账主键；EventKit identifier 只作定位缓存，标题和 Obsidian URL 都不是对象身份。
- Reminders 使用 `R001`、`R002`……；Calendar 使用 `E001`、`E002`……。分别单调递增，退役或删除后不复用。
- 不创建没有可执行价值的顶层 Goal navigation reminder，因此不保留 `R000`。
- `action_id` 只在对象对应具体行动时写入；独立 check-in 或 Goal deadline 不适用时省略。
- Reminder 的 `role` 只使用 `action | check-in`；Calendar event 只使用 `work-block | check-in | deadline`。
- `goal_path` 使用带 `.md` 的规范 vault-relative path。
- `obsidian_url` 的 vault 名和 file 参数必须 URL encode；file 使用去掉 `.md` 的相对路径。
- 同一个 Obsidian URL 同时写入 EventKit URL 字段和 managed block；即使自定义 scheme 未在某个 Apple UI 中显示，notes 仍保留可恢复入口。
- 只替换 managed block，保留区块外的用户文字。标记重复、损坏或无法安全解析时停止 patch 并标为 `conflict`。
- 不复制 Goal Contract、个人基线、研究资料、同步账本、本机绝对路径、账号标识或凭据。

Obsidian 主文档的同步账本反向记录 projection 对应的 provider、source/container 定位信息以及 EventKit identifiers。

## Reminders 映射

只投影未来 7–14 天内真正可执行的一至三个行动，以及用户确实希望在 Reminders 中看到的 check-in。每个行动先在 Obsidian 中取得稳定 `action_id`，再取得独立 `projection_id`。

标题建议：

```text
行动｜<动词 + 对象 + 可判断的完成条件>
复盘｜<需要检查的证据或决定>
```

遵守以下映射规则：

- 一个行动对应一个普通 Reminder；不依赖 tags、sections、Smart Lists、附件、assignment 或 subtasks，因为 EventKit 不完整支持这些现代 UI 特性。
- `dueDateComponents` 只在日期确有执行意义时写入；date-only 只写 Gregorian year/month/day，不能用午夜 timed reminder 代替。date-time 必须提供 RFC 3339 时刻、IANA timezone 和分钟精度，bridge 写入完整年月日时分与该时区并回读。
- 实测表明只写 due date/time 可以在 Reminders 和 Calendar 的 `Scheduled Reminders` 中显示正确时间；这不等于已经验证通知一定弹出，因此不要声称已设置通知。
- 不用 relative `EKAlarm` 模拟 `Early Reminder`。实测中 relative alarm 会改变 Apple UI 显示的提醒时间，而 `Early Reminder` 仍为 None。
- bundled bridge 当前不写入任何 Reminder alarm；它只写入经用户确认的 due date/time。需要 `Early Reminder` 时让用户在 Reminders UI 手工设置，或把提前时刻建成单独、明确命名的 Reminder，不静默改变 due time。原生对象带有手工 alarm 后，bridge 会拒绝自动 patch，直到用户决定保留原生版本或手工移除 alarm。
- priority 只在用户明确表达优先级且 bridge 能回读时写入；不把 action urgency 擅自映射为 flag。
- bundled bridge 不创建、patch、complete 或 delete recurring Reminder。Apple 只暴露系列中的首个未完成实例，完成后会推进到下一实例，旧 item ID 和完成历史不能按普通任务解释。
- 重复行动在每个 7–14 天滚动窗口内物化为独立 Reminder。每个已物化 occurrence 都必须有自己的稳定 action/projection ID，并在 Obsidian 保存不可变的 `(cadence_id, occurrence_anchor)`；下一次窗口按该组合复用已存在 slot 的 ID，只为新进入窗口的 slot 分配新 ID。改期只改变 expected schedule，不改变 anchor。若用户把 managed Reminder 手工改成 recurring，标为 `unsupported_recurrence/conflict` 并停止自动写入。

读取 check-in 状态时必须包含相关 completed reminders。普通 Reminder 的完成状态和 completion date 可以作为行动事实回写，但不能自动使 Goal `achieved`。用户手工改名、改期、移动列表、完成或删除都是待对账的新事实。

## Calendar 映射

只投影有明确日历价值且经用户确认的内容：

- `work-block`：有开始、结束和具体时间占用的执行时间块；
- `check-in`：需要在特定时间发生的复盘；
- `deadline`：用户确实希望单独显示的硬截止事件。

不要因为定时 Reminder 会显示在 Calendar 中，就再创建同内容 event。Reminder 出现在 `Scheduled Reminders` 不代表已经存在独立事件；只有真实时间占用或独立 deadline 才创建 event。

标题建议：

```text
专注｜<行动标题>
复盘｜<Goal 标题>
截止｜<Goal 标题>
```

创建或更新前明确 Calendar ID、时区、start、end、全天语义和 alarm。Timed event 使用带 offset 的 RFC 3339 `start_at/end_at` 和 IANA timezone，且 end 必须晚于 start；all-day event 使用 `start_date/end_date_exclusive`，不把 date-only 边界转成固定时区 instant。Calendar event alarm 可以按用户确认的 `minutes_before` 设置并回读。

本 bridge 不安全管理 attendees，也不保证附件、travel time、视频会议或共享设置；需要邀请他人时不把该事件纳入自动 managed 写入。managed event 后来出现 attendee 或 organizer 时，patch 和 delete 都 fail closed，避免更新邀请或发出取消。Calendar 中新增的 location-based、absolute 或其他非 display-relative alarm 也不自动改写；带坐标或 radius 的 structured location 不得因修改文本 location 而被丢弃。

这类 unsupported 冲突只有两条互斥恢复路径：

1. 用户手工移除不支持的 attendee/alarm/structured-location 状态；bridge 重新回读，冲突消失后只 patch 原 event，不创建新对象。
2. 用户选择保留原生 event 及其不支持状态；先单独预览重复可见性风险并取得确认，再把旧 projection 成功 checkpoint 为 `retired`，之后才可为同一执行目的创建带全新 ID 的 event。旧 projection 未成功退役时不得创建。

“替代”包括复制旧 event、换标题或称其为“独立时间占用”但实际服务于同一执行目的，不能靠改名绕过上述顺序。真正无关的新时间占用按普通新投影流程处理。两条路径都不自动移除 managed block、参会人或用户 alarm。

bundled bridge 当前只创建和管理单次 event，不创建、patch 或 delete recurrence。重复时间块和 check-in 在每个 7–14 天滚动窗口内物化为独立 event，并像 Reminder occurrence 一样按不可变 `(cadence_id, occurrence_anchor)` 在重叠窗口中保留原 action/projection ID；移期不改变 anchor。若 managed event 被用户改为 recurring，标为 `unsupported_recurrence/conflict`；不要把查询到的多个 occurrences 当 duplicates，也不要尝试修改 series。

Calendar 事件已经过去、被移动或被删除只是日程事实，不是行动完成证据。

## 安全 upsert 与回读

对每个投影按以下顺序执行：

1. 从 Obsidian 读取期望状态和同步账本，确认 `local: verified`，且本轮全部预期 projection 已登记为 `pending` 或可安全认领；
2. 按账本中的 entity type、iCloud source 和 container 定位唯一可写容器；
3. 有缓存的 `item_id` 时先按 ID 读取，并验证 container 和 `projection_id` marker；不得因 ID 命中而跳过 metadata 校验；
4. ID 失效或缺失时，只在目标容器内查询。Reminders 必须覆盖相关 completed 状态；Calendar 使用覆盖 expected 与 last-known 时间的最小窗口，并按 marker 和 series 关系过滤；
5. 恰好一个非 recurring 对象匹配时认领或 patch；从未验证的新 projection 没有匹配时才 create；任何 recurrence 或多个独立匹配都标为 `conflict`，不自动修改或删除；
6. 只 patch 本 skill 管理且预览过的字段，保留未知字段和 managed block 外的用户 notes；
7. 写入后立即按返回 ID 回读，验证 source/container、marker、标题、notes、URL、日期/时间/时区、completion、priority、alarm 或 recurrence 等所有本轮受管字段；
8. 计算受管字段 hash，并立即把 EventKit store epoch、identifiers、`expected_schedule`、回读后的 `last_seen_schedule`、hash、`verified_local` 和验证时间 checkpoint 到 Obsidian；cadence occurrence 的 ID 与 anchor 保持不变，再处理下一个对象。

`eventStoreIdentifier` 变化表示 EventKit store 已重建，必须使账本中的 source/container/item locator 全部失效并重新发现。`calendarItemIdentifier` 可能在完整同步后失效；`calendarItemExternalIdentifier` 也可能重复，recurring occurrences 还可能共享外部 ID。同步账本可保存二者作为 locator，但主身份始终是 `projection_id + provider + container`。发现 recurrence 时按上述规则 fail closed。

`expected_fingerprint` 只能提供 best-effort stale-object 防护，不是原子 compare-and-swap。它必须取自本轮最近一次 `items find|get` 返回的 canonical native snapshot，而不是模型自行生成或盲目复用账本中的旧值。该 fingerprint 除了 managed fields，还覆盖会影响安全写入的用户状态，与同步账本里的 `managed_hash` 不是同一概念。bridge 必须刚刚重新 fetch 对象、比较 fingerprint、立即保存并再次回读；同一 bridge 的 mutation 还要用当前用户私有的进程锁串行化。任何 stale 或锁超时都停止 mutation 并要求重新预览。

响应超时、进程被中断或返回 `operation_timeout_outcome_unknown` 时，都先重新查询 marker，不盲目重放 create。曾经 `verified_local` 的对象后来找不到时标记 `native_missing/conflict` 并让用户决定，不自动重建。原生写入成功但 Obsidian checkpoint 失败时停止后续写入；下次按 marker 重新发现并认领。

只有所有预期且非 `retired` 的对象都在目标容器中形成唯一合法投影、按 EventKit 回读一致，且同步账本没有 `pending`、`failed` 或 `conflict` 时，才能把总体状态标为 `projected`。`projected` 只表示本机投影完成，不能表述为“iCloud 已同步”或“iPhone 已收到”。

## Drift、失败与生命周期

固定使用以下对账分类：

```text
in_sync | local_pending | native_missing | native_drift | duplicate | orphan | conflict
```

- Reminder 成功而 Calendar 失败：保留已验证 Reminder，将总体状态标为 `partial`；下次只处理未验证项。
- 已验证对象后来消失：可能是用户删除、移动、完整同步换 ID 或外部异常；先按 marker 重新发现，仍无法唯一识别时标记 `conflict`，不自动重建。
- 标题、日期、时间、priority 或 notes 在 Apple 端改变：只比较 managed 字段，展示差异，让用户选择导入到 Obsidian 或恢复投影，不静默覆盖。
- 行动 Reminder 已完成但仍有未来 `work-block`：把完成事实写入 check-in，并展示保留时间块用于后续工作、接受用户手工移期，或经确认取消 event 并将投影标为 `retired` 的选项。
- 同一 projection 出现多个独立对象：保留账本中可验证的 locator，报告其他 duplicate，不自动删除；出现 series 时标为 unsupported conflict。
- Goal 文档缺失而原生对象仍存在：报告 orphan，不自动重建文档或删除原生对象。
- Goal 变为 `paused`、`achieved` 或 `abandoned` 时，先更新并回读 Obsidian，再预览未来 Reminders / Events 的保留、完成、取消、退役或删除方案；不自动处理历史对象。

以下操作必须单独展示对象和影响范围并取得确认：删除 Reminder/Event、批量完成、批量改期、移动到其他容器、合并重复项和删除 Calendar/list。bundled bridge 不修改或删除 recurring series/occurrence；删除的 Reminder 可能进入 `Recently Deleted`，不要自动清空该区域。

“整理一下”“重新规划”“清理旧任务”等模糊指令不授权删除、批量完成或覆盖用户文字。

## iCloud 同步与降级

- EventKit 目标 source 是 iCloud 且本机回读成功时，记录 `verified_local`；离线或同步延迟不回滚本地对象。
- Apple 没有提供逐项 iCloud server acknowledgement。只有用户明确要求时，才把另一台设备或 iCloud.com 的人工可见性作为额外观察证据；它不是结构化对账主键。
- 不要求用户提供 Apple Account 密码、app-specific password 或任何 iCloud token；不把凭据写入命令行、日志、Obsidian 或仓库。
- bridge 不可用、权限不足、目标 source/container 不明确或不可写时，只输出本地已确认计划和可复制清单，把投影保留为 `pending`。
- 部分写入或响应不确定时按 projection marker 对账；不自动回滚已经验证的 Obsidian Goal，也不通过 AppleScript、UI 点击或 CalDAV 盲目补写。

## 事实来源

- [EventKit](https://developer.apple.com/documentation/eventkit)
- [访问 EventKit store 与授权](https://developer.apple.com/documentation/eventkit/accessing-the-event-store)
- [创建 Calendar events 与 Reminders](https://developer.apple.com/documentation/eventkit/creating-events-and-reminders)
- [Recurring event 语义](https://developer.apple.com/documentation/eventkit/creating-a-recurring-event)
- [EventKit 本地 identifier 限制](https://developer.apple.com/documentation/eventkit/ekcalendaritem/calendaritemidentifier)
- [iCloud 数据安全与 CalDAV 边界](https://support.apple.com/102651)
