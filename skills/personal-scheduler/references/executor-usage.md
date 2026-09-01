# personal-scheduler Executor 使用规范

仅在需要访问 Apple Calendar / Reminders 时读取。本文件说明随 skill 发布的可执行入口；完整 JSON 字段见 [`../scripts/protocol-v1.json`](../scripts/protocol-v1.json)。

## 唯一公开入口

先把入口解析为相对于本 `SKILL.md` 的真实路径；不要假设当前工作目录就是 skill 根目录。以下示例仅为简写：

```zsh
./scripts/personal-scheduler.sh <command>
```

不要直接调用或执行内部 `apple-eventkit-bridge/` 中的 runner / app；它们的静态 invocation token 只是防误用护栏，不是对当前 macOS 用户的认证或权限边界。支持的 Codex 流程只调用公开 executor，由它负责 owner-only state、操作 journal、preview hash、跨进程锁和超时结果；直接调用内部层属于明确不支持的用法，会失去这些保护。

所有有输入的命令从 stdin 接受一个 JSON object，stdout 恰好返回一个 JSON object。`ok: false` 或非零退出都不是成功。

先运行：

```zsh
./scripts/personal-scheduler.sh doctor
./scripts/personal-scheduler.sh self-test
```

- `doctor` 不读取 Calendar/Reminder 内容，也不触发权限框；检查 bundle identity、授权状态和 state 状态。
- `self-test` 只运行纯 marker、schema、构建和临时 state 测试；不得访问生产日程或生产 state。
- bundle identity、protocol v1、managed schema v1 或 state schema v2 不匹配时停止。

## 首次设置

只为当前任务需要的 provider 请求权限。权限请求可能弹出系统对话框，必须先得到用户同意：

```zsh
print -r -- '{"entity":"reminder","confirmed":true}' | ./scripts/personal-scheduler.sh authorize
```

分别用 `sources list` 和 `containers list` 展示 source/container 的人类可读名称与风险 flags。`.calDAV` 不能单独证明它就是用户想用的 iCloud source；EventKit 也不能可靠证明任意可写 Calendar 一定私有。用户必须分别确认：

- Calendar read set；
- Reminders list read set；
- event write source + container，并确认它是预期的私人写容器；
- Reminder write source + container，并确认它是预期的私人写容器；
- IANA timezone。

先执行空输入的 `settings get` 取得 `revision`，并从这一轮同源 discovery 返回中取得 `event_store_id`；再把确认结果完整写入：

```json
{
  "expected_revision": 0,
  "confirmed": true,
  "event_store_id": "EVENT-STORE-ID-FROM-DISCOVERY",
  "timezone": "Asia/Shanghai",
  "event": {
    "read_container_ids": ["CALENDAR-ID"],
    "write_source_id": "EVENT-SOURCE-ID",
    "write_container_id": "CALENDAR-ID",
    "private_confirmed": true
  },
  "reminder": {
    "read_container_ids": ["LIST-ID"],
    "write_source_id": "REMINDER-SOURCE-ID",
    "write_container_id": "LIST-ID",
    "private_confirmed": true
  }
}
```

```zsh
./scripts/personal-scheduler.sh settings set
```

未使用某个 provider 时，其 read set 为空、两个 write ID 为 `null`、`private_confirmed` 为 `false`。不要用 Default、local、Google 或同名容器自动填空。

创建新 Calendar/list 使用 `containers create`，需要 `COP-UUID`、用户确认过的 source 和两次相同 payload 的 dry-run/execute 流程。创建成功后重新枚举，让用户确认并通过 `settings set` 保存新 container ID。初版没有 container delete。

## View 命令

- `availability`：只返回 occupied intervals；不返回标题或位置。
- `events list`：`detail: busy` 不返回标题；只有用户要求日程详情时使用 `detail: summary`。
- `reminders list`：必须传明确 `list_ids`；默认不含 completed 或 undated backlog。
- `items get`：按 exact source/container/item ID 读取 managed 或 unmanaged 对象。
- `items find`：只在已确认容器和有界窗口内按 `PS-UUID` 恢复 managed 对象；event 必须传 `search_window`。

从 `settings get` 取 read IDs，但仍把本次实际范围显式传给命令。不要扩大到未确认 container。`goal_planner`、`foreign_marker` 和 `malformed_marker` 只读。

## ID

由 executor 生成，不自行拼接或复用：

```zsh
print -r -- '{"kind":"schedule"}' | ./scripts/personal-scheduler.sh id new
print -r -- '{"kind":"operation"}' | ./scripts/personal-scheduler.sh id new
print -r -- '{"kind":"container_operation"}' | ./scripts/personal-scheduler.sh id new
```

- 每个 managed native object 使用一个永久 `PS-UUID`；
- 每次 mutation 使用一个 `OP-UUID`；
- container create 使用一个 `COP-UUID`；
- `prepared` 的同一完整 intent 可沿用原 operation ID 重做 fresh preflight；若要改变 intent，先把该 prepared journal 明确关闭为 `not_applied`，再生成新 operation ID；
- `in_flight` / `outcome_unknown` 只能沿用原 operation ID 做 `operations reconcile`，不得重放 mutation；
- 已由自动对账证明 terminal `not_applied` 后，再次预览和授权使用新的 operation ID。managed create 已预留的 schedule ID 保持不变；claim 只在原对象仍由 exact locator 和 before fingerprint 唯一识别时复用原 schedule ID。不能另造第二个身份。

## Mutation：preview 与 execute

支持 mutation 的命令：

- `containers create`
- `items create | patch | delete | claim`
- `reminders complete`
- `unmanaged items patch | delete`
- `unmanaged reminders complete`

每次 mutation 都使用两段式协议：

1. 生成稳定 operation ID；managed create/claim 还要先生成 schedule ID。
2. 读取 fresh snapshot；patch/complete/delete/claim 保存其 `fingerprint`。
3. 用完整请求执行 `dry_run: true`。executor 返回 `preview_hash`，但不创建 journal。
4. 按 `SKILL.md` 的确认规则向用户展示实际影响；简单明确单项可由当前消息授权。
5. 使用相同 operation ID、相同字段，把 `dry_run` 改为 `false` 并加入原 `preview_hash`。
6. executor 在同一 owner-only lock 中依次写 `prepared`、做 fresh dry-run、写 `in_flight`，把 fresh preflight 的 EventKit store ID 仅作为内部握手字段交给 actual 调用；Swift 在任何 native effect 前再比对，随后才写入、回读并写 terminal。

示例中的 ID 和容器只是占位：

```json
{
  "operation_id": "OP-22222222-2222-4222-8222-222222222222",
  "entity": "reminder",
  "source_id": "CONFIRMED-SOURCE-ID",
  "container_id": "CONFIRMED-LIST-ID",
  "confirm_private_container": true,
  "managed": {
    "schema_version": 1,
    "schedule_id": "PS-11111111-1111-4111-8111-111111111111",
    "entity": "reminder",
    "role": "task"
  },
  "payload": {
    "title": "交课程作业",
    "due": {
      "kind": "date_time",
      "at": "2026-09-08T20:00:00+08:00",
      "timezone": "Asia/Shanghai"
    },
    "priority": 0
  },
  "dry_run": true
}
```

实际执行只新增：

```json
{
  "dry_run": false,
  "preview_hash": "sha256:<dry-run-returned-hash>"
}
```

不要在公开请求中加入 `expected_event_store_id`。它不参与用户可见的 preview intent，而是 executor 在 fresh preflight 后自动注入 actual bridge 调用的内部字段；调用方提供时会被拒绝。

这里表示在原完整 JSON 中替换/加入字段，不是只提交这两个字段。任何字段改变都会使 `preview_hash` 失效并要求新预览。

完整样例还包括 [Reminder create](../scripts/apple-eventkit-bridge/samples/reminder-create.json)、[timed event create](../scripts/apple-eventkit-bridge/samples/event-create.json) 和 [claim](../scripts/apple-eventkit-bridge/samples/claim.json)。event 的 `search_window` 必须同时覆盖当前与目标时间（create 时覆盖目标时间）；timed event 使用：

```json
{
  "search_window": {
    "start_at": "2026-09-08T00:00:00+08:00",
    "end_at": "2026-09-09T00:00:00+08:00"
  },
  "payload": {
    "title": "社团面试",
    "location": "大学生活动中心",
    "time": {
      "kind": "timed",
      "start_at": "2026-09-08T14:00:00+08:00",
      "end_at": "2026-09-08T15:00:00+08:00",
      "timezone": "Asia/Shanghai"
    },
    "alarms": [{"minutes_before": 10}]
  }
}
```

这是 event create/patch 请求中的片段；仍需同时提交对应命令要求的 operation、scope、managed、`dry_run` 等字段。all-day event 的 `time` 改为 `{"kind":"all_day","start_date":"2026-09-08","end_date_exclusive":"2026-09-09"}`。

### Managed 与 unmanaged

- `items create/patch/delete` 和 `reminders complete` 管理带 `[personal-scheduler:v1]` 的对象。
- `unmanaged ...` 只做一次性操作，保留 notes/URL 和未改变字段，不添加 marker。
- `items claim` 只在用户明确要求持续管理时使用；它只添加 personal-scheduler marker，不顺带改标题、日期或其他字段。
- event claim 必须提交覆盖当前 event 的有界 `search_window`；bridge 会在写前确认该窗口与 exact item 一致，并检查目标 container 中不存在同一 schedule ID。
- 对象不是 `unmanaged`、含 recurrence、邀请、unsupported alarm，或目标容器 subscribed/immutable/delegated 时，one-shot/claim fail closed。
- `items patch` 与 `unmanaged items patch` 的 `payload` 都是**全部受管字段的目标快照**，不是只含变化字段的 partial patch。Reminder 必须完整提交当前/目标 `title + due + priority`；event 必须完整提交当前/目标 `title + location + time + alarms`。先从 fresh `items get` 取得原生值，只改变用户授权字段；bridge 保留 payload 之外的 notes、URL、completion、availability 等字段，并在内存中对 notes 做 exact readback compare，不把正文或其副本写进 stdout/state。event snapshot 的 `location` 与 `alarm_minutes_before` 用于无损重建完整 payload；无 location 必须提交 `null`，不能用空字符串代替。

## 结果未知与对账

先用空输入执行：

```zsh
./scripts/personal-scheduler.sh operations list
```

`in_flight` 或 `outcome_unknown` 表示 EventKit 可能已收到 mutation。禁止再次执行原 mutation，也禁止分配新 ID。

把原来的实际 mutation 请求完整重交给公开 executor，让它自行核对 journal intent hash、执行有界 `find/get/list` 并判断结果：

```json
{
  "operation_id": "OP-22222222-2222-4222-8222-222222222222",
  "command": "items create",
  "original_request": {
    "operation_id": "OP-22222222-2222-4222-8222-222222222222",
    "entity": "reminder",
    "source_id": "CONFIRMED-SOURCE-ID",
    "container_id": "CONFIRMED-LIST-ID",
    "confirm_private_container": true,
    "managed": {"schema_version": 1, "schedule_id": "PS-11111111-1111-4111-8111-111111111111", "entity": "reminder", "role": "task"},
    "payload": {"title": "交课程作业", "due": {"kind": "none"}, "priority": 0},
    "dry_run": false,
    "preview_hash": "sha256:<original-preview-hash>"
  }
}
```

```zsh
./scripts/personal-scheduler.sh operations reconcile
```

executor 只会自动收敛为有机器证据的 `verified_local | not_applied | conflict`；证据必须来自 `ok=true`、含非空且与 journal 匹配的 `event_store_id`、且命令级 shape 自洽的成功 read envelope。`items find` 只接受 `(count=0,item=null)` 或 `(count=1,item=object)`，`items get` 必须返回 request 中 exact `item_id` 的 object，`containers list` 必须返回 object array。任何 nonzero/`ok=false` 错误（包括 duplicate、ownership、type 或 recurrence 错误）都不能作为跨 epoch 安全证据，只能保持 `outcome_unknown`。create/container/delete 的零匹配也仍保持 `outcome_unknown`。调用方不能上传自报的 item/container evidence 来宣称成功或没写。对账使用的 item snapshot 只包含结构化受管字段、fingerprint 和 `content_hash`，不返回 raw notes、URL、attendees 或 structured-location 坐标。

`prepared` 明确证明 actual EventKit mutation 尚未调用；若不再沿用同一 intent，可用 `operations resolve` + `{"resolution":"not_applied","confirmed":true}` 关闭并释放新建/claim 的预留身份。进入 `in_flight` 后，`not_applied` 只能由 `operations reconcile` 证明。用户决定停止自动追踪时，可明确把 unresolved journal 记为 `conflict` 或 `abandon_unknown`；后者仍不表示 native mutation 成功或失败，并让对应 managed schedule 保持不可自动写的 unknown 状态。

对于 delete：timeout 后零匹配仍然只是 unknown；任何手动 resolution 都不得把这种 absence 标为 deleted。只有 executor 在未中断的同一调用中收到明确 delete success 并回读缺失，才会写 deleted tombstone。`abandon_unknown` 只关闭 journal，让用户承担后续人工对账；它不表示 native mutation 成功或失败。

## 状态与隐私

生产 state 固定在：

```text
~/Library/Application Support/io.github.sylviachenxy.sylvia-agent-skills.personal-scheduler-eventkit/state-v2.json
```

executor 验证目录 `0700`、state/lock `0600`、当前 owner、普通文件/目录和 no-symlink，使用 atomic replace。state 只保存 IDs、状态、时间、hash 和 fingerprint；不保存 title、notes、location、attendees、账号名称、邮箱或请求 payload。

`state-v2.json` 是 first cut 的首个受支持状态格式。若同目录存在预发布阶段生成的 `state-v1.json` 而 v2 尚未建立，executor 会以 `legacy_state_requires_manual_audit` fail closed；v1 中未解决的 operation 缺少可置信的 EventKit epoch，不能自动迁移、重放或宣称 terminal success。应先在 Apple 原生应用中人工核对相关对象，保留旧文件作为审计证据，并在明确完成审计后再由用户自行移出该目录。

`event_store_id` 发生变化时，executor 在 mutation 前失效全部 saved scopes 和 locators，保留 schedule IDs 与未解决 journal，并要求重新选择容器。即使此前没有 schedule cache，actual result 跨 epoch 时也只能新建 locator 全空的 `outcome_unknown` 记录，不能从旧 request 重建 source/container/item/fingerprint。每个进入 `in_flight` 的 operation 固定记录 fresh-preflight epoch；即使用户后来在新 epoch 重配 scope，旧 unresolved operation 也不能自动对账。历史 terminal success 只在当前 state 仍是同一 epoch 时可作为幂等成功 replay，否则返回 stale-epoch non-success。不要跨 source 或账号扫描恢复。

## 版本限制

- macOS 14 或更高；
- Calendar 与 Reminders 分别需要 full access；
- native recurrence mutation、container delete、邀请事件 mutation、Reminder alarm 写入均不支持；
- ad-hoc app 重建后 macOS 可能要求重新授权；
- `verified_local` 不证明 iCloud server、iPhone 或通知送达。
