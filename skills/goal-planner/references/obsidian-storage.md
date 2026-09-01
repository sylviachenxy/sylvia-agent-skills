# Obsidian Goal 存储规范

仅在定位 vault、创建或更新正式 Goal、记录 check-in 或维护 Goal 索引时读取本文件。尚未通过 Goal Readiness Gate 的访谈和草稿不需要读取。

## Vault 定位

按以下顺序确定本机 vault：

1. 复用用户在当前请求或既有配置中明确给出的 vault；
2. 当前工作目录位于含 `.obsidian/` 的目录树中时，使用最近的该祖先目录；
3. 必要时读取 Obsidian 的本机 vault 配置，只接受其中仍存在且含 `.obsidian/` 的路径；
4. 只有一个候选时可以使用；多个候选时让用户选择。

必须确认 vault 目录和 `.obsidian/` 均存在且目标位置可写。不要新建 vault，不要为寻找 vault 全盘扫描用户目录，也不要根据同步盘文件夹名称猜测。绝对路径只用于本次本机文件操作，不写入 Goal 文档、Reminders、Calendar 或 skill 仓库。

## 固定目录结构

```text
<Vault>/
└── Goals/
    ├── Goals.md
    └── G-2026-001/
        ├── G-2026-001.md
        ├── check-ins/
        │   └── 2026-09-07.md
        └── sources/
```

规则：

- 所有持久化引用使用 vault-relative path。
- `Goals/G-YYYY-NNN/G-YYYY-NNN.md` 是唯一完整 Goal 文档。
- 标题、状态和目标日期不进入路径；Goal 变化时不重命名或移动目录。
- `check-ins/YYYY-MM-DD.md` 保存当日 check-in 历史。
- `sources/` 只保存与 Goal 直接相关、用户有权持有且值得长期保留的本地材料；不要自动复制网页全文或私人账号数据。

## 分配 Goal ID

只在 Goal Contract 已经确认且 Gate 为 `GOAL_READY` 后分配 ID。

格式固定为 `G-YYYY-NNN`：

- `YYYY` 是正式创建年份，不是目标截止年份；
- `NNN` 是同一 vault、同一年内三位单调递增序号；
- 扫描 `Goals/` 下匹配的目录和主文件，取已出现最大序号加一；
- 已存在的目录或主文件都视为占号；不补洞、不复用、不重排；
- ID、目录和主文件名创建后永久不变。

若发现重复 ID、目录名与主文件名不一致、同一编号对应多个目标或即将创建的路径已被占用，停止写入并先对账。不得依赖 `Goals.md` 分配编号，也不得按标题识别同一 Goal。

## Goal 文档

创建时以 [Goal 文档模板](../assets/goal-document-template.md) 为基础，只保留适用字段。Frontmatter 至少包含：

```yaml
schema_version: 1
type: goal
goal_id: G-2026-001
title: "2026 年 12 月英语四级达到 500 分"
status: active
created: 2026-09-01
target_date: 2026-12-12
review_cycle: weekly
timezone: Asia/Shanghai
tags:
  - goal
```

规范：

- 日期使用 ISO `YYYY-MM-DD`；具体时刻使用带 offset 的 ISO 8601。
- `status` 只允许 `active | paused | achieved | abandoned`。
- `trajectory` 若记录，只允许 `unknown | on_track | at_risk | off_track | blocked`。
- `contract_version` 从 `1` 开始；修改目标结果、目标值、成功标准、核心期限或主要取舍时递增。
- `target_date` 不适用或尚未确定时删去该字段，在正文记录已确认的时间窗口；不要写空值或 `N/A`。
- 对包含冒号、`#`、日期式字符串或其他 YAML 特殊字符的值正确加引号。

Goal 文档至少包含会影响决策的以下内容：SMART Goal、动机与取舍、成功标准、当前基线与差距、范围边界、可行性、权威事实、里程碑、未来 7–14 天计划、check-in 节奏、replan triggers、证据和决策记录。不要复制完整聊天或用模板空标题制造伪完整性。

行动使用稳定 ID `G-YYYY-NNN-A001`、`A002`……。行动标题和计划日期可以改变，ID 不变；退役后的 ID 不复用。主文档中的当前计划应让 Reminder 和 Calendar event 投影都能引用同一个行动。

用户确认的重复行动主题另分配稳定 cadence ID `G-YYYY-NNN-C001`、`C002`……。cadence ID 表示一版已确认的频率、星期/日期、原始本地时刻和时区规则，不是 EventKit recurrence。规则实质变化时退役旧 cadence ID，为新规则分配新 ID；不得改写旧 ID 的含义。每个已物化 occurrence 仍取得自己的 `A` ID，并在当前计划中同时保存 cadence ID 与不可变的原始 cadence slot。

更新前读取完整文件。只修改目标字段或明确的 managed block，保留用户在 Obsidian 中增加的未知 frontmatter、正文和注释。不要通过重新生成整份文档覆盖用户内容。

## Goal 索引

`Goals/Goals.md` 是可重建的人类导航页，不是事实源，也不参与编号分配。只维护以下区块，并保留区块外的用户内容：

```markdown
<!-- goal-planner:index:start -->
- [[Goals/G-2026-001/G-2026-001|2026 年 12 月英语四级达到 500 分]] · active · 2026-12-12
<!-- goal-planner:index:end -->
```

索引按 Goal ID 排序，每个 Goal 恰好一条。没有目标日期时省略日期。区块不存在时追加；开始或结束标记重复、损坏或嵌套时停止覆盖并报告冲突。状态变化只更新索引行，不移动 Goal 文件。

首次创建使用可恢复的本地提交顺序：

1. 主文档同步账本以 `local: initializing` 写入并回读；
2. 创建或更新 `Goals.md` 索引并回读；
3. 再把主文档账本 patch 为 `local: verified` 并回读；
4. 只有 `local: verified` 后才允许创建外部投影。

若流程在索引完成前中断，已存在且内容合法的主文档仍然占用该 Goal ID。恢复时按该文件补全或修复索引，再标为 `verified`；不得分配新 ID 或重复创建 Goal。若主文档自身不完整、与路径冲突或无法确认是否属于同一次写入，保持 `initializing` 并停止，先让用户对账。

## Check-in 记录

每个 Goal 每天使用一个 `check-ins/YYYY-MM-DD.md`。首次 check-in 创建文件；同日再次 check-in 时追加带本地时间的小节，不覆盖早先内容。

每次至少记录适用项：

- 本次时间与 Goal ID；
- 新证据及来源；
- Reminder 完成信号和 Calendar 安排变化；
- 基线、容量、阻塞或外部事实变化；
- status、trajectory 或 Goal Contract 是否变化；
- 继续、拆小、改期、替换、暂停、达成或放弃的决定；
- 下一步和待同步差异。

Check-in 文件保存历史事实；主 Goal 文档保存当前状态。先写并回读 check-in，再更新并回读主文档，最后更新外部投影。

## 投影同步账本

在主 Goal 文档末尾维护一个隐藏的 machine-managed block：

```markdown
<!-- goal-planner-sync
schema_version: 3
local: verified
overall: partial
last_reconciled_at: 2026-09-01T10:00:00+08:00
event_store_id: "<eventkit-store-epoch-id>"
projections:
  G-2026-001-R001:
    provider: apple_reminders
    action_id: G-2026-001-A001
    cadence_id: G-2026-001-C001
    occurrence_anchor: "2026-09-07T14:00[Asia/Shanghai]"
    source_id: "<eventkit-reminders-source-id>"
    container_id: "<eventkit-list-id>"
    item_id: "<last-seen-calendar-item-id>"
    external_id: "<last-seen-external-id>"
    expected_schedule:
      kind: date_time
      at: "2026-09-07T14:00:00+08:00"
      timezone: Asia/Shanghai
    last_seen_schedule:
      kind: date_time
      at: "2026-09-07T14:00:00+08:00"
      timezone: Asia/Shanghai
    managed_hash: "sha256:<canonical-managed-fields-hash>"
    state: verified_local
    last_verified_at: 2026-09-01T10:00:00+08:00
  G-2026-001-E001:
    provider: apple_calendar
    action_id: G-2026-001-A001
    cadence_id: G-2026-001-C001
    occurrence_anchor: "2026-09-07T14:00[Asia/Shanghai]"
    source_id: null
    container_id: null
    item_id: null
    external_id: null
    expected_schedule:
      kind: timed
      start_at: "2026-09-07T14:00:00+08:00"
      end_at: "2026-09-07T15:00:00+08:00"
      timezone: Asia/Shanghai
    last_seen_schedule: null
    managed_hash: null
    state: pending
-->
```

本地 `local` 只使用 `initializing | verified`。单项 `state` 只使用 `pending | verified_local | failed | conflict | retired`；`overall` 只使用 `pending | partial | projected | conflict`。`verified_local` 和 `projected` 只表示本机 EventKit 回读一致，不表示 iCloud server 或 iPhone 已经收到。错误只记录清洗后的错误码或简短摘要，不保存完整响应、Apple Account、账号邮箱、token 或其他凭据。

开始任何 EventKit 写入前，先从当前计划确定本轮全部预期投影，分配不复用的 `R/E` projection ID，把每一项的 provider 和 `state: pending` 写入同步账本并回读。已经唯一确认的 source/container 同时写入；权限或能力不可用、尚未能合法枚举或需要用户选择时，保留 `source_id: null` 和 `container_id: null`，仍预分配永久 projection ID，也不阻塞另一 provider。权限恢复后只在用户确认唯一 iCloud source/container 并回读定位后补齐 locator，然后才能对该项 dry-run 或写入。模板中的空 `projections` 只表示尚未请求任何 Apple 投影；不能先 create 再补 projection ID 或账本。

每个已物化的重复行动或时间块 occurrence 都是一个具体行动：在当前计划中保留自己不复用的 `action_id`，并在账本中稳定映射到自己的 `projection_id`。同一概念 occurrence 的 Reminder 与 Calendar event 共用 action/cadence/anchor，但各自使用独立 projection ID。滚动到下一个 7–14 天窗口时，以 `(cadence_id, occurrence_anchor)` 判断 cadence slot 是否已经物化：重叠窗口中已有的 slot 必须复用原 action/projection ID，只为新进入窗口的 slot 分配新 ID。

`occurrence_anchor` 是 occurrence 按该版 cadence 首次生成时的不可变原始 slot：date-only 使用 `YYYY-MM-DD`；定时 occurrence 使用 `YYYY-MM-DDTHH:mm[IANA/Zone]`，例如 `2026-09-07T14:00[Asia/Shanghai]`。它不等于当前 due/start time。手工或计划内移期只更新 `expected_schedule`，回读只更新 `last_seen_schedule`，两者都不得改写 anchor；完成、取消、移期或退役后仍保留 action/cadence/anchor 与 projection 历史映射。这样即使一个 occurrence 被移到另一 cadence 日期，后续重叠窗口也不会把原 slot 再创建一遍。非 cadence 对象的 `cadence_id` 和 `occurrence_anchor` 都写 `null` 或同时省略，不得只保留其中一个。

`expected_schedule` 和 `last_seen_schedule` 使用 bridge payload 的同一语义：Reminder 为 `none`、`date` + `date` 或 `date_time` + `at/timezone`；Calendar 为 `timed` + `start_at/end_at/timezone` 或 `all_day` + `start_date/end_date_exclusive`。`expected_schedule` 保存 Obsidian 当前期望，`last_seen_schedule` 保存最近一次 EventKit 回读，从未回读时为 `null`。RFC 3339 回读可能把同一 instant 规范化成不同 offset；timed schedule 按 instant 加 IANA timezone 比较，不按原始字符串逐字比较，date-only 则按日历日期精确比较。独立 check-in 或 deadline 没有 action 时 `action_id: null`。

`event_store_id` 标识当前 EventKit store epoch；它发生变化时，原 source/container/item locator 全部视为待恢复。`item_id` 与 `external_id` 都只是最近一次观察到的 locator，不是永久身份；完整同步、移动和 recurrence 都可能使它们变化或重复。`projection_id` metadata 才是主要幂等键。每次对象成功并回读后，立即把 store epoch、source/container、identifiers、受管字段 hash、last-known 日期或时间、验证时间和状态 checkpoint 回主文档。若 checkpoint 写入失败，停止创建后续原生对象。Obsidian URI 只用于点击，不是对象身份；vault 改名或迁移时可以重新生成 URI，不改变 Goal ID 或相对路径。

`managed_hash` 只对已确认的 managed metadata 和期望 payload 做确定性摘要，不包含 `user_notes` 或 managed block 外的用户文字。它用于判断投影的期望内容是否变化，不是 bridge mutation 所需的 `expected_fingerprint`。后者必须直接取自本轮最近一次 EventKit `find/get` 回读，不得从 `managed_hash` 推导。无法按稳定 canonical JSON 计算时保留 `null` 并按字段对账，不伪造 hash。

## 本地写入验收

在创建任何外部对象前，必须从磁盘回读并确认：

- 目录、主文件名和 frontmatter `goal_id` 完全一致；
- status、日期和必需字段合法；
- 当前行动、cadence 和 projection ID 各自唯一，且没有复用历史 ID；每个 cadence occurrence 都有成对且不可变的 cadence ID / occurrence anchor；
- Goal Contract 与用户确认的版本一致；
- `Goals.md` 中恰好有一条指向该稳定路径的链接；
- 同步账本的 `local` 为 `verified`；
- 同步账本语法完整，所有预期投影最初为 `pending`、`verified_local` 或已被安全认领；只有因权限、能力或用户选择尚不可用的 `pending` 项允许 source/container 为 `null`。

本地主文档写入失败时停止，不创建任何 EventKit 对象。Apple 投影失败不回滚或删除已验证的 Obsidian Goal，只更新同步账本并说明如何继续。
