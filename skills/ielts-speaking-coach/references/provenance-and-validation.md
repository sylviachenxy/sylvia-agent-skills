# 设计来源与维护验证

## 致谢与设计来源

特别感谢 [maverickgao8848](https://github.com/maverickgao8848) 创作并公开分享 [口语练习台 Kouyu](https://github.com/maverickgao8848/kouyu)。它展示了如何把场景陪练、分级帮助、轻量纠错和训练后的本地复习连接起来，是本 skill 的重要设计启发。区分独立表达、提示后表达与模仿表达的做法，也帮助我们明确了训练记录应保留哪些信息。

本项目在这些思路启发下，加入了 IELTS Speaking 8+ 的专项训练、严格模考与练习的区分、间隔迁移复测，以及 Sylvia 的 Obsidian / Goal / 日程 / 周复盘接线。维护和分发本 skill 时应保留原作者、项目链接与这份具体贡献说明。

## 版本与实现范围

- 参考版本：[Kouyu · b36f02a](https://github.com/maverickgao8848/kouyu/tree/b36f02a8446588f2f1c677031e2cb4e64fed7dbc)，核对 2026-09-17。固定版本用于追溯当时参考的设计；原项目入口见上方链接。
- 该版本未附项目许可证。本 skill 的指令、脚本、题目和模板独立编写，没有复制其源码、前端、课程或完整文本；上游 clone 是开发者独立参考副本，不随本 skill 分发，也不是运行依赖。
- 雅思考试事实来源列在 `ielts-standard.md`，使用简要转述和链接，不随包分发完整官方评分 PDF 或商业题库。
- 接线沿用 Sylvia 仓库的既有原则：Obsidian 保存主记录、稳定 ID 与相对引用、Goal / scheduler 分权、真实证据和提示程度分开、保留用户内容、写入后回读。

## 维护验证

从仓库根运行：

```bash
python3 -m unittest discover -s skills/ielts-speaking-coach/tests -v
python3 "/path/to/skill-creator/scripts/quick_validate.py" skills/ielts-speaking-coach
gh skill publish --dry-run
skills-ref validate skills/ielts-speaking-coach
```

skills-ref 不在 PATH 时，使用 Agent Skills 项目当前官方文档的一次性调用。quick_validate 位置按实际 skill-creator 安装解析，不能假设它是运行时依赖。

测试使用独立临时 Vault 和 registry，不接触真实个人资料、麦克风、Apple 应用或账号。验证重点是跨进程恢复、偏好版本、重复写入、更正不重复计数、用户笔记保留、部分失败恢复，以及文本 / 受提示 / 已见题不能升级成严格语音证据。

2026-09-17 本地验证：35 项自动化测试通过，含公开 CLI 的 init → configure → archive → context 端到端流程；quick_validate、官方 skills-ref 和仓库 gh dry-run 均通过。skills-ref 使用 `uv tool run --from 'git+https://github.com/agentskills/agentskills.git#subdirectory=skills-ref' skills-ref validate skills/ielts-speaking-coach` 一次性运行。没有建立真实用户档案、录音或创建 Apple 对象。

行为走查同时检查：

1. “目标8分，今天5分钟，没有基线” → 短冷样本，不强塞整套考试或许诺提分。
2. “做完整模考，先给我本题范句” → 说明提供范句将改变证据条件；用户要帮助则切训练，保留真实标签。
3. “Voice 能听到我，但你只有转写” → FC/P 不评分，可继续文字训练。
4. “照着你的句子说得很流利，算8分了吗” → 保留重做价值，安排新题，不提升基线。
5. “继续上次” → 从 context 恢复到期复测与偏好，不再问整套入组问题。
6. “改成20分钟，以后用英文反馈” → configure 后读回；不依赖本轮 context。
7. “把 Goal 行动加到日历” → 路由 goal-planner，不通过 scheduler 另建对象。
8. “把同一条回执写进周报” → 按 session ID 去重，区分训练和进步。
9. “忘了录音/计时中断/题目以前见过” → 相应降级，保留实际完成的训练。
10. “档案已存但复习台坏了” → 同一 ID / rebuild 恢复，保留用户补充。

以上行为走查不能替代真人教学效果验证。first cut 的实机语音体验、Sylvia 自身基线、连续数周使用效果以及 AI 与真人评分一致性，都须用真实练习另行检验；不能把本地软件检查通过表述成已证明能快速达到 8+。
