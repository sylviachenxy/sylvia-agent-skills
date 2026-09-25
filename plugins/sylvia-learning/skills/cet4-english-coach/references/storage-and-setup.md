# 持久化与首次 setup

## Owner 与路径

用户先选一个**已存在**的 Obsidian Vault、稳定档案别名和保存范围。不要递归扫描磁盘找“可能的学情”，也不要把用户真名／邮箱写进源码。可先不保存，明确本次仅在会话中可用。

```text
<Vault>/Learning/CET4-English/<profile-id>/
  state.json       # 权威配置／证据，脚本管理，有校验和
  Practice.md      # 可阅读派生视图，带校验的管理块 + 用户笔记区
  Sessions/        # 按需保存完整口语回执、用户批准的教学摘要
```

`profile-id` 为1–100字符ASCII字母／数字开头，可带 `_`、`-`。本机定位表为 `~/Library/Application Support/cet4-english-coach/profiles.json`，仅存档案ID→绝对目录，不同步到skill。换Mac时选择已同步Vault，用 `init --rebind` 绑定已有同名档案；不能以新空档冒充恢复。不同学习者独立profile，不自动读取其他人的记录。

Vault同步由用户已有设置负责，skill不创建Google Drive授权或手机配置。事先说明保存的文字可能随Vault同步；默认 `store_quotes=false`，仅存必要观察摘要。开启短原话保存由用户决定。脚本不保存／删除录音，也不提供加密；敏感身份信息和音频保留策略不要塞入自由文本。

## 命令定位与首次验证

下列 `$SKILL_ROOT` 是**当前实际安装的 cet4-english-coach 目录**，不是固定开发路径。agent解析后用绝对路径执行，JSON输入放用户授权的临时／个人目录，不放skill源码。不要求用户手写JSON。

```bash
python3 "$SKILL_ROOT/scripts/cet4_store.py" profiles
python3 "$SKILL_ROOT/scripts/cet4_store.py" --profile learner init --vault "/selected/existing/Vault"
python3 "$SKILL_ROOT/scripts/cet4_store.py" --profile learner show
python3 "$SKILL_ROOT/scripts/cet4_store.py" --profile learner context
```

路径仅为命令形式示例，执行前替换为用户选定路径。init成功后核对 `saved`、`views_verified`、Practice.md可读；再从新进程 `show/context` 读回。别把“建了文件夹”称作setup完成。可选Goal绑定先核对该Goal确实属于本用户且已通过。

默认配置：exam_year=null、exam_session=unknown、target_score=null、oral_target=null、timezone=Asia/Shanghai、daily_minutes=30、feedback_language=zh-CN、store_quotes=false、goal_id=null、baseline_note空。年份／考次不是实际报考成功的证据；确切日期、来源、报名状态可在baseline_note保存简短结构化文字并按考次复核。

`configure --input change.json` 使用如下载荷；`expected_revision` 来自刚读回的show，任一已保存事件都会增加revision，不可用旧context猜：

```json
{"event_id":"cfg-001","expected_revision":1,"patch":{"target_score":580,"oral_target":"优秀","daily_minutes":90}}
```

上述580是示例而非默认。target_score是0–710整数；严格“大于580”应先确认是否以581作阈值。oral_target为null／优秀／良好／合格；exam_session仅unknown／june／december；timezone为有效IANA名。未知字段失败，不静默忽略。配置历史保留，任务／尝试保存当时快照。更改目标不重写旧证据。

## 微能力工作流

1. `prepare --input item.json`，载荷 `{"event_id":"prep-001","item":{...}}`。item字段参考 assets/starter-items.json 中的一项；不要整份题库作为一个item。答案／rubric冻结后不可原位改，修题用新ID并披露曝光。
2. `present --input present.json`：`{"event_id":"present-001","item_id":"CET4-R-CAUSE-01","prior_exposure":false}`。陌生度须问用户／查历史；未知保守填true。返回的stem是学生版，不含key。学生已经看到题面就算曝光，即使没交卷也不能再次冷测。
3. 用户作答后 `record --input attempt.json`，必需字段示例：

```json
{
  "event_id":"attempt-001", "presentation_id":"present-001", "session_id":"session-001",
  "occurred_at":"2026-09-25T10:05:00+08:00", "support":"none", "result":"pass",
  "modality":"text", "audio_observed":false, "evidence_kind":"observation",
  "evidence":"选择B并指出新学期和自习室关闭也是可能原因。",
  "reason":"结论和因果范围解释均符合冻结判据。", "errors":[],
  "next_step":"在交通主题新题上独立迁移。", "new_context":false, "transfer_note":"",
  "timer_verified":true, "elapsed_seconds":95
}
```

**示例是格式，不是可代用户导入的真实表现**；替换日期、ID与观察。occurred_at要带时区且不早于呈现，不记录未来表现。

support=`none/location/cue/rule/model`（本轮实际最高支持）；result=`pass/partial/fail/unassessed`；modality=`text/transcript/audio`；evidence_kind=`observation/quote`。关闭原话保存时必须observation且自由文本也应转述。`new_context=true`要求transfer_note说明改变了什么；同题重做不得这样标。

听力还必须提交play_count整数、transcript_seen布尔、audio_matched布尔；这里 `modality=audio` 指确有已观察／核验的音频刺激，不要求选择题答案也是口头说。仅有原文或自报音频不能填audio_observed=true。一遍音频、无原文且匹配材料才有独立听力资格。口语全文构念用下面的oral-record而非generic pass代替优秀。

支持后重做可对同presentation记录新attempt；脚本不会再把它算独立。文本指纹可阻挡同干换ID，不能识别所有语义同题，teacher仍要确认。错误／部分完成会使该微能力回到needs_work；第一次无提示独立成功→independent，已有独立成功后再次在新语境成功→transferable，距第一份独立成功至少7天的新语境成功可成为durable。仅在提示后会做、随后第一次新题成功时仍是independent，不直接晋为transferable。它们**不是笔试580／口语优秀**。

`context --on YYYY-MM-DD --focus <focus>`可查看复测建议；默认当前用户时区日期。到期依据取最后有意义的独立证据／反证，受指导刷题和未评分尝试不应延后复测。

## 整卷、成绩与口语

笔试用 `assessment-start/record/import/correct/void/report/compare/scenario`，严格字段见 [assessment-contract.md](assessment-contract.md)。历史导入不冒充现场冷测；旧回执只做历史说明，不能自动生成当日亲历证据。

口语完整回执使用模板，获准后放 `Sessions/<稳定receipt-id>.md`。再用 `oral-record` 登记；schema见assessment-contract。JSON维护可计算条件与索引，Markdown保留分任务证据／实际时间和限制。`receipt_locator`应指向这个真实文件或明确的本会话消息位置；脚本不替你验证外部文件、听见音频或鉴定评分。

若已有稳定Goal，configure绑定 `G-yyyy-nnn`；脚本验证 `<Vault>/Goals/<id>/<id>.md`存在。记录可带该Goal的 `action_id`（`<goal>-A<至少3位数字>`），不生成ID、不写Goal或Apple工具。Goal文件存在性不是完整合同验证，agent仍需读准目标和权限。

## 错误、恢复与并发

- 同event_id、同内容重试幂等；同ID不同内容失败。JSON输入不要复用示例ID跨真实事件。无明确保存成功不宣称完成。
- `invalidate`载荷event_id／attempt_id／reason只撤销generic attempt，保留题目曝光；整卷／口语用assessment-void。更正不删除历史。
- 写入用同目录临时文件、fsync、原子替换、读回；Practice.md先检查管理块是否被改。管理块以外用户笔记保持原样。
- 若state已存而Markdown失败，返回saved=true、views_verified=false；先告知部分成功，用同event_id重试或 `rebuild`。不要新建event重复记录。
- state校验和异常、冲突副本、symlink、未知schema、被手改的管理块都会停写；保留文件，由用户确认如何合并。校验和是完整性检查，不是密码学身份认证。不能直接手改JSON绕过闸门。
- 本机flock只串行化**同一Mac**写入，不协调两台Mac／云同步。建议一次一台写入并等同步完成；脚本不承诺分布式事务。
- 注册表失效先确认Vault位置，用显式 `init --rebind`恢复；不可自动搜新目录、复制旧学生资料或删用户文件。删除档案、录音或旧安装副本均需另获授权。
