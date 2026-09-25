# cet4-speaking-partner 退役记录

- kind：skill
- name：cet4-speaking-partner
- 原 plugin／路径：sylvia-learning / `plugins/sylvia-learning/skills/cet4-speaking-partner/`
- status：deleted（当前工作树候选；远端生效待发布）
- 生效日期：本地候选2026-09-25；远端pending
- 理由：口语并入完整四级综合教练，避免平行档案和两个竞争入口。
- 批准：维护者明确要求旧skill直接失效、不考虑兼容，并于2026-09-25要求构建综合教练。因此按授权跳过通常建议的公开deprecated版本，不留alias／wrapper。
- 最后发布版本：sylvia-learning 0.1.0。
- 最后完整 Git SHA：`f3fd86be2d44de10087366de9b96cefcf3775f86`。
- [固定历史源码](https://github.com/sylviachenxy/sylvia-agent-skills/tree/f3fd86be2d44de10087366de9b96cefcf3775f86/plugins/sylvia-learning/skills/cet4-speaking-partner)。正常删除不改写Git历史。
- 替代项：`cet4-english-coach`，同属sylvia-learning；口语标准、会话协议、rubric和回执模板迁入新skill，并补综合评估／持久化规则。
- 候选版本：sylvia-learning 0.2.0，BREAKING；个人效率包保持0.1.0。
- 消费者：根README、学习包README、manifest描述／默认提示、显式安装命令和分发测试；新发现集合仍为8个skill，不含旧入口。
- 用户迁移：更新包／安装新skill须另获授权。旧独立副本仍可能在用户环境；确认来源和个人修改后再决定卸载，不自动删缓存。新档案只引用用户提供的旧回执，不篡改旧source_skill或将旧自报升为现场证据。
- 数据边界：Obsidian档案、Goal、已有回执、录音、Apple数据均未迁移／删除。旧skill原本没有统一持久存储脚本，不伪造自动schema迁移。
- 恢复条件：维护者重新授权并检查当前资料、入口和版本；本次不保留可执行archive副本。
- 撤出提交：pending；未commit／push，不伪造未来SHA。
- CHANGELOG：2026-09-25未发布候选“四级综合教练”。验证证据随该条目与新skill的验证记录更新。
