# Weekly Review 证据模型

执行实际复盘、判断候选类别或跨来源去重时读取本文件。

## 从 trace 到成果

严格使用以下阶段：

```text
trace
→ evidence
→ evidence cluster
→ result candidate
→ user classification
→ weekly narrative
```

- `trace`：来源直接返回的活动痕迹，例如文件 mtime、Reminder completion、邮件时间。
- `evidence`：经过范围、时间、身份与内容核验，能够支持一个有限事实的痕迹。
- `cluster`：可能指向同一件事情的一组 evidence。
- `candidate`：Codex 基于 cluster 提出的成果、进展、活动、学习或阻塞解释。
- `user classification`：用户确认、改写、降级、合并或拒绝后的含义。
- `weekly narrative`：只使用用户确认含义的周报表述。

任何自动化都只能提出 candidate。不要通过置信度、来源数量或措辞把 candidate 伪装成用户已经确认的事实。

## 证据等级

| 等级 | 例子 | 默认可支持的表述 |
| --- | --- | --- |
| `direct_outcome` | 作业提交确认、成绩、用户确认的 Goal 达成证据、Git merge/发布记录 | 提出 achievement candidate |
| `content_progress` | 文档有实质内容、Git diff、形成结构化 Notes、完成一版演示稿 | 提出 progress；是否完成仍待确认 |
| `execution_signal` | Reminder completion、Goal check-in、用户明确记录“已做” | 支持行动发生；不自动支持结果达成 |
| `activity_signal` | 文件 mtime、Calendar event、下载文件、收到邮件 | 只提示可能相关，不单独形成 achievement |

同一 trace 在不同上下文中可能升级或降级。例如，Sent 中一封普通邮件只是 activity；与作业文件、收件人和提交确认共同出现时，可以成为“已提交”的直接证据候选。

## 时间语义

每条 evidence 区分：

- `occurred_at`：活动或结果实际发生时间；
- `observed_at`：本次复盘读取到它的时间；
- `source_modified_at`：来源自身的最后修改时间；
- `period_membership`：为什么属于本周窗口。

整份复盘另记录开始采集前冻结的 `collected_through`：week-to-date 时是本轮证据截止的实际时刻，完整周时是该周的 `period_end_exclusive` 边界。文档周期始终是完整 ISO 周；所有 evidence adapter 的实际查询区间则是 `[period_start, min(collected_through, period_end_exclusive))`。`observed_at` 可能晚于查询上界，例如周三回顾上周；这不允许把周一至周三的新变化算进上周，也不允许把 week-to-date 截点之后的未来 Calendar event 或 Reminder due 算成本周 trace。

不要把复制时间当作创作时间，把同步时间当作修改时间，或把邮件接收时间当作成果发生时间。普通文件 birthtime 只能说明“本机本周首次出现”；无法证明由 Sylvia 本周创作。filesystem 只有当前最后修改时间：文件在窗口结束后再次改变时，过去窗口的 activity/content 可能无法恢复，必须标 coverage limitation，不能把当前版本回填成旧周事实。Apple Notes 的 `modificationDate` 也只是当前 last-modified；后来再次修改的 note 可能不会再命中过去窗口，因此没有当时 snapshot/version history 时，历史 Notes coverage 必须为 `partial`。

## 候选分类

用户确认时使用以下类别：

- `achievement`：本周得到可描述的结果、交付或里程碑；
- `progress`：形成了实质产出或缩小差距，但尚未完成；
- `activity`：执行过但尚无足够结果证据；
- `learning_or_decision`：获得关键认识、反馈或做出重要选择；
- `blocker`：影响进展且需要后续处理的问题；
- `rejected`：与本周无关、重复、误判或用户不想纳入。

不要用 productivity score、红黄绿表现评级或活动计数评价用户。数量只用于说明 evidence coverage，不作为努力或价值的 proxy。

## Evidence 最小字段

在当前运行的内存/临时结构中，每条 evidence 至少保留：

```yaml
evidence_id: E001
source_type: file | git | goal | reminder | calendar | note | mail | user
source_ref: stable-id-or-local-logical-ref
occurred_at: 2026-09-03T16:00:00+08:00
observed_at: 2026-09-06T20:00:00+08:00
level: direct_outcome | content_progress | execution_signal | activity_signal
summary: "完成民法展示稿第二版"
supports: "展示稿本周有实质进展"
limitations:
  - "尚未确认是否正式提交"
goal_ids:
  - G-2026-001
```

字段只保存在当前工作结构或私有 state 的最小索引中。周文档保留简短证据摘要与必要的逻辑链接，不复制 provider locator、绝对路径或原始正文。

## 跨来源聚类与去重

按以下优先级关联：

1. Goal ID、action ID 或明确的用户引用；
2. Git commit hash、Mail message/thread ID、Note ID、EventKit item ID；
3. 邮件附件与本地文件的实际 hash；只有文件名相同不算相同内容；
4. 同一 approved root 内的规范化相对路径和内容 hash；
5. 标题、课程/项目名、发生时间邻近和内容语义相似性。

低优先级只能产生“可能同一事项”的推断。多个 evidence 合并后仍保留各自来源和限制；不要为了简洁删除相互矛盾的证据。

典型聚类：

```text
PPT 内容变化
+ Reminder complete
+ Sent 中有同名附件的提交邮件
+ Goal check-in 写明“已提交”
= “完成并提交课程展示稿” candidate
```

Git 管理文件优先以 Git diff/commit 表示内容变化，不再把同一路径的 mtime 单独计数。一个 candidate 可以关联多个 Goals，但周报正文只出现一次，在 Goal 部分建立交叉引用。

## 冲突与不确定性

来源矛盾时不要选择看起来更乐观的一项：

- Reminder complete 但文件仍是草稿：候选降为待确认；
- Calendar 有考试但用户没有确认参加：只能记录安排或询问；
- Sent 有邮件但没有提交确认：表述为“已发送”，不自动表述为“提交成功”；
- 文件时间在窗口内但内容与本周无关：拒绝或降为 activity；
- Goal 文档与原生 App 状态不同：报告 drift，交给 goal-planner 对账。

给每个 candidate 只列会影响分类的关键不确定点，避免把复盘变成法证报告。

## 用户确认界面

每个候选展示：建议表述、建议类别、关键 evidence、关联 Goal 和一至两个不确定点。用户可以：

- `确认`：接受或微调表述；
- `降级`：achievement → progress/activity；
- `合并`：与另一 candidate 合并；
- `拒绝`：不进入周报；
- `补充`：添加结果、意义或线下证据。

最终 `Confirmed achievements` 不包含未回答的关键不确定点。若用户不想逐项确认，可以让其确认一份编号后的整批预览；确认必须能明确映射到实际候选集合。

## 周报叙事原则

- 先写结果和意义，再写证据，不堆砌文件列表。
- 成果与有意义进展分开，避免“尚未完成”被写成失败。
- 只保留支持判断的最小证据摘要。
- 对不完整来源如实写 coverage gap，不写成借口或自我评价。
- 保留用户原话中的个人意义，不根据身份、学校或活动量推断情绪、能力或动机。
