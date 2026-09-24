# Apple 执行与安全规范

仅在 personal-scheduler 需要读取或修改 Apple Reminders / Calendar 时读取。本文件定义结构化 EventKit executor、managed identity 和本机 state 的行为契约；普通方法回答不需要加载。

## 架构与当前实现边界

```text
Apple 原生对象（standalone 日程事实源）
        ↑↓
内部 Swift EventKit app（独立 TCC identity）
        ↑↓
公开 personal-scheduler executor
        ↕ owner-only lock
本机最小 state / operation journal
        ↑↓
Codex personal-scheduler
```

- EventKit 是正式结构化读写层；iCloud 只负责 Apple 设备传播。
- 本机 state 保存容器选择和定位缓存，不复制事件标题、notes、attendees 或完整日程。
- first cut 已随附公开入口 `scripts/personal-scheduler.sh`、owner-only state executor 和独立 Swift EventKit app；命令流程见 [executor 使用规范](executor-usage.md)，机器契约见 [`../scripts/protocol-v1.json`](../scripts/protocol-v1.json)。Codex 不直接调用内部 bridge。内部静态 token 只防误用，不是对同一 macOS 用户的不可伪造安全边界；绕过公开入口属于明确不支持用法，安全性不成立。
- 公开 executor 缺失、不能构建、版本不匹配或自检失败时，不能读取原生 agenda、判断冲突或 mutation；只能依据用户主动提供的数据离线编排并输出可复制清单。不能借用 goal-planner bridge 伪造 Goal metadata。
- AppleScript、Computer Use、iCloud.com、CloudKit、私有 iCloud endpoint 和 CalDAV 不作为自动读写、对账或重试 fallback。
- EventKit 写入并回读成功只证明本机 EventKit store 已提交，不证明 iCloud server、iPhone 或通知已经送达。

## Executor 能力闸门

正式 executor 至少需要以 JSON 等结构化输入输出完成：

- 分别报告 Calendar 与 Reminders 的 authorization status；
- 在用户授权后请求 `fullAccess`，并区分 denied、restricted、write-only 与 unavailable；
- 枚举 source、Calendar/list、container ID、writable/subscribed/immutable/delegate 等可观察风险；EventKit 无法可靠证明任意 Calendar 一定私有，不能返回虚假的 `shared=false`；
- 在用户确认 source 后 dry-run、创建并回读缺失 Calendar/list；初版不删除 container；
- 在明确容器和最小时间窗内查询、读取与创建普通 event/reminder；
- patch、完成普通 reminder、删除普通对象；
- 返回带 availability 语义的 occupied intervals；`free` 不阻塞，`tentative` 与 `unavailable` 必须和普通 `busy` 可区分；
- 严格解析 managed marker，保留 marker 外 notes，并把候选对象分类为 `personal_scheduler | goal_planner | unmanaged | foreign_marker | malformed_marker`，不向 Codex 暴露 raw foreign block；
- 检测 recurrence、attendee/organizer、共享对象、structured location 和不支持的 alarms；
- 每次 mutation preview、fresh preflight 与成功结果都必须返回非空 `event_store_id`。公开 executor 把 fresh preflight 的 ID 作为内部 `expected_event_store_id` 交给 actual Swift 调用；Swift 在任何 EventKit native effect 前再次比对，缺失或变化都 fail closed。actual 成功 envelope 的 ID 若仍与 preflight 不同，不能 terminal success，必须保留 `outcome_unknown` 并清空不可信 locator。该内部字段不属于公开请求、用户确认内容或 preview intent；
- 每次 mutation 后按 ID 与 marker 回读；
- 使用 fresh fingerprint 和进程锁降低 stale write 风险；
- 对校验错误、权限、冲突、超时和“结果未知”返回可区分错误码。

Calendar 与 Reminders 必须分别取得 `fullAccess` 才能进行闭环读取和 mutation；Calendar `writeOnly` 不足以检查冲突或对账。只请求当前任务需要的 provider：单条 Reminder 不顺带请求 Calendar，纯 agenda 也不请求无关 Reminders。系统权限请求可能弹出 macOS 对话框，公开 executor 的 `authorize` 还会强制要求 `confirmed: true`；只能在用户已经明确同意请求权限后触发。权限被拒绝后不要循环请求。

任何未知字段、错误响应、非零退出或 timeout 都不能解释为成功。create/patch/delete/complete 应支持 `dry_run`；Codex 生成的批量安排必须先 dry-run 与预览，再执行相同 payload。

## 与 goal-planner bridge 的隔离

现有 goal-planner executor 强制使用 Goal ID、action/projection ID、Obsidian path、`obsidian://` URL 和 `[goal-planner:v2]` marker。personal-scheduler 不得直接调用它创建 standalone 对象，也不得为普通提醒编造 Goal 数据。

随附的 personal-scheduler executor：

- 使用独立 bundle ID、managed schema、marker、build cache、TCC purpose strings 和测试；
- 可以复用已验证的 EventKit 实现方法，但不能跨 skill 依赖 goal-planner 的安装目录；
- 允许两个 skill 独立安装、授权、升级和卸载；
- 对任何其他 namespace fail closed，不修改或删除 foreign managed block。

## Source、读取范围与默认容器

Calendar 与 Reminders 可能由不同的 iCloud EventKit source 提供，不能假设共用 source ID。`.calDAV` 类型也不能单独证明该 source 就是用户想用的 iCloud source；首次必须让用户确认。

首次设置分开确认：

- 用于冲突检查和 View 的 Calendar 集合；
- 用于 View 与任务候选的 Reminders lists；
- standalone event 的默认可写 Calendar；
- standalone Reminder 的默认可写 list。

优先复用用户明确确认为私人且预期用于写入的 iCloud 容器；不能只凭 `allowsContentModifications` 推断它非共享。只有用户希望隔离时才建议并创建专用容器，例如 Calendar `个人安排`、Reminders list `日常安排`。容器名只是人类标签，执行时必须用 source + container ID 定位。首次选择时可以瞬时展示 source/container 名称，但不能把这些名称、账号或邮箱写入 state。不得自动使用 `defaultCalendarForNewEvents`、`Default`、local、Google 或其他未确认 source。

读取原则：

- 只读取用户确认的 containers 与当前请求所需最小时间窗；
- “何时有空”优先使用 busy intervals，不返回标题；
- 只有用户要求日程详情时读取 title/location；
- notes、URL、attendees、账号名称和 completed history 只在当前操作确实需要时读取；
- goal-planner Calendar 可以贡献 busy interval，但其对象不得由本 skill mutation。

## 本机 state

公开 executor 把最小状态保存在当前 macOS 用户的 Application Support，而不是 skill 安装目录、仓库或 Obsidian：

```text
~/Library/Application Support/io.github.sylviachenxy.sylvia-agent-skills.personal-scheduler-eventkit/state-v2.json
```

实现 schema 的骨架如下；精确字段以 protocol v1 和 executor 的 strict validator 为准：

```json
{
  "schema_version": 2,
  "revision": 3,
  "event_store_id": "<eventkit-store-epoch-id>",
  "timezone": "Asia/Shanghai",
  "scopes": {
    "event": {
      "read_container_ids": ["<calendar-id>"],
      "write_source_id": "<source-id>",
      "write_container_id": "<calendar-id>",
      "private_confirmed": true
    },
    "reminder": {
      "read_container_ids": ["<list-id>"],
      "write_source_id": "<source-id>",
      "write_container_id": "<list-id>",
      "private_confirmed": true
    }
  },
  "schedules": {
    "PS-<UUID>": {
      "entity": "reminder",
      "state": "verified_local",
      "event_store_id": "<eventkit-store-epoch-id>",
      "source_id": "<source-id>",
      "container_id": "<list-id>",
      "item_id": "<last-seen-item-id>",
      "external_id": "<last-seen-external-id>",
      "intent_hash": "sha256:<confirmed-intent>",
      "last_fingerprint": "sha256:<fresh-native-snapshot>",
      "updated_at": "2026-09-01T02:00:00Z"
    }
  },
  "operations": {
    "OP-<UUID>": {
      "kind": "complete",
      "phase": "in_flight",
      "entity": "reminder",
      "schedule_id": "PS-<UUID>",
      "event_store_id": "<fresh-preflight-eventkit-store-epoch-id>",
      "source_id": "<source-id>",
      "container_id": "<list-id>",
      "item_id": "<exact-item-id>",
      "before_fingerprint": "sha256:<fresh-native-snapshot>",
      "intent_hash": "sha256:<confirmed-intent>",
      "created_at": "2026-09-01T02:00:00Z",
      "started_at": "2026-09-01T02:00:01Z",
      "finished_at": null,
      "outcome": null,
      "error_code": null
    }
  }
}
```

规则：

- state 首次创建、修改读取范围或默认写容器前展示变化并让用户确认；
- 固定目录权限使用 `0700`，state 与 lock 使用 `0600`；通过 `O_NOFOLLOW`/`fstat` 拒绝 symlink、错误 owner、错误类型或过宽权限，并在同目录写随机 temp、`fsync`、atomic rename；
- 不保存 Calendar/Reminder title、notes、location、attendee、账号邮箱或凭据；
- operation 只保存 kind、phase、fresh-preflight EventKit store epoch、exact locator、schedule ID、intent hash、操作前 fingerprint 与时间，不保存字段原文；terminal record 保留用于 idempotency 与审计，不能用同一 ID 绑定新意图；
- phase 只使用 `prepared | in_flight | outcome_unknown | terminal`。`prepared` 表示 EventKit 尚未被调用；executor 在调用 child app 前先 durable checkpoint `in_flight`。因此只有 `prepared` 能在 fresh preflight 后首次执行，`in_flight`/`outcome_unknown` 永远不能自动重放；
- unmanaged 一次性 mutation 使用 `OP-<UUID>` journal 记录 exact locator 和 hash/fingerprint，不添加 native marker；container create 使用 `COP-<UUID>`，其 intent hash 覆盖确认 source、entity 与 title，但 state 不保存 title 原文；
- owner-only state lock 覆盖 prepared checkpoint、fresh dry-run、in-flight checkpoint、child EventKit mutation、readback response 和 terminal checkpoint；parent 被中断时已写入的 phase 仍可恢复；
- `event_store_id` 改变时在 mutation 前失效全部 saved scopes 和 locators，保留 schedule IDs、tombstone 与 unresolved journal，并要求用户重新选择容器；actual 成功 envelope 跨 epoch 时，即使此前没有 schedule cache，也只能建立 locator/fingerprint 全空的 `outcome_unknown`，不能从旧 request 重建定位；unresolved operation 不能跨 epoch 自动对账，历史 terminal success 的 replay 也不能冒充当前 store 的验证；
- item/external ID 都只是 cache，可能因完整同步、移动或 recurrence 改变；managed schedule ID 才是主要恢复身份；
- schedule `state` 只使用 `pending | outcome_unknown | verified_local | deleted | conflict | retired`；写后回读成功才变为 `verified_local`，删除成功保留最小 tombstone 而不复用 ID；
- state 缺失或损坏时不盲写，用 marker 和用户重新确认的容器恢复；不从其他账号猜测。

`state-v2.json` 是 first cut 的首个受支持状态格式。若同目录出现预发布阶段的 `state-v1.json` 而 v2 尚未建立，executor 返回 `legacy_state_requires_manual_audit` 并停止；旧 unresolved operation 没有可信 epoch，不能自动迁移或重放。用户须先在 Apple 原生应用中人工核对相关对象，再自行把旧文件移出该目录。

## Managed metadata

personal-scheduler 创建或 claim 的对象使用独立 metadata block：

```text
[personal-scheduler:v1]
schedule_id=PS-550E8400-E29B-41D4-A716-446655440000
entity=reminder
role=task
[/personal-scheduler]
```

规则：

- `schedule_id` 使用随机 UUID，创建后永久不变；title、日期或容器不是身份；
- `entity` 只使用 `reminder | event`，必须与 EventKit 对象类型和 state 一致；
- Reminder role 使用 `task | deadline`；Calendar role 使用 `appointment | commitment | time-block`；
- marker 由 executor 从结构化字段序列化，Codex 不手拼后直接覆盖 notes；
- 只替换自己的唯一 managed block，保留区块外所有用户 notes；重复、嵌套、损坏或 schema 不支持时停止 mutation；
- 不修改 `[goal-planner:v2]` 或任何未知 namespace；
- marker 和 locator state 都不包含账号、邮箱、绝对路径或日程正文。

即使 cached item ID 命中，只要自己的 marker 缺失、损坏、版本未知或 schedule ID 不一致，就失去 managed ownership 并标为 conflict；不得自动补回 marker、claim 或覆盖用户内容。唯一例外是尚未完成且证据自洽的 `pending claim`：journal 必须结构完整，schedule ID 与 pending locator key 一致，并保存 exact item、container 和 before fingerprint；这份证据只允许跳过通用 ownership 的提前判定、进入下文 `claim` 对账，不代表当前对象已经具有 schedule ID，也不预先断言 mutation 结果。journal 缺失、不完整或内部 identity 不一致仍按 conflict 处理。一个对象同时出现多个 namespace marker 时同样只读。

没有 managed marker 的对象不是自动 managed 对象。对用户唯一指定的普通 private、non-recurring 对象，executor 可以提供一次性 patch/complete；明确确认后也可一次性 delete：先以临时 operation ID checkpoint exact item ID、container、fresh fingerprint 和 expected hash，只改用户明确要求的字段，保留其他字段且不添加 marker；timeout 后按 operation journal 对账，无法证明结果时停止，不自动重试。只有用户明确要求持续管理时，才预览并 claim。claim 先分配新的 schedule ID、checkpoint pending locator 与 before fingerprint，再用 CAS patch marker 并回读；不能把 ordinary patch 偷换成 claim。对象带 foreign marker、recurrence、邀请或共享风险时不得一次性 mutation 或 claim。

## Reminder 语义

- `due: none` 表示未排期事项；date-only 只保存 Gregorian year/month/day；date-time 保存年月日时分和 IANA timezone。
- 不用午夜 timed Reminder 模拟 date-only。
- 不用 relative `EKAlarm` 模拟 `Early Reminder`。此前本机测试中，它改变了 UI 显示的提醒时间，却没有形成 Early Reminder。
- executor 初版不写 Reminder alarm，只写确认过的 due date/time；这不等于通知弹出已经验证。
- 用户需要提前提醒时，选择在 Reminders UI 手工设置 Early Reminder，或创建一个单独且明确命名的提前 Reminder；不能静默改 due time。
- priority 只有在用户明确表达时写入，不把 Calendar 冲突或 deadline 自动映射为 flag。
- 完成状态与 completion date 是原生事实。完成普通、非 recurring Reminder 后立即回读。

## Calendar 语义

- timed event 使用带 offset 的 RFC 3339 start/end 与 IANA timezone，end 必须晚于 start；
- all-day event 使用 start date 和 exclusive end date，不转换成午夜 UTC；
- alarm 仅支持用户确认的 display-relative `minutes_before`，写后回读；
- 读取 event 可以把邀请或共享事件视为 busy，但 executor 不自动修改 attendee、organizer、邀请响应或共享设置；
- location-based、absolute 或其他不支持的 alarm，以及带坐标/radius 的 structured location，都会使相关 patch/delete fail closed；
- 已有 event 被手工移动或改名后，以最新 native snapshot 为准，不恢复旧值。

定时 Reminder 出现在 Calendar 的 `Scheduled Reminders` 不等于存在独立 event。只有用户确认真实时间占用或独立事件时才创建 Calendar 对象。

## Recurrence 与有限物化

初版 executor 不创建、patch、complete 或 delete native recurrence。读取到 recurring Reminder/event 时可以展示 series 信息，但任何 mutation fail closed。

有限物化只接受用户确认的明确 start/end 或 count，每个 occurrence 使用独立 schedule ID 并作为普通单次对象管理。预览必须列出全部 dates 和对象数量。它不建立 cadence registry、不承诺滚动续建，也不能称为“已设置重复”；以后扩展时重新读取既有对象并把拟新增日期作为新的有限批次确认。

## 安全 mutation 与对账

创建新对象时先分配不复用的 schedule ID 和 operation ID。第一次 `dry_run: true` 返回绑定完整 intent 的 `preview_hash`，但不写 journal；实际调用必须提交相同 intent、`dry_run: false` 和该 hash。公开 executor 随后在 owner-only lock 中 checkpoint `prepared`，执行 fresh dry-run 和 event-store epoch 检查，再 checkpoint `in_flight` 后调用 EventKit。只有 `prepared` 证明 mutation 尚未开始；一旦进入 `in_flight`，零匹配也不能自动重放。写入和回读成功后 schedule 才 checkpoint 为 `verified_local`，operation 变为 terminal。

对 managed 对象：

1. 从 state locator 按 ID 读取，验证 entity、source/container、schedule marker；
2. locator 失效时，只在确认容器和最小时间窗内按 schedule ID 查找；无 due Reminder 可在目标 list 内按有限 timeout 查找；
3. 恰好一个普通对象匹配时读取 fresh snapshot；零个、多个或 recurrence 都停止；
4. 展示本轮管理字段 diff，取得所需确认；
5. 用完整 payload dry-run，取得 preview hash；
6. 在公开 executor 中提交同一 payload；它在进程锁内依次 checkpoint `prepared`、fresh preflight、`in_flight`，记录 intent hash 和 before fingerprint；
7. 只在 `in_flight` checkpoint 成功后，以该 fresh fingerprint 调用一次 mutation；
8. create/patch/complete/claim 按返回 ID 回读本轮字段和 marker；delete 只有在 executor 明确返回 success，且同一操作闭环按原 locator 与 marker 确认本机已不存在时，才写 `deleted` tombstone；
9. 本机验证后 checkpoint terminal schedule 和 operation record，再处理下一对象；terminal record 保留且不可绑定新意图。

对 unmanaged 对象：先在用户给出的 container/time window 中返回候选，让用户唯一选择。低风险一次性 patch/complete 以及明确确认后的 delete 按 fresh ID/fingerprint 和 operation journal 执行且不加 marker；需要持续管理时才在确认 claim 后添加 marker。不得自行采用搜索 API 返回的“第一个结果”；但用户针对本轮刚展示、仍在 fresh snapshot 内的编号候选明确说“第一个”时，视为唯一选择。snapshot 已变化时重新展示。

响应 timeout、进程中断或返回 outcome unknown 时，operation checkpoint 为 `outcome_unknown`。调用方必须把原 actual request 完整重交给公开 executor 的 `operations reconcile`；executor 自行核对 operation ID、command 和 journal intent hash，再按 kind、before fingerprint 和最小 scope 发起有界只读对账。调用方不能提交自报 item/container evidence：

- `create`：成功 read 中恰好一个 marker 匹配且受管字段 hash 一致才是 `verified_local`；零匹配保持 unknown；同 epoch 的自洽结构化 snapshot 显示字段不符时为 conflict。multiple/duplicate 若由 nonzero/`ok=false` 错误 envelope 表达，因为没有可验证 epoch，仍保持 unknown，并等待用户审计后显式 resolve conflict。
- `patch` / `complete`：恰好一个对象且目标字段 hash 一致才是 `verified_local`；仍与 before fingerprint 相同则为 `not_applied` 并要求新预览；其他 drift 为 conflict。对象不存在不能推断成功。
- `claim`：原 item 恰好一个，且新 marker、schedule ID 与 expected hash 全部一致时才是 `verified_local`；before fingerprint 未变且仍无 marker时为 `not_applied`；零匹配保持 unknown；同 epoch 的自洽 snapshot 显示 foreign marker 或其他 drift 时为 conflict。multiple/duplicate 错误 envelope 本身不能 terminalize，仍保持 unknown。
- `delete`：timeout/outcome unknown 后即使零匹配也保持 `outcome_unknown`，因为对象可能被并发移期、移容器或更换 locator；不得写 deleted tombstone。对象仍在且 fingerprint 未变为 `not_applied`，已变化为 conflict。只有 executor 已明确返回 delete success，并在未中断的同一操作闭环完成原 locator/marker 缺失确认时，才能 terminal 为 `deleted`。

零匹配或缺少可恢复的 operation 证据都保持 `outcome_unknown` 并停止本轮，不能落回普通 mutation 分支。只有 `operations reconcile` 把原 operation 明确收敛为 `not_applied`，用户看到结果、重新预览并再次授权时，才可使用**新的 operation ID**重试；managed create 仍复用原 schedule ID。claim 只在原 exact locator 和 before fingerprint 仍能唯一识别同一对象时复用原 schedule ID，不为同一对象另造第二个身份。unmanaged 操作仍使用原 exact locator 做对账，不能转成盲目 create。写入成功但 terminal checkpoint 失败时，下一次按 marker 和 unresolved operation 恢复，不创建替代对象。发现同一 schedule ID 对应多个对象时报告 duplicate，不自动删除或合并；无 epoch 的 duplicate error 继续保持 unresolved，用户人工核对后才能显式 resolve。

所有 terminal 对账证据都必须来自 `ok=true`、`mutated=false`、command 精确、非空且匹配 journal 的 `event_store_id`、命令级 shape 自洽的 read envelope。`items find` 只接受 count 0/no item 或 count 1/一个 exact managed item；`items get` 必须返回 exact entity/source/container/item ID；`containers list` 必须返回结构完整的 object array。任何 nonzero、`ok=false` 或矛盾 success envelope 都保持 `outcome_unknown`。

创建 Calendar/list 使用 `COP-UUID` 和相同的 preview/execute phase；intent hash 覆盖确认 source、entity 与 title，state 不保存 title 原文。outcome unknown 后，调用方必须在原 actual request 中重传原 title；`operations reconcile` 核对 intent hash，并只在确认 source 内有界 enumeration：恰好一个同 title、同 entity、可写且非 delegated/subscribed/immutable 的 container 匹配时，才收敛为 `verified_local`；零匹配保持 unknown，多个匹配标 conflict。不得盲目重放 container create。初版不删除 container。

删除、批量完成、批量改期、移动容器、合并 duplicate 和创建 Calendar/list 都要逐项预览。初版 executor 不删除 Calendar/list；删除 Reminder 可能进入 `Recently Deleted`，executor 不清空该区域。

## iCloud 与失败边界

- 只有 source 已由用户确认为 iCloud、write container 已明确确认为私人用途、可写且本机回读一致时，才能报告 `verified_local`；
- Apple 没有逐项 iCloud server acknowledgement；另一台设备可见性只能作为人工观察证据；
- 不索取 Apple Account 密码、app-specific password 或 iCloud token；
- 一侧 provider 不可用时另一侧可以独立工作，不创建替代对象掩盖失败；
- 部分批量成功时保留已验证对象，以 schedule ID 对账未验证项；
- executor 缺失、权限不足、source/container 不明确或对象不安全时，输出可复制清单并停止。

## 事实来源

- [EventKit](https://developer.apple.com/documentation/eventkit)
- [访问 EventKit store 与授权](https://developer.apple.com/documentation/eventkit/accessing-the-event-store)
- [创建 Calendar events 与 Reminders](https://developer.apple.com/documentation/eventkit/creating-events-and-reminders)
- [Recurring event 语义](https://developer.apple.com/documentation/eventkit/creating-a-recurring-event)
- [EventKit identifier 限制](https://developer.apple.com/documentation/eventkit/ekcalendaritem/calendaritemidentifier)
