# 评估契约与命令

命令均由实际安装的 `scripts/cet4_store.py` 调用，默认读用户选定档案；读取 `--help` 可查入口。脚本严格校验字段和数值，但不替代教师核对题面、实际听音、计时与评分。不要把“schema通过”写成“学生表现已经验证”。

## 笔试：开始前冻结，而非事后补造冷测

`assessment-start --input start.json`：

```json
{
  "event_id":"baseline-start-001", "prior_exposure":false,
  "material":{
    "material_id":"user-paper-a", "title":"用户选定并核验的四级卷A",
    "kind":"user_mock", "locator":"用户材料的真实文件位置及版本",
    "sha256":"实际文件的64位小写SHA256，需要替换此说明",
    "exam_session":"以实际题卷为准",
    "verified_components":["news","conversations","passages","banked_cloze","matching","careful_reading","writing","translation"],
    "answer_status":"checked", "audio_status":"matched", "issues":[]
  }
}
```

此为字段说明，SHA等必须先取得实物再填写；不得提交伪哈希。kind=`official_sample/publisher_reconstruction/user_mock/original`。verified_components只列已核题面／答案的组成部分。answer_status=`checked/partial/unverified`；audio_status=`matched/unverified/missing`。目录中的已下载PDF不自动满足checked/matched。

脚本保存started_at、设置快照、材料身份与曝光。已有同material_id或同文件哈希的开始／导入记录，下一次不能算unseen，即使前次作废；重新排版、改ID或语义相似曝光仍需教练查明。`prior_exposure`不确定按true。

## 笔试：记录与部分证据

`assessment-record --input result.json` 必需：event_id、start_id、occurred_at、sections、conditions、execution、limitations（字符串列表）、next_step（非空）。occurred_at是实际作答完成时间，不是几天后复盘的时间；现场start必须早于作答。结束后才知道的条件不能回填成事前已核验。

sections键只有八个组成部分；缺项省略或null。客观题value是正确题数（整数），不是710分制分数：

```json
{
  "news":{"value":5,"basis":"key_checked","evidence":"已核卷A答案1–7与原答对应"},
  "conversations":{"value":6,"basis":"key_checked","evidence":"已核卷A答案8–15与原答对应"},
  "passages":{"value":7,"basis":"key_checked","evidence":"已核卷A答案16–25与原答对应"},
  "writing":{"value":[10,12],"basis":"rubric_estimate","evidence":"具体题面与学生全文位置；档次依据与反证"}
}
```

news满7、conversations满8、其余四种客观题型各10。客观basis可key_checked／self_report（后者不算已核冷测）；写作／翻译必须rubric_estimate加0–15有序区间。evidence写真实答案／作答位置和依据，不把示例文字当已核证据。空白0与未取得作答null分开。

conditions按writing/listening/reading/translation四块记录已进行的块；每块必需：

```json
{"completed":true,"elapsed_seconds":1500,"timer_verified":true,"support":"none","technical_failure":false,"note":"真实计时与限制说明"}
```

听力块另加play_count、transcript_seen、audio_matched；一遍匹配音频、未看原文才有独立冷测资格。support=`none/location/cue/rule/model/unknown`。未知计时填null、timer_verified=false，不编秒数。completed是该块真实完整作答，不是所有答案都正确。

execution必含三个布尔：single_sitting、order_verified、closed_blocks_respected。只有八组成完整、四块冷测条件成立、同一次按序且无回改收卡块、start至完成不超过4小时且足以容纳申报的各块实际时长，脚本才标complete_cold_written。4小时是保守的技术完整性上限，不是额外考试时间；各块正式时限仍独立约束。跨天拼卷／暂停回查资料不能标完整冷测。

`assessment-report --record <id>`输出：

- raw_sections保留原始证据；未知不补零。
- known_weighted_subtotal只加已记录部分；有缺项则weighted_practice_percent=null。
- 完整卷的weighted_practice_percent单位是教学加权百分比，**不是710报道分**；forecast_710始终null。
- cold_blocks说明各块条件；complete_cold_written更严格。材料未经核验、自报得分、额外播放、提示、超时、技术故障会使相应块不合格。
- 没有公开等值表就没有score预测；主观题区间是教练依据，不是统计置信区间。

## 历史成绩与模卷导入

`assessment-import`公共必需event_id、occurred_at、kind、title、limitations、next_step。

- `kind=official_report`另需report，禁止同时sections/material/conditions：

```json
{"total":560,"listening":170,"reading":220,"writing_translation":170,"oral_grade":"优秀","verification":"verified","locator":"用户已展示的该考次成绩报告位置，不复制证件号"}
```

四个数值可为null但不能全部无信息；已知分项须与总分相容。verification=verified只在实际看到原报告并核对考次／身份归属后填；口头自报填self_report，不能叫已验证基线。旧口试字母等级保留原值及考次，不冒称当前报告。口语永不加到总分。

- `kind=practice_import`另需material、sections、conditions；不能放report。适用于过去做过的卷，即使用户说严格计时也不标本教练现场冷测。外部机构报的“模拟580”没有转换依据时，只在limitations记 `external_mock_score_unverified`，不塞进官方report或转换原始值。

`assessment-show --record <id>`查看笔试或口语原始记录／撤回／被替代状态；`assessment-correct`载荷event_id、record_id、replacement、reason，仅更正笔试。replacement为整份report或整份sections；日期、题卷、条件保留，生成新记录supersedes旧ID，不能把旧更正当新学习进步。`assessment-void`载荷event_id、record_id、reason，撤出活动证据但保留审计与曝光；可撤口语索引。口语误记先撤回并保留旧回执，新回执说明修正；同题不因此重新成为独立证据。

## 比较和假设算术

`assessment-compare --first <id> --second <id>`只比较时间递增的不同作答。官方vs官方给报道分差值和核验状态；练习vs练习按题型并列原始表现与冷测条件，提醒不同卷未等值；混合两类失败。同次作答更正不能作为进步。

`assessment-scenario --record <官方id> --input assumption.json`仅接受已核官方成绩。例如 `{"assumed_reported_components":{"reading":240}}`，其他已知分项保持不变；未知分项不能当0。输出明确prediction=false；假设是报道分，不是“阅读正确率100%”。用户问原始正确率如何提分，应返回教学判断，不绕过这个边界造公式。

## 口语索引与稳定性

完整分任务证据保存在口语模板回执；`oral-record --input oral.json`让跨会话恢复时不用只靠聊天。必需字段：

| 字段 | 内容 |
| --- | --- |
| event_id / occurred_at / pack_id / topic | 稳定ID、带时区实际时间、真实题包、主题；同题换ID仍算曝光 |
| prior_exposure / stage_reveal / technical_failure | 布尔：是否已见、是否逐阶段揭题、是否发生技术故障 |
| support | none / light / structured / modelled |
| modality / audio_observed | live_voice / recording / text_only；是否亲听足量原始音频 |
| partner | none / human / ai:balanced / ai:terse / ai:talkative / ai:dissenting / ai:repairable |
| speaker_attribution | reliable / unreliable / unknown / not_applicable |
| timekeeper | external_timer / human_timekeeper / approximate / unknown |
| dry_run_user / dry_run_partner | 双方10秒测试是否通过，布尔；不能用模型自估替代 |
| task_times | 已测阶段实际秒数：intro、read_prep、read、short_1、short_2、statement_prep、statement、interaction_prep、interaction；未测省略 |
| observations | 下列12维度的status＋evidence；证据来自真实观察，不凭模板填满 |
| receipt_locator / next_step | 实际回执文件／本会话消息位置，下一步 |

12维度：exchange、opinion、description、accuracy_range、discourse、flexibility、read_accuracy、read_fluency、read_completeness、response、negotiation、completion。前3是优秀结果，中间6是人工／朗读构念，末3是互动回应、协商、共同完成。status使用口语rubric五种状态；未触发／不确定不算差，也不算优秀。没有可听音频时12项均INDETERMINATE，文字内容表现另存generic practice。

脚本只按**声明的实际条件**保守计算：完整时限、未见、无帮助、无故障、计时和揭题合格且全部构念观察到，才demonstrated_once；真人还要求可靠话轮归属。不同日期、主题与AI画像的两套完整证据可repeated_ai_consistent，再有严格计时且可归属的真人互动证据才能通过内部excellent_evidence_gate。

稳定性不是永久勋章：脚本以最近30天作为内部复核窗（非官方有效期），近期完整严格模考的反证会重置稳定判断；仍需人工检查未被自动索引的反证和证据质量。技术失败／近似计时只降证据强度，不直接判能力退步。官方等级始终另存成绩报告。

Generic record用none/location/cue/rule/model，口语回执沿用none/light/structured/modelled；两者不能直接平均。口语里light近似location/cue，structured近似rule，modelled近似model；交接时保留原等级与帮助内容，遇不确定取较强支持，不能为了独立标签降级。
