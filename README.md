# Sylvia Agent Skills

Sylvia 的个人 Agent Skills 仓库，主要适配 Codex。按领域组织为两个可单独安装的 plugin，也保留 [Agent Skills 开放规范](https://agentskills.io/specification) 的独立 skill 安装方式。

## Codex Plugins

Marketplace：`sylvia-agent-skills`。添加 marketplace 只是添加目录，不会自动安装任何 plugin。

| Plugin | 包含的 skills |
| --- | --- |
| [sylvia-learning · 学习辅导](plugins/sylvia-learning/README.md) | 课堂笔记与英语听写、深度阅读、CET-4 综合教练、雅思口语、上海高考英语。 |
| [sylvia-productivity · 个人效率](plugins/sylvia-productivity/README.md) | 目标规划、个人日程、每周复盘、晨间简报。 |

两个 plugin 可分别使用。学习训练需要目标／日程／周复盘联动时可再安装个人效率；不自动安装依赖或连接账号。

## Skills

| Skill | 功能 |
| --- | --- |
| [`class-notes`](plugins/sylvia-learning/skills/class-notes/) | 按 A／B 模板整理单课或批量待办，英语录音另交完整原语言听写；规范新旧笔记与课程目录，用 log.md 区分内容完成和素材归位，支持教材增补与订正，目录和课程分档独立配置。 |
| [`deep-reading-coach`](plugins/sylvia-learning/skills/deep-reading-coach/) | 培养可迁移的精读能力、独立阅读流程与可持续阅读习惯。 |
| [`cet4-english-coach`](plugins/sylvia-learning/skills/cet4-english-coach/) | CET-4 笔试与 CET-SET4 口语综合教学，基线诊断、分项证据、高分目标分析、迁移复测与 Obsidian 学情；不将正确率转换为710报道分。含来源／材料验收目录，不附网上整卷或音频。 |
| [`ielts-speaking-coach`](plugins/sylvia-learning/skills/ielts-speaking-coach/) | 以雅思口语 8+ 为目标，进行日常专项训练、无提示模考、间隔迁移复测，在 Obsidian 持久管理偏好与证据，并衔接目标、日程和周复盘。 |
| [`shanghai-gaokao-english-tutor`](plugins/sylvia-learning/skills/shanghai-gaokao-english-tutor/) | 上海英语一对一教学、115＋25＋10分项评估、140分条件差距分析与迁移复测，Obsidian持久保存学情；随附完整教材、3,319行词表、国家基线及22份题卷的分项验收目录（不附网上整卷或听说音频）。 |
| [`goal-planner`](plugins/sylvia-productivity/skills/goal-planner/) | 把模糊意图建立成有证据的 SMART Goal，以 Obsidian 保存完整记录，并通过 EventKit 投影到 Apple Reminders 与 Apple Calendar、由 iCloud 同步、持续 check-in。 |
| [`personal-scheduler`](plugins/sylvia-productivity/skills/personal-scheduler/) | 使用随附的 macOS EventKit executor，在 Apple Reminders 与 Apple Calendar 中捕捉、查看、编排和调整个人学习与生活日程。 |
| [`weekly-review`](plugins/sylvia-productivity/skills/weekly-review/) | 从 Obsidian、Apple 原生工具和用户批准的 Mac 工作痕迹中提炼证据，经用户确认后形成个人周复盘并写入 Obsidian。 |
| [`morning-brief`](plugins/sylvia-productivity/skills/morning-brief/) | 在 Mac 持久管理偏好并于起床前生成晨间简报，经 Apple Notes 分发；手机绑定一次后无需随偏好重配，附首次 setup、只读采集与手机/排程验证。 |

## 安装

### 方式一：Codex marketplace（推荐）

先确认 `codex --version` 与 `codex plugin --help` 均可运行，再添加仓库来源并安装：

```bash
codex plugin marketplace add sylviachenxy/sylvia-agent-skills --ref main
# 按需选择一个或两个 plugin；不是必须全部安装。
codex plugin add sylvia-learning@sylvia-agent-skills
codex plugin add sylvia-productivity@sylvia-agent-skills
codex plugin list --marketplace sylvia-agent-skills --json
```

从本地 clone 试用尚未推送的版本，在仓库根目录将第一条换成 `codex plugin marketplace add .`。已经添加同名 marketplace 时，先运行 `codex plugin marketplace list` 确认来源；本地与远端不要用同名来源混装。切换来源前由用户确认，不直接覆盖原配置。

支持自定义来源的 Codex 桌面插件面板中，也可从 `Sylvia Agent Skills` 来源选择安装。安装后读回确认目标 plugin 的 `installed`、`enabled` 均为 `true`，再新建任务，在技能选择器中选择对应 skill，或输入 `用 $shanghai-gaokao-english-tutor 评估我的基线`。

PATH 中的旧 npm wrapper 即使能被 `command -v codex` 找到，也可能无法执行。Mac 可检查 `/Applications/ChatGPT.app/Contents/Resources/codex`、`/Applications/Codex.app/Contents/Resources/codex` 或用户 Applications 中对应路径；仅在实际文件可执行且通过版本／能力检查后，以该绝对路径替换示例中的 `codex`。不要求为此重装全局 Node.js。

更新 Git 来源的 marketplace 后重新安装所需 plugin，并在新任务中验证：

```bash
codex plugin marketplace upgrade sylvia-agent-skills
codex plugin add sylvia-learning@sylvia-agent-skills
codex plugin add sylvia-productivity@sylvia-agent-skills
codex plugin list --marketplace sylvia-agent-skills --json
```

这里只更新所选安装包，不迁移或覆盖 Obsidian 学情、Goal、个人偏好、Apple 数据或快捷指令。检查读回版本是否符合预期；没有变化时检查来源和发布版本，不手工删除缓存。安装成功也不代表首次 setup、账号授权、音频链路或手机同步已验证。

### 方式二：只安装独立 skill

需要支持 `gh skill` 的 GitHub CLI。本仓库采用其支持的 `plugins/<plugin>/skills/<skill>` 发现结构，以下按名称安装命令保持不变：

已知限制：本地验收中 `gh 2.98.0 --from-local` 会保留文件内容，但丢失部分脚本执行权限；未据此断言远端安装的权限行为相同。涉及 Apple 自动化的 skills 推荐使用上面的 Codex marketplace 安装。独立安装后如果遇到 `Permission denied`，先确认安装来源和报错脚本，再恢复该安装目录内相应入口及其调用脚本的执行位，不能把文件下载成功当作自动化已可运行。

```bash
gh skill install sylviachenxy/sylvia-agent-skills deep-reading-coach \
  --agent codex \
  --scope user
```

例如安装课堂笔记 skill（课程名单与分档可暂不配置，真实资料保存在包外）：

```bash
gh skill install sylviachenxy/sylvia-agent-skills class-notes \
  --agent codex \
  --scope user
```

固定格式与模板见 [class-notes](plugins/sylvia-learning/skills/class-notes/SKILL.md)。本地未发布候选须通过已核实的本地来源验收；不要把远端安装命令当成未推送内容已经可用。

例如安装四级综合教练（含原口语伙伴能力）：

```bash
gh skill install sylviachenxy/sylvia-agent-skills cet4-english-coach \
  --agent codex \
  --scope user
```

`sylvia-learning 0.2.0` 为 BREAKING：旧 `cet4-speaking-partner` 入口直接移除，无转发别名。更新发布源不会自动删除旧独立安装或个人回执；由用户确认迁移，见[退役说明](docs/lifecycle/skill-cet4-speaking-partner.md)。

例如安装雅思口语教练（首次使用由 Codex 引导选择 Vault 并建立档案）：

```bash
gh skill install sylviachenxy/sylvia-agent-skills ielts-speaking-coach \
  --agent codex \
  --scope user
```

例如安装上海高考英语教练（完整校对教材与词表随 skill 安装；持续学情另由 Codex 引导保存到个人 Vault）：

```bash
gh skill install sylviachenxy/sylvia-agent-skills shanghai-gaokao-english-tutor \
  --agent codex \
  --scope user
```

例如安装目标规划 skill：

```bash
gh skill install sylviachenxy/sylvia-agent-skills goal-planner \
  --agent codex \
  --scope user
```

例如安装个人日程 skill：

```bash
gh skill install sylviachenxy/sylvia-agent-skills personal-scheduler \
  --agent codex \
  --scope user
```

例如安装个人周复盘 skill：

```bash
gh skill install sylviachenxy/sylvia-agent-skills weekly-review \
  --agent codex \
  --scope user
```

例如安装晨间简报 skill（安装后先由 Codex 引导完成个人配置与设备验证，不会自动连接账号或建立定时任务）：

```bash
gh skill install sylviachenxy/sylvia-agent-skills morning-brief \
  --agent codex \
  --scope user
```

安装仓库中的全部 skills：

```bash
gh skill install sylviachenxy/sylvia-agent-skills --all \
  --agent codex \
  --scope user
```

### 从旧独立安装迁移

先用 `gh skill list --agent codex --scope user` 和 `codex plugin list --json` 核对来源。**同一 skill 选择一种安装方式**，避免旧独立副本与 plugin 副本并存造成重复或调用旧版本。

安装包更新不会修改个人 Vault／配置。确认新 plugin 完整可用后，由用户选择备份并移出旧的独立安装目录；不要自动删除，也不要把备份留在仍会被扫描的 skills 目录里。旧 `gh skill` 元数据如果记录了迁移前的 `skills/<skill>` 路径，应先备份本地改动，再按名称重新安装以刷新来源路径，不能保证旧路径的原地 update 能跨目录迁移。

## 仓库结构

```text
.agents/plugins/marketplace.json
plugins/
├── sylvia-learning/
│   ├── .codex-plugin/plugin.json
│   ├── README.md
│   ├── LICENSE
│   └── skills/<skill-name>/
│       ├── SKILL.md
│       ├── agents/
│       ├── scripts/
│       ├── references/
│       └── assets/
└── sylvia-productivity/    # 同样结构
scripts/                    # 仓库分发校验与隔离安装验收
tests/                      # 分发层回归测试
docs/plugin-development.md
docs/maintenance-policy.md
CHANGELOG.md
```

每个 skill 只有一份实体内容，目录名与 `SKILL.md` 的 `name` 一致。教材、词表、脚本、模板和来源说明随所属 skill 安装；开发用 `research/`、OCR 底稿、私人笔记和个人配置不发布。不使用指向包外内容的软链接，也不以空壳安装成功代替内容验收。

## 验证

在仓库根目录运行：

```bash
python3 scripts/validate_marketplace.py
python3 -m unittest discover -s tests -v
gh skill publish --dry-run
# 指向经过 --version / plugin --help 验证的实际 CLI；全程使用临时安装环境。
python3 scripts/smoke_marketplace.py --codex /absolute/path/to/codex
```

静态校验、真实安装、运行时发现、技能行为和发布是不同证据；完整说明、版本规则与发布门槛见[维护文档](docs/plugin-development.md)。对后续尚未推送的本地候选，不能将本地 smoke 通过表述为 GitHub 安装已可用。

## 维护与版本

新建、编辑、弃用、归档、删除、恢复或迁移组件前，阅读[Skill / Plugin 生命周期与版本规范](docs/maintenance-policy.md)。归档退出当前默认分发但保留固定 Git SHA 的恢复依据；删除不等于清除历史或用户设备上的副本，两者都不能自动删除用户资料。

每个 plugin 独立采用 [SemVer 2.0.0](https://semver.org/spec/v2.0.0.html)，以 manifest 为唯一版本 authority。当前 `0.x` 阶段的明确规则、稳定期 major／minor／patch 判定、预发布、main 分发与迁移要求见已确认的维护规范。只有仓库级文档变动且不影响安装契约时不升包版本；已分发版本不以同一版本号替换内容。

## 致谢

`ielts-speaking-coach` 的设计受到 [maverickgao8848](https://github.com/maverickgao8848) 的 [口语练习台 Kouyu](https://github.com/maverickgao8848/kouyu) 的重要启发。感谢原作者公开分享分级提示、轻量纠错、独立表现记录与本地复习台的实践。我们据此获得启发，独立实现了雅思专项训练与 Sylvia 工作流适配；具体贡献和参考版本见 [设计来源](plugins/sylvia-learning/skills/ielts-speaking-coach/references/provenance-and-validation.md)。

## 许可

见 [`LICENSE`](LICENSE)。本仓库目前保留全部权利，未授予开源许可。
