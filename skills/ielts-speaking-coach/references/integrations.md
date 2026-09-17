# Sylvia 工作流接线

本 skill 是可独立执行的专项教练。只在用户任务需要时交接；检查实际已安装的相邻 skill，读取它的入口后操作，不能假设存在跨 skill 调用 API 或父子继承。

## 所有权

| 内容 | Owner | 本 skill 的动作 |
| --- | --- | --- |
| 训练偏好、口语原始表现、复测队列 | ielts-speaking-coach | 自己持久保存和回读 |
| 完整 SMART Goal、正式基线、合同与 check-in | goal-planner | 提供证据 / 建议，交接给 owner |
| Goal 关联的 Calendar / Reminders 对象 | goal-planner | 包括新建、改期、完成均由 owner 处理 |
| 独立提醒或独立练习时段 | personal-scheduler | 用户明确要排程时交接 |
| 周成果解释与周报 | weekly-review | 提供范围明确的回执，由周报流程确认含义 |
| 起床前当天重点 | morning-brief | 经已批准 Goal / 原生事项进入其已有来源 |

### Goal planner

完整基线结束时输出候选：目标口语 8+、实际条件、四维现状、首要缺口、建议 7–14 天的训练实验、复测条件和可用容量。目标日期未知或进展证据不足时写未知，不替用户作期限承诺。

若已经绑定 Goal，先核对 `Goals/<goal-id>/<goal-id>.md`；action ID 由该文档提供。档案配置的 goal_id 和当次回执的 goal_id 必须一致。不要按“雅思”标题猜关联；独立练习无需先创建 Goal。

交接示例（字段值来自真实记录）：

```yaml
source_skill: ielts-speaking-coach
session_id: IS-actual-id
occurred_at: 2026-09-17T19:00:00+08:00
evidence_path: Learning/IELTS-Speaking/profile/sessions/IS-actual-id.md
evidence_role: baseline_candidate
conditions: rehearsal
finding: "Part 3 在陌生追问中缺少理由展开；计时未核实，尚非完整严格基线。"
proposed_next_action: "先做一次因果解释微练习，再用陌生题无提示复测。"
limitations: ["AI 练习环境，非官方成绩"]
```

已有稳定 goal_id/action_id 时原样附加，示例不包含虚构 Goal ID。Goal owner 决定采纳基线、调整计划、写 check-in 和 Apple 投影；回执留在训练目录，通过相对链接引用，不复制成第二个真相源。

### 日程

用户说“下周安排五次练习”时，给 owner 实际时长、范围、时区、每次训练意图和前置条件。若属于已批准 Goal，即使目前还没有对应 action ID，也交 goal-planner 分配；不能另建 standalone 对象绕过它。

没有 Goal 的明确单次提醒可交 personal-scheduler。队列的 due_date 是建议复测日，不是刚性截止时间；说“下次练这个”不自动创建系统提醒。课程计划、脚本归档和日历经过都不意味着任务已完成。

### Weekly review

用户请求周复盘且训练目录已纳入获准范围时，提供实际周窗口内的有效 session 回执。按 occurred_at / 本地日期判断，不能按同步 mtime 或回执重建时间算新增成果。

同一次 session 的 JSON、Markdown、Practice 索引和 Goal check-in 是同一证据链，不能计成四项成就。被 supersedes 替代的记录不重复计数；复测成功与训练次数分开，教学示范文字不算用户能力。

可推荐表述：“本周完成三次口语训练；在两个新话题中能独立限定观点；发音仍缺少可比较音频证据。”是否列为 achievement、progress 或 activity 由周复盘流程和用户确认。

不要擅自扩大 weekly-review 的扫描范围。未安装时只给阶段证据摘要，不宣称已写周报。

### Morning brief

first cut 没有新增 morning-brief 学习档案适配器。已批准的当天训练行动经 goal-planner 或独立 scheduler 写入后，morning-brief 可按其现有配置读取。只传简短行动和必要 Goal 元数据；不把完整成绩估计、原话或录音放入通知。

用户希望晨报直接展示到期复测时，先交 goal owner 把选中的下一步纳入计划；不要自动修改 morning-brief 配置或声称它已经读取 Practice.md。

### Learning / reading coach

未来 learning-coach 可以根据问题路由到本 skill；没有它也能直接练。需要阅读材料建立话题理解时，deep-reading-coach 可提供阅读证据，随后回到本 skill 做独立口头迁移；阅读理解好不能自动计为口语达标。
