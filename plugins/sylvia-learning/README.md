# Sylvia 学习辅导

Codex plugin：`sylvia-learning`。五个独立 skill 按需触发，不把课堂笔记、阅读训练和不同考试的评分标准混用。

| Skill | 用途 |
| --- | --- |
| [class-notes](skills/class-notes/SKILL.md) | 单课／批量扫描待办，规范新旧笔记与课程目录，按 A／B 模板成稿及英语完整听写；素材归位与内容完成分开，各课 log.md 展示进度。教材覆盖、订正和课程分档独立配置。 |
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

整理课堂资料时可输入：

> 用 $class-notes 整理这节课，按适用档次使用固定模板；英语课另交完整原语言听写稿。

用户确认课堂根目录与两个子目录后，也可以说“整理今天所有课堂笔记”：主动寻找全部待办，结合本学期课表、创建／更新时间与内容智能归课，确定的素材安全移入“笔记素材”，按课次成稿到“正式课堂笔记”；不要求素材先分类或含课程名。归位不表示内容完成，未完成原因与下一步保留在各课素材目录的 log.md 中。整目录整理包含旧主笔记命名、错放文件和只有参考资料的课程。安全流程见[批量流程](skills/class-notes/references/batch-workflow.md)，证据见[归课规则](skills/class-notes/references/course-inference.md)。扫描、结构审计和日志辅助脚本需要 Python 3.11+；扫描／审计只读，日志默认预览，均不自动搬移文件。

也可在技能选择器中选中本 plugin 下的对应 skill。更新步骤、CLI 排障与旧独立安装迁移见[仓库安装说明](https://github.com/sylviachenxy/sylvia-agent-skills#安装)。

## 依赖与边界

- `0.3.0` 兼容新增 `class-notes`：包含[固定格式](skills/class-notes/references/note-formats.md)及五种 Markdown 模板，保留独立配置 schema 1。真实课程、音频、教材和笔记均不随包分发；不会自动分类课程、迁移资料或切换旧独立安装。
- 课堂录音依赖实际可用的音频能力；不会把 ASR 冒充逐字听核。A 档仅对已核验的内容范围承担完整性要求，不保证替代所有教材或覆盖全部考试；B 档同样保留必要条件、例外与证据。
- `0.2.0` BREAKING：原 `cet4-speaking-partner` 入口已退出源包，其能力并入 `cet4-english-coach`，不保留兼容入口；不会自动卸载旧副本、重写Goal或迁移个人数据。新档案可引用用户指定的旧回执，历史记录不冒充新冷测。
- 四级完整基线须使用当次验收的题卷、答案与匹配音频。包内有原创微题和公开来源目录，不含已验收整卷／音频题库或分数预测模型。
- 纯阅读／文字教学不要求 Apple 应用或账号；长期档案按各 skill 引导选择 Obsidian Vault，脚本需要 Python 3。
- 口语评价取决于当前宿主实际提供的音频证据，只有文字转写时不能声称已准确评估发音。
- SMART Goal、Apple 日程或周复盘联动可另装 `sylvia-productivity`；它是可选配套，不自动安装，也不影响独立学习训练。
- 安装不替用户建档、登录服务或授权个人数据；不保证分数或替代官方考试评分。
- 同一个 skill 不要同时保留旧独立安装和 plugin 安装；确认来源后由用户决定迁移，不自动删除旧副本。

## 许可与致谢

遵循随包 [LICENSE](LICENSE)，未新增开源授权。IELTS skill 保留了对 Kouyu 的[设计来源说明](skills/ielts-speaking-coach/references/provenance-and-validation.md)；其他资料的来源和限制随各 skill 的 references 一并交付。
