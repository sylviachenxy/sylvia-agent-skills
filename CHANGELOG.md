# 变更记录

## 2026-09-24 — 首次 marketplace 分发

### 生命周期与 SemVer 规范

- 新增 `docs/maintenance-policy.md`：明确新建、编辑、弃用、归档、删除、恢复、改名及跨 plugin 移动的授权、入口同步、追溯与迁移要求。
- 定义 plugin 的公开契约、0.x／稳定期升版矩阵、预发布、已发布版本不可变性、可安装 main 的发布边界和回滚规则。
- 同步根 AGENTS、README 和分发文档；区分自动检查与需人工判断的兼容性／批准事项。维护者已确认新增约定并授权通过 PR 合并到 main。
- 规范部分仅修改仓库级治理文档，不独立改变安装包版本；两个首次分发版本均为 `0.1.0`。未执行任何组件退役或用户安装迁移。

### 首次 Codex marketplace 分发

- 新增 `sylvia-agent-skills` marketplace，以及 `sylvia-learning`、`sylvia-productivity` 两个初始版本为 `0.1.0` 的领域 plugin。
- 八个既有 skill 从 `skills/` 原样迁入 `plugins/<plugin>/skills/`，教材、脚本、模板和来源说明全部随包；修订两份维护文档中的仓库命令路径。
- 保留 `gh skill` 按名称安装入口；补充 marketplace 安装／更新、旧独立副本迁移、版本规则和安装验收。
- 使用实体目录，避免本轮 Codex CLI 在安装时跳过目录软链接；增加防回归验证与干净安装 smoke test。
- **BREAKING（仓库路径）**：公开路径由 `skills/<skill>` 改为 `plugins/<plugin>/skills/<skill>`。旧独立安装的来源路径可能无法原地 update，须先备份本地修改再按名称重新安装，或按 README 迁移到 plugin；不会自动卸载旧副本或修改个人资料。两个 plugin 是首次引入，故均从 `0.1.0` 开始，而不是复用旧 plugin 的已发布版本。
- 本地验收：24 项分发回归、安装包内 490 项 unittest 与两组独立离线断言通过；两个原生 plugin 的文件集合、SHA-256、执行位和 8 个 skill 的运行时发现通过。`gh --from-local` 的执行位丢失限制见 README；未将其内容检查等同于运行通过。
- 不修改教学／个人工作流、用户配置或许可证；不创建 tag 或 GitHub Release。远端合并及 GitHub 来源安装证据见对应 PR，不以本地检查代替远端验收。
