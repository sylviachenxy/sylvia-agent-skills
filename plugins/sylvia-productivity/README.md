# Sylvia 个人效率

Codex plugin：`sylvia-productivity`。四个 skill 分别负责目标、日程、复盘与简报，保持各自的数据和写入授权边界。

| Skill | 用途 |
| --- | --- |
| [goal-planner](skills/goal-planner/SKILL.md) | 经证据和用户确认建立 SMART Goal，保存到 Obsidian，并投影到 Apple Reminders／Calendar。 |
| [personal-scheduler](skills/personal-scheduler/SKILL.md) | 查看与编排已确定的个人日程，不越权修改 Goal 管理的对象。 |
| [weekly-review](skills/weekly-review/SKILL.md) | 从用户批准的活动范围提炼成果，经确认写入 Obsidian。 |
| [morning-brief](skills/morning-brief/SKILL.md) | 在 Mac 持久配置并生成晨间简报，经 Apple Notes 和手机快捷指令分发。 |

## 安装与开始

需要具有 `plugin` 子命令的 Codex CLI：

```bash
codex plugin marketplace add sylviachenxy/sylvia-agent-skills --ref main
codex plugin add sylvia-productivity@sylvia-agent-skills
codex plugin list --marketplace sylvia-agent-skills --json
```

已有同名 marketplace 时先核对其来源，不重复添加；从本地 clone 安装时将第一条命令的来源换成仓库根目录。添加目录不等于安装。安装后在新任务中输入：

> 用 $goal-planner 帮我明确这个学期的目标，先检查基线和可行性。

也可在技能选择器中选中本 plugin 下的对应 skill。更新步骤、CLI 排障与旧独立安装迁移见[仓库安装说明](https://github.com/sylviachenxy/sylvia-agent-skills#安装)。

## 环境与权限

- 主要执行环境是 Mac；EventKit、Apple Notes、Reminders、Calendar 适配器需要 macOS 及各 skill setup 所述的系统权限和开发工具。
- Obsidian Vault、个人配置、账号和设备绑定均在首次使用时由用户选择；不会写进安装目录或 Git 仓库。
- iCloud 负责用户已配置的跨设备同步；手机通知和晨间快捷指令仍需首次设置与真机验证，安装 plugin 不等于设备链路已完成。
- 不捆绑 Gmail 等账号连接器，不自动读取邮件或扫描整个 Mac；仅使用当前实际可用且用户批准的来源。
- 不附自动执行的 hooks，不在安装时写入日历／提醒事项、发布 Notes 或建立定时任务。
- 学习专项可另装 `sylvia-learning`，不是本 plugin 的硬依赖；同名独立 skill 与 plugin skill 的迁移由用户确认，不自动删除旧副本。

## 许可

遵循随包 [LICENSE](LICENSE)，未新增开源授权。
