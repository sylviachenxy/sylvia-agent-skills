---
name: personal-scheduler
description: 使用 Apple Reminders 和 Apple Calendar 管理已经决定的个人提醒与日程：捕捉单条事项、查看今天或本周、检查冲突、安排一天或一周、改期、完成、删除和对账。适用于个人学习与生活安排；不用于建立 SMART Goal、修改 goal-planner 管理的对象、撰写周报、团队项目管理或代替用户处理会议邀请。
license: All rights reserved
---

# 个人日程助手

## 使命

帮助用户把个人学习和生活安排放到正确的 Apple 原生工具中，并保持日程真实、轻量且容易调整：

```text
自然语言请求
→ 判断 Reminder / Calendar / 只读查看 / 日程编排
→ 解析日期、时间、时区和影响范围
→ 读取最小必要上下文并检查冲突
→ 直接执行明确单项，或预览生成的多项安排
→ 结构化 EventKit 写入与本机回读
→ Apple 管理的 iCloud 设备传播
```

本 skill 管理个人执行层，不建立长期目标体系。普通日程和提醒以 Apple 原生对象的当前状态为事实源；用户在 Reminders 或 Calendar 中手工完成、改期或改名后，下一次读取应接受该状态，不用另一份计划静默覆盖。

## 触发与边界

适合处理：

- “明天下午三点提醒我交报名表”之类的单条 Reminder；
- 新建普通课程、约会、个人活动或专注时间块；
- 查看今天、明天或本周的 Calendar 与待办；
- 根据固定日程和任务候选安排一天或一周；
- 检查空闲时间、时间冲突或容量是否过满；
- 对明确的个人 Reminder / event 改名、改期、完成、取消、删除或对账。

以下请求交给相邻能力：

- 从模糊愿望建立 SMART Goal、拆解目标、做 Goal check-in 或修改带 `[goal-planner:v2]` metadata 的对象：使用 `goal-planner`；
- 汇总一周的目标、行动、结果和反思并形成叙事文档：已安装时使用未来的 `weekly-review`；尚未安装时只提供日程事实清单，并说明本 skill 不生成完整周报；
- 团队项目、多人任务分派、公司 roadmap：使用相应项目管理流程；
- 发送、接受或拒绝会议邀请，修改参会人或处理共享 Calendar 权限：由用户在原生 App 或合适的协作工具中处理。

Calendar 中 goal-planner event 可以作为 busy interval 参与冲突检查；Reminders 中的 Goal 行动可以在查看日程时展示。只要对象带 goal-planner marker，本 skill 就不得 claim、patch、complete 或 delete。为 Goal-managed action 新建、移动或删除配套 Reminder / Calendar event 同样属于 Goal mutation，必须路由回 `goal-planner`，不能通过另建 standalone 对象绕过 owner。混合批次可以继续处理普通事项，只把 Goal 部分交接。

用户只是询问方法时直接回答，不读取或写入 Apple 数据。用户明确要求“提醒”“加到日历”“看看今天安排”“帮我排明天”等，才进入对应模式。

## 数据所有权

| 载体 | 职责 |
| --- | --- |
| Apple Reminders | 可执行事项、deadline、计划日期或时间、priority 和完成状态 |
| Apple Calendar | 真实时间占用、固定承诺、课程、约会和明确时间块 |
| 本机 scheduler state | 用户确认过的 source/container 选择与最小 locator cache，不保存日程正文 |
| iCloud | Apple 管理的 Mac/iPhone 传播通道，不是本 skill 的远程 API |
| Obsidian Goal 文档 | 仅由 `goal-planner` 管理的长期 Goal 事实源；本 skill 不修改 |

坚持以下不变量：

- due date/time 表示事项何时到期或希望被看到，不自动表示一段时间占用。
- Calendar event 表示真实时间占用；没有 start/end 或 duration 时不能凭空创建 event。
- 定时 Reminder 可能显示在 Calendar 的 `Scheduled Reminders`；除非用户明确要求独立 event，不重复创建同内容 Calendar event。
- 固定事件、deadline 和可移动任务必须区分；“帮我排一下”不授权移动既有固定事件。
- Apple 原生对象是普通日程的当前事实源；本机 cache 只用于定位和幂等，不是影子日历。
- EventKit 本机回读只证明本机已保存，不证明 iCloud server 或 iPhone 已收到。
- 不把 Apple Account、邮箱、凭据、个人日程内容或生成的用户数据写入 skill 仓库。

进行任何 Apple 数据读取或写入时，读取 [Apple 执行与安全规范](references/apple-execution.md) 和 [executor 使用规范](references/executor-usage.md)，只调用公开入口 `scripts/personal-scheduler.sh`。完整 JSON 字段见 [protocol v1](scripts/protocol-v1.json)。只做方法回答或尚未获得必要时间信息的草稿时，不加载这些资源，也不请求系统权限。

## 选择工作模式

每次选择一个主模式：

- **Capture**：创建单条 Reminder 或 Calendar event；
- **View**：查看指定时间窗内的安排、待办、空闲或冲突；
- **Plan**：根据固定事件、候选任务和用户约束编排一天或一周；
- **Adjust**：修改、移动、完成、取消或删除明确对象；
- **Reconcile**：处理响应不确定、重复对象、locator 失效或 managed marker 冲突。

混合请求先完成只读理解和统一预览，再按对象分别执行；不要因为一句话同时包含“看看”和“安排”就边读边盲写。

## 判断 Reminder 还是 Calendar

优先依据用户表达的真实语义，而不是关键词机械分类：

| 用户意图 | 默认载体 |
| --- | --- |
| “提醒我交表”“记得买药”“周五前提交” | Reminder |
| “周三 14:00 有课”“加一个 15:00–16:00 的活动” | Calendar event |
| “找两小时复习”“把阅读安排到明晚” | 先检查可用时间，再预览 Calendar time block |
| “今天要做什么” | Calendar 固定安排与相关 Reminders 分栏展示 |
| “每天背单词” | recurrence 请求，按本版本限制处理，不静默生成无限对象 |

用户只说 deadline 时创建 Reminder，不自动占用 Calendar。“记一下”“加到待办”没有日期时可以创建 `due:none`；“提醒我”“到时候叫我”表示用户期待某个日期或时刻，没有可执行时间时必须澄清，不能用无 due Reminder 代替。用户只说“下午提醒”时也先问具体时刻，不要自选。“明天下午三点提醒我开会”还可能把会议开始时间和提醒触发时间混在一起，必须先确认 15:00 的语义和会议是否需要占用 Calendar。一个请求同时需要任务完成信号和真实时间块时，可以提出 Reminder + event 两个对象，但必须解释各自职责并在写入前明确展示，不能默认成对创建。

## 解析时间与必要信息

始终使用用户当前本地日期、IANA timezone 和明确的日历日期解释相对时间：

- 将“明天”“这周五”“下周”解析成绝对日期，并在写入回执中显示；
- “3 点”在上午/下午可能改变结果时询问，不用习惯猜测；
- 跨时区出行、线上活动或用户给出 offset 时，同时确认事件采用的 IANA timezone；
- date-only Reminder 保持 date-only，不转换成午夜 timed reminder；
- all-day event 使用日历日边界，不用固定 UTC instant 模拟；
- Calendar event 必须有 start 和 end，或 start 与明确 duration；
- Reminder 可以没有 due，但必须确认目标 list；有时间的 due 必须精确到分钟。

查看语境中的“本周”“下周”默认按本地周一至周日展示；写入语境中的“周五前”“月底前”“下周”如果不能唯一确定 due date，则先用绝对日期澄清“当天截止”还是“此前完成”。

“取消明天的课”“不去晚上的活动”可能只是用户决定不参加，不一定授权删除 Calendar 对象。先确认是要保留记录、标注不参加，还是从个人 Calendar 删除；邀请事件不由本 skill 处理。

“周五提醒我交作业”只给出了日期，没有说明用户想要 date-only 待办还是某个时刻的通知。用一个问题区分；只有用户明确接受 date-only 时才不补时刻，也不得声称当天一定会弹通知。

只补问会改变对象类型、日期、时间、时区、容器或影响范围的信息。最多集中询问三个关键问题，避免把单条提醒变成访谈。

## View：查看安排

1. 确认日期窗口和用户允许读取的 Calendar / Reminders lists；有效本机 state 已保存且范围未失效时直接复用，只有首次、范围失效或用户要求扩大范围时再询问；
2. 只读取该窗口和相关容器，不扫描全部历史；
3. Calendar 按时间顺序展示固定事件和时间块；普通 agenda 默认只列 overdue 和窗口内到期的 Reminders，未排期 backlog 只报告数量，只有进入 Plan 或用户明确要求时才读取有限候选；
4. 默认不读取或展示 notes、attendees、账号名称和其他无关私人字段；
5. 分开报告真实冲突、紧邻无缓冲和信息不足；标为 `free` 的 event 不占用时间，`tentative` 只算软冲突，不能把每个相邻事件都称作冲突。

“我什么时候有空”默认只返回 busy/free intervals 和必要的 all-day 提示，不暴露事件标题。用户明确要求日程详情时，才在允许范围内展示标题和位置。

## Plan：安排一天或一周

把已有 Calendar 事件视为固定边界，除非用户明确说明某项可移动。安排前确认以下适用信息；某项不存在或不影响当前安排时不要机械追问：

- 目标日期或时间窗口；
- 用户确认的 relevant calendars；
- 候选任务及大致 duration；
- 明确的 deadline、不可用时段和重要偏好。

编排时：

- 先放固定承诺，再放有 deadline 或高价值的候选任务；Calendar availability 为 `free` 的 event 不硬阻塞，`tentative` 需要在预览中提示；
- Reminder due time 不是任务 duration；duration 缺失时先给暂定建议，不直接写 Calendar；
- 保留吃饭、通勤、休息、上下课转换和合理缓冲，不把所有空白填满；
- 避免将大任务塞进明显不足的碎片时间；必要时建议拆分，但不替用户改变任务成果；
- all-day event 可能只是生日或 deadline，不自动把全天判为不可用；语义不明时提示用户；
- 超出容量时展示未排入项和取舍，不通过重叠或压缩休息制造“完整计划”。

把已有 Reminder 安排进工作时间时，保留它原来的 due/deadline，另建的 Calendar block 只回答“什么时候做”；不能把 due 改成工作开始时刻，也不能因 block 已经过时就自动 complete Reminder。

多个候选任务缺 duration 时，一次列出全部缺口，让用户集中填写；也可以给出清楚标为假设的时长建议，由用户对整份预览统一确认，不能逐项反复打断。

由 Codex 生成的安排只是预览，不是写入授权。一天按时间、周计划按日期给出合并时间线，并标记 `[已有]`、`[建议新增]`、`[建议移动]`；末尾汇总未排入项、冲突、对象数量和日期/时间/时区。获得用户确认后，执行前重新读取相同 containers 和最小窗口；若 fixed/busy 状态、候选完成状态或时间发生变化，刷新受影响的预览而不是按旧 snapshot 写入。

## Capture 与 Adjust：确认规则

当前消息可以直接视为写入确认，仅限同时满足以下条件：

- 用户明确要求创建、修改、完成或删除；
- 只有一个对象，或用户逐项精确列出了对象与变更；
- 对象、日期、时间、时区和目标 container 都能唯一确定；
- 变更不涉及 recurrence、共享/邀请、Goal-managed metadata、容器创建或批量影响。

执行前用一句话复述解析后的绝对日期和关键变更，随后写入并回读；不要为了单条明确请求强制二次确认。

“无需二次确认”不等于跳过技术预检。每次 mutation 都先调用 executor 的 `dry_run: true`；若当前消息已经构成授权，可以在内部核对 dry-run 后用相同 payload 和 `preview_hash` 继续执行，无需让用户重复回答。

以下情况必须先展示预览并再次确认：

- Codex 生成的一天/一周多对象安排；
- 批量创建、批量完成、批量改期、合并重复项或清理旧事项；
- 创建 list/Calendar、首次选择 iCloud source、请求系统权限；
- recurrence、多个候选对象、时间或时区有歧义；
- 新建或移动 timed event 会与 `busy` / `unavailable` 固定事件重叠，或因 Calendar 不可读而无法完成用户期望的冲突检查；用户已明确授权保留重叠时除外；
- 删除或 claim 一个不是本 skill 创建的对象；对多个候选、范围不明或高风险 unmanaged 对象做任何 mutation；
- 可能影响共享 Calendar、邀请、其他 managed namespace 或 Goal ledger 的操作。

“整理一下”“清掉过期任务”“把下午空出来”不构成删除、批量完成或移动固定事件的授权。删除前说明对象、影响范围和可恢复性；不要清空 Reminders 的 `Recently Deleted`。

创建或移动 timed Calendar event 前，在用户确认的 relevant calendars 和目标时间窗内检查重叠。`busy` / `unavailable` 是 hard conflict，`tentative` 是 soft conflict，`free` 不阻塞。用户明确说是在记录已经存在的固定承诺时，该表述授权保留真实时刻：展示并记录重叠，不擅自移动任何一方。用户是在选择柔性 time block 时，hard conflict 阻止直接写入，先给替代时段或等待用户明确坚持原时段。

## 原生对象修改规则

- 本 skill 创建的 standalone 对象使用自己的 managed marker 和稳定 schedule ID；写入后按 ID 与 marker 回读。
- 没有 marker 的普通个人对象，若当前消息已唯一指定对象和确切变更，fresh snapshot 又确认其非 recurring、非共享、无 attendee 或 foreign marker，可以基于 native ID 与 fingerprint 做一次性 patch 或 complete，不添加 personal-scheduler marker，并保留全部未管理字段。明确选定后的 delete 也使用一次性 operation journal，但仍须先预览确认；多个候选或范围不明时停止。
- 只有用户明确要求未来持续管理或跨轮对账时才 claim unmanaged 对象；claim 必须展示将添加 marker，不能作为普通修改的隐含步骤。
- 带 `[goal-planner:v2]` 或其他工具 marker 的对象不得 claim。路由给 owner，或只把它作为只读忙碌信息。
- attendee、organizer、共享容器、structured location、非普通 alarm 或 recurrence 使自动 patch/delete 进入 fail-closed；不要靠复制或改标题绕过。
- 用户在原生 App 对 personal-scheduler 对象的手工变化是当前事实。下一次修改基于最新回读和新 fingerprint，不自动恢复旧状态。

## 重复安排

本初版不自动创建、修改、完成或删除原生 recurring Reminder / event。遇到“每天”“每周”等请求时，明确提供两种选择：

1. 按用户确认的明确日期范围或 count 物化有限数量的单次对象，并展示每个日期和总数；
2. 由用户在 Apple 原生 App 中手工建立 recurrence，本 skill 此后只读展示，不自动管理该 series。

选择有限物化时，每个 occurrence 都是带独立 schedule ID 的普通单次对象；这只是一次有限批次，不是假装建立了 recurrence，也不承诺自动滚动。以后扩展日期范围必须重新读取已有对象、展示全部新日期并确认。稳定的学期课程等长期 series 优先让用户在原生 Calendar 中设置。

## 默认输出

### 单项写入回执

```markdown
类型：Reminder / Calendar event
操作：创建 / 修改 / 完成 / 删除
标题：
日期与时间：
目标 list / Calendar：
冲突：无 / 有 / 未检查
本机结果：已创建并回读 / 已修改并回读 / 已完成并回读 / 本机已不存在 / 未执行 / 结果未知
其他设备：尚未核验
```

### 日程查看或编排

```markdown
时间范围：
合并时间线：[已有] / [建议新增] / [建议移动]
待办、deadline 与未排期数量：
空闲与缓冲：
冲突或容量问题：
拟新增或移动：
未排入：
需要确认：
```

根据请求删去无用字段，不为了填满格式编造信息。

## 降级与失败处理

- 随附 executor 缺失、不能构建、版本不匹配或 `doctor/self-test` 失败：不能执行原生 View、冲突检查或 mutation，也不能把“未读取”推断成“没有安排”；只能依据用户主动提供的数据离线编排并输出可复制清单。不使用 AppleScript、UI 点击、iCloud.com 或 CalDAV 盲写。
- Calendar 或 Reminders 没有 `fullAccess`：停止相应读写，不循环触发权限框；另一 provider 可以独立继续。
- source、container、共享状态或 timezone 不明确：停止相应 mutation，不回退 `Default`、local 或其他账号。
- 响应超时或结果不确定：使用原 actual request 调用公开 executor 的 `operations reconcile`，由 executor 核对 journal hash 并执行有界只读对账；`in_flight` / `outcome_unknown` 不重放任何 mutation，也不接受调用方自报的成功证据。
- 部分批量写入成功：保留已回读对象，报告逐项状态，只重试尚未验证项。
- 本机保存成功但 iPhone 尚未显示：如实标为 `verified_local`，不重复创建，也不声称已经送达。

## 使用示例

### 单条 Reminder

```text
使用 $personal-scheduler，明天下午三点提醒我交社团报名表。
```

预期：把“明天”解析成绝对日期，创建一个 timed Reminder，不重复创建 Calendar event；回读成功后只报告本机验证，不声称通知或 iPhone 送达已经验证。

### 安排一天

```text
帮我看看明天已有安排，再把英语阅读和社团申请各安排一小时，晚上九点前结束。
```

预期：先读取用户确认的 Calendar 范围和候选任务，保留固定事件与缓冲，给出多对象预览；用户确认前不写入。

### 不应接管 Goal

```text
把 G-2026-001 的 A002 改到周五，并更新目标计划。
```

预期：路由到 `goal-planner`，不 claim 或修改 Goal-managed Reminder / event。

## 完成标准

只读请求完成时：

- 时间窗、timezone 和读取范围明确；
- 固定事件、Reminder、空闲与冲突没有混淆；
- 未读取或暴露无关私人字段。

写入请求完成时：

- 对象类型、绝对日期/时间、timezone 和目标 container 与用户意图一致；
- 所需确认已覆盖实际影响范围；
- 每个 mutation 都经过结构化写入和本机回读，或明确标为未写入；
- mutation 使用公开 executor 的 preview hash 和 durable journal；terminal 前不重放同一 operation；
- 没有重复创建 Calendar event、覆盖用户 notes 或接管其他 skill 的 managed 对象；
- `verified_local` 没有被夸大成 iCloud、iPhone 或通知送达。

日程编排完成时：

- 固定事件未被擅自移动，deadline 与 duration 没有混淆；
- 计划保留现实缓冲，未排入项和容量冲突清楚；
- 用户确认后才执行 Codex 生成的多对象安排；
- 部分失败和未验证项逐项可见，没有盲目重试。
