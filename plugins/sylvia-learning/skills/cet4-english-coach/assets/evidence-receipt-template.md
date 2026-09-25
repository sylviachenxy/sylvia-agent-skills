---
schema_version: 1
type: cet4-speaking-evidence
receipt_id: "CET4S-YYYYMMDD-HHMMSS"
source_skill: cet4-english-coach
created_at: "YYYY-MM-DDTHH:MM:SS±HH:MM"
session_mode: baseline_diagnostic
target_standard: "CET-SET4 优秀"
---

# CET-SET4 speaking evidence receipt

> 若已有稳定 `goal_id` 或 `action_id`，加入原值；没有时省略，不生成临时值。完成持续学情 setup 后，保存到选定档案的 `Sessions/<receipt_id>.md` 并用 `oral-record` 登记证据索引；未授权保存则仅在对话输出。不要修改模板本身或默认保存录音。

## 协议依据

- 适用考次：
- 官方来源：
- 事实核实日期：
- 当前来源冲突或未知项：

## 本次条件

- 日期与时区：
- Modality：`live_voice / recording / text_only`
- Partner：`ai:<profile> / human / none`
- Prompt ID 与来源：
- Prompt familiarity：`unseen / partly_seen / seen / unknown`
- Timing：`strict / approximate / unknown`
- Timekeeper、信号方式与 dry run：
- Support：`none / light / structured / modelled`
- Audio：`not_saved / user_saved:<location> / unavailable`
- Transcript：`none / verified_against_audio / asr_unverified`
- Speaker attribution：`not_applicable / reliable / unreliable / unknown`
- 技术中断、提前见题或其他偏差：

## 任务覆盖

每一行只记录一个 task × attempt。冷作答、同题重做和陌生题迁移必须分行，不能覆盖或合并；按需复制空行。

| Evidence ID | 任务 | Attempt | Modality / partner | Prompt ID | 实际准备 / 作答时长 | Timing | Support | 证据位置与限制 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E01 |  | `cold / same_task_retry / unseen_transfer` |  |  |  |  |  |  |
| E02 |  |  |  |  |  |  |  |  |
| E03 |  |  |  |  |  |  |  |  |

## 用户自评

- 最接近真实水平的部分：
- 最影响沟通的部分：
- 互动任务是否达成共同决定，以及依据：

## 官方“优秀”结果对齐

Alignment 只能引用满足准入条件的 Evidence ID；`text_only`、同题重做或受支持 attempt 不得用于结论。

| 结果 | 状态 | Evidence ID 与直接证据 | 反证 / 限制 |
| --- | --- | --- | --- |
| 熟悉话题交流顺畅，少有沟通障碍 | `OBSERVED / PARTIALLY_OBSERVED / NOT_OBSERVED / NOT_ELICITED / INDETERMINATE` |  |  |
| 有条理地表达个人意见 |  |  |  |
| 清楚、流畅地叙述或描述常见事件 / 现象 |  |  |  |

## 官方评分构念的练习观察

每一行只引用一个或一组条件相同的 Evidence ID；不同 attempt / support 必须分行。

| 构念 | 状态 | Evidence ID | Attempt / support | 直接证据 | 反证 / 限制 |
| --- | --- | --- | --- | --- | --- |
| 准确性和范围 |  |  |  |  |  |
| 话语长短和连贯性 |  |  |  |  |  |
| 灵活性和适切性 |  |  |  |  |  |
| 朗读准确性 |  |  |  |  |  |
| 朗读流利度 |  |  |  |  |  |
| 朗读完整性 |  |  |  |  |  |

## 互动证据

| 行为 | 状态 | Evidence ID 与直接证据 | 说明 |
| --- | --- | --- | --- |
| 回应并延伸对方发言 |  |  |  |
| 话轮管理 |  |  |  |
| 立场协商 |  |  |  |
| 澄清与故障修复 |  |  |  |
| 推进并收束共同任务 |  |  |  |

## 结论

`demonstrated_once` 必须引用完整 strict mock；`human_partner_observed` 必须引用严格计时且 speaker attribution 为 `reliable` 的真人证据。否则降级，不因自报提升 Stability。

- Alignment：`demonstrated_once / partial / not_demonstrated / indeterminate`
- Stability：`insufficient / single_observation / repeated_ai_consistent / human_partner_observed`
- Baseline candidate：`yes / limited / no`
- 最可靠优势（最多 2 项）：
- 最高优先级缺口（最多 2 项）：
- 结论依据：
- AI / ASR / 计时 / 搭档限制：
- 声明：这是非官方练习证据，不是 CET-SET4 正式评级或优秀概率。

## 训练交接

- 同题最小练习动作：
- 下一条未见迁移任务：
- 建议复测日期 / 条件：
- 是否建议真人搭档检查：
- 给 `goal-planner` 的采纳提示：
