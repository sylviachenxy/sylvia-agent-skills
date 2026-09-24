# Sylvia 工作流接线

本 skill 独立可用；其他 skill 是协作分工，不是技术父子继承。只有当前任务真正需要时，检查对方是否已安装并读其入口；不假设存在 `invoke_skill` API。

| 数据／动作 | Owner | 本 skill 提供 |
| --- | --- | --- |
| 英语偏好、题目曝光、真实作答、错因、复测证据 | 本 skill | 在个人档案保存并回读 |
| SMART Goal、正式基线采纳、里程碑、check-in | goal-planner | 来源、样本条件、差距、可行训练实验 |
| Goal 关联 Calendar / Reminders 的新建、改期、完成 | goal-planner | 已核对的 goal_id/action_id 和候选行动 |
| 与 Goal 无关的单次练习时段／提醒 | personal-scheduler | 用户明确请求的时长、时区、时间范围 |
| 周成果解释与周报 | weekly-review | 获准范围内真实时间窗口的证据，不自动扫描新范围 |
| 晨报重点 | morning-brief | 经正式 Goal／独立日程进入已有来源，不新增暗中采集器 |
| 长篇精读与通用阅读习惯 | deep-reading-coach | 特定阅读问题和已有证据；回来后另测英语任务迁移 |

长期备考目标先经过 goal-planner 的 context grounding，不仅改写一个分数和日期。目标已存在时从 `Goals/<goal-id>/<goal-id>.md` 核对身份和 action，不靠标题猜。Goal 文档是正式合同唯一来源；本 skill 中 target_score 是学习偏好，修改它不会自动修订正式合同。

交接最小字段：source_skill、profile_id、session_id、实际 occurred_at、Vault-relative evidence_path、attempt_ids、conditions、findings、limitations、proposed_next_action。已有真实 Goal/action 才附 IDs；不是本 skill 临时编造。脚本的 state.json 与 Practice.md 是同一证据链，不计两次 achievement。

示例（说明格式，不是实际成果）：本周三次练习中，两次在不同新文本里独立识别作者让步关系；概要的信息压缩仍依赖提示。下阶段建议两次短概要训练，再做一次陌生文本检查。不要把示范答案、配置完成或预约经过算作能力进步。

due_date 是复测建议，不自动变成 Reminder deadline。用户只说“下次练这个”不授权创建提醒；说“明晚安排半小时”再交日程 owner。已有 Goal 的行动即使尚无 action ID，也交 goal-planner 分配，不能绕开 owner 建 standalone 对象。

weekly-review 应按作答 occurred_at，而不是文件 mtime 或云盘同步时间归周。失效 attempt 不计成绩；同一 session 多题是一次训练的多个证据。qualifies_independent 是作答条件，只有 independent_pass=true 才是该题的独立通过，仍不能单独宣称持久掌握。没有记录总训练时长时不拿 daily_minutes 偏好或题目计时求和冒充实际投入。

first cut 没有晨报档案适配器，没有自动写 Apple 日历、提醒、Notes 或发送消息。相邻 skill 不可用时给交接包并明确“尚未写入”；本地教学和持久化仍可继续。手机无需随配置更新重新设置。
