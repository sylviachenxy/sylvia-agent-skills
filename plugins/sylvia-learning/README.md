# Sylvia 学习辅导

Codex plugin：`sylvia-learning`。四个独立 skill 按需触发，不把不同考试的评分标准混用。

| Skill | 用途 |
| --- | --- |
| [deep-reading-coach](skills/deep-reading-coach/SKILL.md) | 精读、闭卷复述、论证分析与可持续阅读习惯。 |
| [cet4-english-coach](skills/cet4-english-coach/SKILL.md) | 四级笔试听读写译与口语优秀训练、证据诊断、迁移复测及持久学情；不伪造710换算。 |
| [ielts-speaking-coach](skills/ielts-speaking-coach/SKILL.md) | 以雅思口语 8+ 为目标的训练、模考与长期证据。 |
| [shanghai-gaokao-english-tutor](skills/shanghai-gaokao-english-tutor/SKILL.md) | 上海英语高考教学、分项基线与 140 分条件差距评估；教材、词表和评估目录随包分发。 |

## 安装与开始

需要具有 `plugin` 子命令的 Codex CLI：

```bash
codex plugin marketplace add sylviachenxy/sylvia-agent-skills --ref main
codex plugin add sylvia-learning@sylvia-agent-skills
codex plugin list --marketplace sylvia-agent-skills --json
```

已有同名 marketplace 时先核对其来源，不重复添加；从本地 clone 安装时将第一条命令的来源换成仓库根目录。添加目录不等于安装。安装后在新任务中输入：

> 用 $shanghai-gaokao-english-tutor 评估我离上海英语高考 140 分的差距。

也可在技能选择器中选中本 plugin 下的对应 skill。更新步骤、CLI 排障与旧独立安装迁移见[仓库安装说明](https://github.com/sylviachenxy/sylvia-agent-skills#安装)。

## 依赖与边界

- `0.2.0` BREAKING：原 `cet4-speaking-partner` 入口已退出源包，其能力并入 `cet4-english-coach`，不保留兼容入口；不会自动卸载旧副本、重写Goal或迁移个人数据。新档案可引用用户指定的旧回执，历史记录不冒充新冷测。
- 四级完整基线须使用当次验收的题卷、答案与匹配音频。包内有原创微题和公开来源目录，不含已验收整卷／音频题库或分数预测模型。
- 纯阅读／文字教学不要求 Apple 应用或账号；长期档案按各 skill 引导选择 Obsidian Vault，脚本需要 Python 3。
- 口语评价取决于当前宿主实际提供的音频证据，只有文字转写时不能声称已准确评估发音。
- SMART Goal、Apple 日程或周复盘联动可另装 `sylvia-productivity`；它是可选配套，不自动安装，也不影响独立学习训练。
- 安装不替用户建档、登录服务或授权个人数据；不保证分数或替代官方考试评分。
- 同一个 skill 不要同时保留旧独立安装和 plugin 安装；确认来源后由用户决定迁移，不自动删除旧副本。

## 许可与致谢

遵循随包 [LICENSE](LICENSE)，未新增开源授权。IELTS skill 保留了对 Kouyu 的[设计来源说明](skills/ielts-speaking-coach/references/provenance-and-validation.md)；其他资料的来源和限制随各 skill 的 references 一并交付。
