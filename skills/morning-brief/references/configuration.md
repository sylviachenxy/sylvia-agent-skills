# 配置、候选与命令

配置与候选均为 UTF-8 JSON，拒绝重复键、非有限数值、未知字段及保留机器标记注入；不使用 JSON 作为读取授权的替代。格式示例在 `assets/config.example.json` 和 `assets/candidate.example.json`，均为虚构数据。

## 配置字段

| 字段 | 语义 |
|---|---|
| `schema_version` | 当前为 1 |
| `config_id` / `config_revision` | 稳定的个人配置 ID；受管更新由 `config save` 自动递增 revision，草稿保留当前号；不能沿用另一用户的配置 |
| `timezone` | IANA 时区；不随系统时区静默改变 |
| `windows.lookback` / `lookahead` | 各有 `start/end: {day_offset,time}`，日期偏移相对于适用日；时钟为 HH:MM，区间半开，实际跨度 >0 且 ≤24h |
| `schedule` | `executor:"mac"`，ISO `weekdays` 1–7，`generate_at < ready_by < wake_at`；正整数 `generation_buffer_minutes` 与 `sync_buffer_minutes` |
| `modules` | 必须列出五个模块，每个有 `enabled/required/max_age_hours/max_items`；启用项必须有相应 scope |
| `storage` | `scope:"private-local"`、独立绝对 `state_dir`、`retention_days`，以及 `notes:{account,folder,shared:false}` |

`config_id`、`state_dir`、Notes 账号/文件夹为固定安装入口；受管更新不能改动。其余合法偏好保存在 Mac，修改后不需要手机重配。手机协议版本独立于配置 schema/revision，不能把配置 revision 当成快捷指令常量。

`required` 只影响启用项的完整/部分判定；禁用项不参与。`max_items` 是正文上限，不应混同原生查询上限。超过查询上限必须报 coverage 缺口；从已读完整集合中编辑精选可以只展示几项。

时区切换或夏令时可能让两个相同钟点间并非 24 小时：以实际经过时间校验；歧义或不存在的钟点拒绝猜测。first cut 的时间输入精度为分钟，一个 profile 对所选星期使用同一组时间。

正常回看终点不得晚于 Mac 采集起点，生成不能晚于 `ready_by`。未到回看截止的提前试跑只能形成 PARTIAL 预览。当前程序不自动排程、不读闹钟，也不会为赶时间补写未来数据。

各启用模块的 scope：

| 模块 | scope 必需字段 |
|---|---|
| weather | `location`，非空 `source_urls` |
| calendar | 非空 `calendar_ids` |
| reminders | 非空 `list_ids`，`overdue_days`（0–90），`include_undated_important` |
| goals | 明确绝对 `vault_path`，非空、Vault 内相对 Markdown `goal_paths`；读取前还要做真实路径和 symlink 边界检查 |
| updates | 非空 `topics:[{id,query}]`，`include/exclude/preferred_sources` 数组，`language`，可为 null 的 `region` |

来源偏好不是允许任意跨主题采集。城市、来源和主题由 setup 用户确认；不能把学校、账号或样例推导为偏好。未来扩展须更新契约与测试，不能私自塞入未识别字段。

`retention_days` 是用户选择的保留意图；first cut 不自动清理本地版本或 Notes，须明确说明并另行确认删除。输出及 checkpoint 保存在私有 state_dir，不在仓库。正式配置通过 `config save` 写入私有不可变快照，由用户级 registry 指向当前版；路径、创建/更新/恢复和手机/排程适配以配置持久化参考为准。`validate-config` 只读校验，既不落盘，也不登记配置或授权。

## 候选不是原始 provider 输出

agent 先按来源规则读取、核查和精选，再填写候选；不能把原生响应不加解释地塞给渲染器。顶层字段为 `schema_version/config_id/config_revision/applicable_date/revision/generated_at/modules`。

每个启用模块记录：

- `coverage`：complete / partial / unavailable / declined。
- `as_of`：本次获得有效信息的来源/快照时间；时间未知用 null。`collected_through`：实际采集截止，不等于前瞻终点，不得晚于读取/生成时间。
- `query_window:{start_at,end_at}` 或 null：真正请求的逻辑范围；不是源系统更宽的候选查询范围。完整 updates/calendar 覆盖须与配置窗口一致。
- `result_count`：读取后符合选定范围的结果数；`items` 是本次精选，不得超配置上限。
- `truncated_reason/error`：没有则 null。源未读取时不混入缓存或虚构计数；complete 不得同时带错误/截断。

每个 item 有 `title/summary/source_url`（私人来源可 null）；可选 `source_label/occurred_at/inference/managed`。模块另需：

| 模块 | item 附加字段 |
|---|---|
| updates | `topic_id` 必须属于选题；`published_at` 必须在回看窗内。对旧报道的实质更新，填经核查的新进展发布时间，并在摘要说明原报道与更新的时间关系，不能把页面抓取时间冒充发布时间 |
| weather | `location`、`valid_from/valid_until`；有效期应覆盖起床时刻 |
| calendar | `start_at/end_at/all_day/status/availability`；排除取消事件，按实际发生时间与前瞻窗口重叠 |
| reminders | `due_date/due_at/important`；前两者至多一个非 null，均 null 时须明确允许无日期且确属重要事项 |
| goals | `goal_id/action_id/approved/status`；只接受已批准、active Goal 的行动，并由 agent 核对选定正本 |

所有时间戳用含 `Z` 或显式偏移的秒精度 ISO 8601。来源的未知值不能伪造；来源时间不明、部分窗口或过旧使必需模块降级。URL 只用支持的安全来源链接；不要把任务备注的任意 URL 或内部原文复制进摘要。

脚本只证明字段、声明和规则一致；无法证明“BLACKPINK”标签真的与正文相关、优先级正确、消息真实或用户曾授权。agent 仍负责这些判断。

## 本地命令

以下从已安装 skill 根目录运行。`BRIEF_PROFILE` 是已保存配置的 ID；`BRIEF_CONFIG`、`BRIEF_CANDIDATE` 是明确的私有绝对文件路径。正常运行每次按 profile 解析当前版；显式 `--config` 留给草稿离线预检与历史恢复，不用于真实发布。

```bash
python3 scripts/morning-brief.py doctor
python3 scripts/morning-brief.py validate-config --config "$BRIEF_CONFIG"
python3 scripts/morning-brief.py config show --profile "$BRIEF_PROFILE"
python3 scripts/morning-brief.py config status --profile "$BRIEF_PROFILE" --require-ready
python3 scripts/morning-brief.py plan --profile "$BRIEF_PROFILE" --date 2026-09-02
python3 scripts/morning-brief.py render --profile "$BRIEF_PROFILE" --candidate "$BRIEF_CANDIDATE"
python3 scripts/morning-brief.py publish --profile "$BRIEF_PROFILE" --candidate "$BRIEF_CANDIDATE"
python3 scripts/morning-brief.py publish --profile "$BRIEF_PROFILE" --candidate "$BRIEF_CANDIDATE" --apply
python3 scripts/morning-brief.py verify --profile "$BRIEF_PROFILE" --candidate "$BRIEF_CANDIDATE"
python3 scripts/morning-brief.py setup-status --profile "$BRIEF_PROFILE"
python3 scripts/morning-brief.py checkpoint --profile "$BRIEF_PROFILE" --stage offline --evidence '当前配置的离线检查通过；未联系原生应用'
```

示例日期仅说明参数格式，实际运行替换为适用日。`render` 保存私有 `runs/<config_id>/<date>/c<config_revision>/r<revision>/package.json` 和 `brief.txt`，同一配置/正文版本不同内容拒绝覆盖。每个新配置版本可以从正文 revision 1 开始，同日新旧配置产物不会互相覆盖。两份文件可分别原子落盘，但不宣称整个目录为跨文件事务；重试同一候选补全缺失文件。

无 `--apply` 的 publish 是离线预检，不联系 Notes；verify 明确只查目标 Note，但会维护本地查证账本。真正发布要求受管配置、已匹配的下游适配记录和用户批准的精确输出范围；初次试发的显式 `--setup-test` 例外见持久化参考。不能因 CLI 接受 apply 就推定用户授权。

checkpoint 阶段是 `config/offline/sources/local_publish/iphone_read/iphone_alarm/timed_run`。它记录观察，不自动运行对应验证。原记录按配置指纹保留；`setup-status` 的 `inherited_stages` 可引用仍匹配的手机接收器绑定，无需因改偏好重测手机，也不伪造当前配置的 checkpoint。

失败退出非零并给结构化错误。验证错误先检查指定字段；原生失败与结果不明按发布/来源规则处理，不禁用保护重试。
