# 变更记录

## 2026-09-25 — 四级综合教练（sylvia-learning 0.2.0）

- `sylvia-learning 0.1.0 → 0.2.0`，**BREAKING**：新建`cet4-english-coach`，按维护者明确要求直接移除`cet4-speaking-partner`活动入口，不保留兼容别名；口语协议、rubric与回执并入综合教练。0.x不兼容能力移除按minor升版，个人效率包仍为0.1.0。
- 增加CET4听力、三类阅读、写作、翻译、词句专项；官方710报道分与原始练习、口试等级分离。支持材料准入、现场／历史基线、分项假设、可追溯更正、分级帮助、独立迁移和跨日复测，不提供伪分数预测。
- 增加统一Obsidian配置／学情、本机profile定位、不可变题目与曝光、原子读回和可读Practice.md；口语能力索引保留语音／计时／搭档限制，并可用`assessment-show`读回。脚本不访问账号、录音、Goal写入或Apple工具。
- 随附6项原创微题、官方依据／评分锚点定位、4份近年题卷的结构检查目录与缺口。**完整笔试冷测材料验收数仍为0**：网上题卷的完整答案与配套音频尚未逐项验收，不把下载数当可用基线数；原始PDF／OCR／渲染件仍在本地ignored research，不构成安装依赖。
- 同步根／学习包README、manifest默认入口和分发预期集合；退役依据、固定历史SHA、用户数据边界见`docs/lifecycle/skill-cet4-speaking-partner.md`。原回执不被篡改或自动升级为新教练亲历证据；已安装旧副本不会被自动卸载。
- 本地验证：95项四级skill离线测试、24项分发回归通过；两个独立行为前测覆盖真实请求和临时Vault CLI流程。隔离原生安装验证两个包和8个技能的真实发现／完整文件内容；全包回归及最终工具版本见新skill的验证记录。
- 本条随经维护者授权的[PR #2](https://github.com/sylviachenxy/sylvia-agent-skills/pull/2)合入main生效；合并SHA和合并后的GitHub来源隔离安装验收见该PR记录。未安装到正式profile、创建tag／GitHub Release或修改许可证。真实Voice／真人搭档／教学效果尚未UAT。

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
