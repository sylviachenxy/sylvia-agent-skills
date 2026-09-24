# 评估记录契约（由 Codex 操作）

先读 [评估协议](assessment-protocol.md)。使用已授权的相同个人 profile；不另建学生目录，不让用户手工编辑 JSON。现有 `state.json` 是唯一事实源，新增可选 assessment_starts、assessments、assessment_voids 字典；旧档案无需手工迁移。训练 attempts 的 pass/partial 和独立掌握语义不变。

所有命令调用 `<skill-root>/scripts/tutor_store.py`，全局 `--profile`／测试用 `--registry` 在子命令前。输入放已授权私有位置，用文件编辑工具建立；示例名、时间、哈希均须替换成真实证据。所有写入仍用 event_id 幂等、锁、校验、原子写与读回机制，严禁手改 state 或重算哈希绕过冲突。

## 命令表

| 命令 | 输入／输出 |
| --- | --- |
| `materials` | 无需 profile，只读随包来源与分项验收目录；不下载试卷、不显示答案 |
| `assessment-start --input ...` | 新作答开始前冻结材料、范围、曝光与评分计划；返回 assessment_id、不返回答案 |
| `assessment-record --input ...` | 保存对应现场作答及完整分项题项；返回记录与分数报告 |
| `assessment-import --input ...` | 保存实际已存在的历史报告或作答评价；永不伪装现场冷测 |
| `assessment-show --record ID` | 教师侧完整原始记录及逐题证据，包括已更正/作废旧版的active、withdrawals、superseded_by；不在答题前向学生显示 |
| `assessment-report --record ID` | 来源、分项、缺项、边界、限制与下一步 |
| `assessment-compare --first ID --second ID` | 两条有效记录逐项差值范围与可比性限制 |
| `assessment-scenario --record ID --input ...` | 同一次完整成绩上的条件替换；input 为 `{"reading":[30,30]}` 等 |
| `assessment-correct --input ...` | 保留旧版，替换评分／说明；不改变作答身份和曝光 |
| `assessment-void --input ...` | 作废错误记录，但不删除记录或曝光 |
| `context` / `rebuild` | 最近8条有效评估＋全部有效ID索引／重建含全部有效评估的Practice.md；旧报告按ID读取 |

例如：`python3 "<skill-root>/scripts/tutor_store.py" --profile learner assessment-report --record ASSESS-unique`。`--record` 是 assessment-record/import/correct 返回的 record_id，不是 assessment-start 返回的 assessment_id。

## 公共对象

**material** 必有字段：

- id：稳定 ASCII ID（字母、数字、短横线、下划线）；已收集卷用 M25SJ1 等目录 ID；同卷新格式不能伪装新卷。
- title、locator：卷别和实际来源定位（URL／用户持有材料说明），不能只写“高考题”。
- kind：district_mock / past_exam / school_exam / original / unknown。
- sha256：实际题目文件 64 位小写 SHA-256；历史只有成绩单时对原成绩证据文件取摘要，normalization 说明不是题卷指纹，id 仍标同卷；只有口述、没有原件时import可为null，不能杜撰哈希；现场start不可null。
- year：材料所属届别，2000–2100 整数；历史导入无法得知可null，现场须核实；session：january / june / mock / unknown。
- normalization：原分母、题号映射、删去旧听力或其他改动及限制；无改动也明说。脚本只支持本轮 115＋35 工作蓝图；其他结构先做非整卷训练，不线性换算为 150。

**protocol** 必有：baseline_status=provisional / verified_for_named_session；baseline_source（适用目标年份／场次与规则来源）；checked_on（YYYY-MM-DD）；scoring_plan（逐分项 key／rubric 定位、争议题处理、学生/教师材料分离方案）。这些是教师责任声明，脚本不能联网认证。snapshot 同时冻结当前设置和目录状态；未来资料修订不会悄悄更改旧次评估。

**conditions** 为按 scope 的对象：`written` 和／或 `listening_speaking`。每一块有：

```json
{"support":"none","timing":"verified","elapsed_seconds":6300,
 "audio":"none","evidence":"真实计时来源、无提示与执行条件说明"}
```

support=none/hints/answers/unknown；timing=verified/reported/untimed/unknown；elapsed_seconds 为正秒数或 null，verified/reported 必有时长；audio=observed/transcript/none/unknown，笔试只能 none。听说 observed 必须实际接触声音证据；不代表软件录了音。重听、稿件曝光、中断等在 evidence/limitations 明说；辅助后 support 不能写 none。

**scores** 为下列键的任意非空子集：grammar、vocabulary、cloze、reading_abc、reading_gap、summary、translation、writing、listening、oral；历史报告还可以只有 written / listening_speaking / total。oral 仅是“原听说 10 分”的存储短名，不是纯口语。多个层级必须一致，不重复相加；例如听说 32 与听力 20 不可能同时成立。

每个 score 对象必有：

```json
{"range":[104,104],"basis":"user_report",
 "source":"学习者报告某一次模考笔试成绩，尚未查看原卷",
 "rubric":"原报告分母115；没有逐题评分依据","items":[]}
```

basis=key_checked / rubric_estimate / verified_report / user_report。范围不可反转、超过该项满分、为NaN或bool；最多两位小数，不将139.999…擅自四舍五入成达标。报告分不是数学中点；未知分项**省略**，不填0。report给的possible_bounds是数学约束，不是预测值；从总分反推出的范围不会被写成measured score。

key_checked/rubric_estimate 必须是十个细分项之一并带完整 items；新 coach_run 只允许这两类。外部已有成绩从 import 进入。概要、翻译、作文、原听说不能用 key_checked 字符串相等核分；rubric_estimate 范围必须有宽度。关键争议题使整体区间变宽，不用虚假精确度掩盖风险。

**items**：该分项全部题项（满分与区间分别加总等于分项）。每项必有 id、max、range、evidence_kind、response、reason、errors、next_step：

```json
{"id":"Q1","max":1,"range":[0,1],"evidence_kind":"observation",
 "response":"已收到这道题的真实回答；这里只保存允许的观察",
 "reason":"具体争议尚未解决，暂不精确核分",
 "errors":[],"next_step":"复核语境和可接受替代答案"}
```

id 同一记录内不重复；笔试使用规范Q1–56（旧卷先明确映射），脚本验证题号与所属分项及逐题权重，不允许把Q11–20塞进语法。evidence_kind=quote/observation；关闭 store_quotes 时任何字段都不得夹带原话，不只是改标签。errors 是具体错因短标签列表，可以为空；不将题目问题伪造为学生错误。正常客观题只能 0 或题目满分；争议题可用全范围，不能自行发明半分。目录已隔离的题必须本题保留[0,max]，不能把另一题的区间拉宽就给争议题确定分；已知ID换SHA也不会消除隔离。

脚本检查客观题数和权重：语法 10×1、选词 10×1、完形 15×1、阅读 11×2、六选四 4×2；概要 1×10、翻译 3/3/4/5、作文 1×25。真实听说分题权重须另按可靠依据填写，不由脚本制造。

## 五种写入 payload

- start：event_id、material、scope（非空不重复列表）、prior_exposure=unseen/seen/unknown、protocol。只在实际开始前调用。脚本保守检查同卷 ID 或同 SHA 的历史曝光，已作废记录仍算看过。
- record：event_id、assessment_id、occurred_at（带时区实际完成时间）、conditions、scores、limitations（字符串列表）、next_step。完成时间不能早于开始或晚于当前；验证过的时长之和不能超出真实作答窗口。一份 start 只能正式 record 一次。
- import：event_id、material、protocol、occurred_at、conditions、scores、limitations、next_step、provenance（发生时间、提供者、成绩单／原回答定位、同场次归属及哪些已核验）。只有总分且无条件时 conditions 可为 `{}`；有分项须声明相应范围和未知条件。
- correct：event_id、record_id、reason、完整 scores、limitations、next_step。新记录引用 supersedes，旧记录仍保留但退出有效汇总；原 mode、日期、条件、材料、曝光与设置快照不变。其他错误应 void 后真实导入，不回填假现场条件。
- void：event_id、record_id、reason。不能 void 未知／已失效记录；重试用同 event_id 和同 payload。

更正依据也是历史证据。评分被修正后，历史报告中教学结论需跟随新有效记录重新检查；不可把修正造成的涨分当学习进步。

## 输出的正确解释

`cold_conditions` 只表示该场现场作答条件符合检查，**不表示已测完整块、不表示答对、不表示材料／评分合格**；section_evidence 另外列材料门槛、评分类型和限制。strict_official_equivalence 固定 false：本脚本从不认证与正式考试等值。

`full_score_available` 只表示拥有可报告的完整总分（可能只是用户报告），不表示十个分项都有证据或未来稳达标。unmeasured_leaf_sections 是不能归因的项目；即使聚合约束让某未知项的数学范围变窄，也不得伪造实际拆分。

scenario 的 reading=30、reading_abc=22、reading_comprehension=45；不允许重叠项替换。没有本次完整总分或指定分项实际分数时拒绝，改报告缺项上限。区间计算是保守包络，不是统计区间。

个人 JSON 包含教师侧证据；不要答题前整份展示。现有 state 校验／Practice 受管区／单 Mac 同步冲突规则同 [持久化设置](storage-and-setup.md)。旧状态自动可读，新字段仅在首次真实评估写入时出现；升级不会自动造学生记录。
