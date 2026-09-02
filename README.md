# Sylvia Agent Skills

Sylvia 的个人 Agent Skills 仓库。仓库遵循 [Agent Skills 开放规范](https://agentskills.io/specification)，每个 skill 都是一个可独立安装、按需加载的目录。

## Skills

| Skill | 功能 |
| --- | --- |
| [`deep-reading-coach`](skills/deep-reading-coach/) | 培养可迁移的精读能力、独立阅读流程与可持续阅读习惯。 |
| [`goal-planner`](skills/goal-planner/) | 把模糊意图建立成有证据的 SMART Goal，以 Obsidian 保存完整记录，并通过 EventKit 投影到 Apple Reminders 与 Apple Calendar、由 iCloud 同步、持续 check-in。 |
| [`personal-scheduler`](skills/personal-scheduler/) | 使用随附的 macOS EventKit executor，在 Apple Reminders 与 Apple Calendar 中捕捉、查看、编排和调整个人学习与生活日程。 |
| [`weekly-review`](skills/weekly-review/) | 从 Obsidian、Apple 原生工具和用户批准的 Mac 工作痕迹中提炼证据，经用户确认后形成个人周复盘并写入 Obsidian。 |
| [`morning-brief`](skills/morning-brief/) | 在 Mac 持久管理偏好并于起床前生成晨间简报，经 Apple Notes 分发；手机绑定一次后无需随偏好重配，附首次 setup、只读采集与手机/排程验证。 |

## 安装

使用 GitHub CLI 安装到 Codex 的用户级 skill 目录：

```bash
gh skill install sylviachenxy/sylvia-agent-skills deep-reading-coach \
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

## 仓库结构

```text
skills/
└── <skill-name>/
    ├── SKILL.md          # 必需：触发元数据与操作指令
    ├── agents/          # 可选：产品界面元数据
    ├── scripts/         # 可选：可执行脚本
    ├── references/      # 可选：按需读取的参考资料
    └── assets/          # 可选：模板与静态资源
```

每个目录名必须与其 `SKILL.md` 的 `name` 完全一致，并使用小写字母、数字和连字符。

## 验证

在仓库根目录运行：

```bash
gh skill publish --dry-run
```

该命令会发现 `skills/*/SKILL.md`，并依据 Agent Skills 规范检查名称、目录匹配和必需的 frontmatter 字段。

## 许可

见 [`LICENSE`](LICENSE)。本仓库目前保留全部权利，未授予开源许可。
