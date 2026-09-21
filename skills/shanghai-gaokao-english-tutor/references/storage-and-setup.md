# 首次设置与持久化

## 第一次由 Codex 引导

持续训练前说明会保存偏好、必要回答／观察、错因和复测；这些文字跟随 Vault 原有同步。原始音频默认不复制、保存或上传。询问是否保留原话（store_quotes），一次明确的持续保存授权覆盖以后同范围归档，不每次重问。

1. 定位 `<skill-root>`，检查 Python 3.10+。先运行 `python3 "<skill-root>/scripts/curriculum_lookup.py"` 验证随附完整教材与词表；不需要原开发仓库、原始 PDF、网络或 corpus_path。仅用标准库；无需安装 App 或付费服务。缺件／哈希不符时先报告资料未就绪，不宣称 setup 完成。
2. 运行 profiles。唯一已绑定档案可继续；多个学习者要选择，不能按 Mac 账户猜是谁。
3. 复用用户指明的 Vault；否则只检查当前目录的 `.obsidian` 祖先或 Obsidian 已登记 vault 配置。多个候选询问用户，不全盘搜索、不新建 Vault、不猜云盘路径。也可明确选择既有 Markdown 文件夹。
4. 确定稳定 profile ID、时区和必要偏好；年级、目标届别／场次、分数、时间预算可未知或逐步确认。不得把测试数据或开发者本人当成 Sylvia 配置。
5. init 后 show 和 context 回读，打开 Practice.md。默认 corpus_path=null 表示自动使用 skill 随附资料，不要求学生找目录或设置安装路径。所有个人更改由 Mac 端持久化，手机不用重配。
6. 进行一小段真实训练，prepare → present → record → context。另一个对话重新读取能恢复才叫跨 context 闭环；开发合成测试不是学习者真实验收。
7. 若要听说，另验真实客户端的音频输入、播放和必要计时。没有就做文字降级，不阻塞词汇／阅读／写作训练。

## 位置和事实源

```text
Mac 本机：Library/Application Support/shanghai-gaokao-english-tutor/profiles.json
<Vault>/Learning/Shanghai-Gaokao-English/<profile-id>/
├── state.json       # 唯一机器事实源：偏好历史、冻结题目、曝光、作答与更正记录
├── Practice.md      # 可重建练习台；末尾“我的笔记”保留用户内容
└── .lock            # 同机写入锁
```

registry 只负责定位；不进仓库。随附资料按安装位置定位，不在个人配置复制教材或安装路径；只有明确选择外部副本时才保存 corpus_path。其他 skill 交接使用 Vault-relative path。JSON 含教师答案，只给教练按需读取，不把整个 JSON 当学生练习页打开；本地文件不是防作弊保险箱。

state.json 使用内容校验、原子写、读回验证及同机文件锁；不可手改。Practice.md 受管区附内容摘要，发现用户改动或标记缺失会停写，保留文件等待对账；受管区外笔记不覆盖、不反推成成绩。不要通过重建哈希“解决”内容冲突。

first cut 仅支持一个 Mac 写同一档案；同步锁不是分布式锁。出现 conflict 副本、损坏 JSON、同名异内容或多机并发时停止相关写入，先保留各副本并对账。回读成功只证明本机保存，不证明 Google Drive 已同步。

## 命令

全局参数在子命令前。`--registry` 仅用于测试或用户明确选择本机定位文件；普通使用默认即可。显式选择的目录别名（如 macOS 的 `/tmp`）会规范化到真实路径；registry 文件和受管档案内部的符号链接仍拒绝。命令中的占位符由 Codex 替换，不让学生维护 JSON。

```bash
python3 "<skill-root>/scripts/tutor_store.py" profiles
python3 "<skill-root>/scripts/tutor_store.py" --profile learner init --vault "/selected/Vault" --input "/private/settings.json"
python3 "<skill-root>/scripts/tutor_store.py" --profile learner show
python3 "<skill-root>/scripts/tutor_store.py" --profile learner configure --input "/private/change.json"
python3 "<skill-root>/scripts/tutor_store.py" --profile learner prepare --input "/private/item.json"
python3 "<skill-root>/scripts/tutor_store.py" --profile learner present --input "/private/presentation.json"
python3 "<skill-root>/scripts/tutor_store.py" --profile learner record --input "/private/attempt.json"
python3 "<skill-root>/scripts/tutor_store.py" --profile learner context
python3 "<skill-root>/scripts/tutor_store.py" --profile learner context --on 2026-09-20 --focus "sh-verb-tense:past-perfect"
python3 "<skill-root>/scripts/tutor_store.py" --profile learner rebuild
```

私有输入文件用 agent 文件编辑工具建立在获准个人档案目录或私有临时目录，别放 skill 源码、公共 output 或 Git 中。临时文件也可能含答案／原话；无需保留时按授权安全处理。源扫描始终只读。

所有 mutation 用唯一 event_id（建议 UUID，允许字母数字、短横线、下划线）。重试必须复用相同 ID 和内容；同 ID 异内容拒绝。prepare 的 item_id 也不可覆写。用户不需要知道这些 ID。

### 偏好

init 的 input 是部分偏好对象，无 input 则用默认值。支持字段：exam_year（null／年份）、exam_session（unknown/january/june/both）、grade、target_score（null 或整数 0–150）、timezone（IANA）、daily_minutes、feedback_language、store_quotes、goal_id、corpus_path、baseline_note。未知分数不是 0。

corpus_path 默认 null，升级或移动 skill 后自动跟随新的安装位置。已有非 null 值是显式外部覆盖：查询时传给 curriculum_lookup 的 --corpus，不可静默忽略。用户同意恢复内置版时，正常 configure `{"corpus_path":null}`；保留设置历史。外部目录损坏时不自动回退，以免不知情地更换依据。

baseline_note 只放简短用户报告／核验摘要及详细材料的本地定位；历史成绩和已有作答不伪造为本脚本的未见题冷测。必要的详细诊断 Markdown 在同一获准个人目录另存，由教练维护，不写回公共 skill。

自然语言“以后每天只练 20 分钟，不存原话” → 先 show，使用当前 revision：

```json
{"event_id":"CFG-unique","expected_revision":5,"patch":{"daily_minutes":20,"store_quotes":false}}
```

revision 变化表示有其他写入，先重新读取再合并实际用户要求，不盲目重放。未传字段不变；settings_history 保存旧设置。恢复旧设置也通过新 configure 形成新版本，不倒退版本号。

store_quotes 关闭后，新 record 必须 evidence_kind=observation；evidence、reason、errors、next_step、transfer_note 都不得夹带原话。旧记录不自动删除；用户要求清除历史时先定位真实档案和同步副本，另行处理。系统不声称能从任意自由文本自动识别隐私。

goal_id 只有核对了 Goal 文档才绑定；脚本检查文件存在和 action ID 前缀，不替代读取正式合同与行动归属。改训练偏好不会自动改 Goal 或 Apple 投影。

Vault 移动后：使用相同 profile 的 init --vault 新位置 --rebind；目标须已有相应档案，不复制数据或重置偏好。新 Mac 指向已同步的相同档案可普通 init 绑定。旧机停止写入后再迁移。

## 题目与曝光契约

prepare 输入：`{"event_id":"PREP-unique","item":{...}}`。item 采用 assets/starter-items.json 中的单个对象，必要字段如下：

- item_id；domain（vocabulary/grammar/reading/cloze/summary/translation/writing/listening/speaking）。
- focus：稳定且窄的能力 ID；demand=understand/produce，不能一项掌握覆盖另一项。
- stem：完整学生可见题干；key：教师侧答案／可接受替代；rubric：1–8 条判断条件。
- source：kind=original/user_material/official_verified；locator=URL、原书页、卷别题号或原创版本；answer_status=tutor_checked/official_key_verified。
- 可选 time_limit_seconds：本题训练限时，不自动等于官方限时。

prepare 只返回 ID，不提前显示 key。出题后独立解题检验，再设置 tutor_checked；脚本只校验字段，不能证明题目自然、多解性或答案正确。

present 输入：

```json
{"event_id":"P-unique","item_id":"ITEM-unique","prior_exposure":false}
```

返回 presentation_id、stem、unseen；只展示 stem。prior_exposure 是教师核实过的外部曝光，未知用 true。present 的实际时刻和设置快照被保存。重复曝光即使换 item ID 也受文字指纹约束；语义近似题还需教师主动标记。

## 作答契约

record 输入示例是**合成格式**，不要导入真实学习者：

```json
{
  "event_id":"A-unique","presentation_id":"P-unique","session_id":"SESSION-unique",
  "occurred_at":"2026-09-20T19:10:00+08:00",
  "support":"none","result":"pass","modality":"text","audio_observed":false,
  "evidence_kind":"observation","evidence":"学生给出了正确形式并说明先后参照。",
  "reason":"满足本题的形式和时间关系两条标准。","errors":[],
  "next_step":"换一段叙事，检查时间参照能否独立迁移。",
  "new_context":false,"transfer_note":"","timer_verified":false,"elapsed_seconds":null
}
```

occurred_at 是这次真实回答时间，须带 offset、晚于实际展示且不在未来。session_id 在同一次训练的多题之间保持一致。support 取本次实际最高帮助等级；result=pass/partial/fail/unassessed。partial 不是通过。未评分可保存原始表现，但不能提升状态。

modality=text/transcript/audio；audio_observed 只有实际获得声音证据才 true。仅转录文本不能提升听说独立状态。evidence_kind=quote/observation，保留证据与 reason 分开；模型自己写的答案不能放 evidence 冒充学生。

new_context=true 时 transfer_note 说明真正的变化；timer_verified=true 须有测得的正数秒值。不计时则 null，不填 0。脚本独立计算 qualifies_independent、independent_pass 和 within_verified_limit，输入不能自带这些结论。qualifies_independent 只表示未见、无提示等作答条件，不表示答对；independent_pass 同时要求 result=pass。即使 partial 的作答条件独立，能力状态仍回到 needs_work。下游不得把条件字段单独当作成就或掌握。

已有 Goal 行动可附 action_id，须属于展示时冻结的 goal_id；动作归属由教练读正式 Goal 核验。没有就省略，不造号。脚本不自动完成行动。

## 更正、恢复与边界

错录／错误判定：用 invalidate，输入 event_id、attempt_id、reason；原证据留存但不计有效进展，后续状态重新计算。不能删除曝光、把同题变回未见；必要时用新题重新核实能力。撤销不构成新的训练成果。

state 已保存但练习台写入失败时返回 saved=true、views_verified=false；修复具体问题后 rebuild 或重放原 event，不生成另一次作答。context 只读有效记录，返回最近八条、所有能力状态、到期建议与曝光信息；查询旧日期时按设置时区筛选作答。

本脚本不是官方阅卷器，不识别语义近似题、不验证教练是否真的听到音频、不决定题目与 rubric 的学术正确性。它的价值是保证记录、幂等、条件约束和跨对话恢复，不是让不可靠判断变成可靠成绩。
