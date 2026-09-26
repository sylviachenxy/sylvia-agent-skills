# Codex 分发与维护

在修改目录、发现配置、manifest、版本、安装或发布时读取；不要求普通教学文案修改启动完整发布流程。

新建、编辑、弃用、归档、删除、恢复或判定版本升级前，先读[生命周期与 SemVer 规范](maintenance-policy.md)。本文件保留分发操作细节，不另行定义一套版本判定规则。

## 唯一事实源

- 仓库 marketplace：`.agents/plugins/marketplace.json`，名称 `sylvia-agent-skills`。不增加 Claude 兼容 catalog 或 commands。
- 两个领域 plugin：`sylvia-learning` 与 `sylvia-productivity`；同领域新增 skill 不新增 marketplace plugin。
- plugin 源文件位于 `plugins/<plugin>/`；skill 位于其 `skills/<skill>/`，每个 skill 只有一份实体内容。
- manifest 使用 Plugin Creator 当前支持的 `.codex-plugin/plugin.json`。只在此维护 plugin 的 SemVer；marketplace 不再声明版本，不另建重复 identity 的 root `plugin.json`。
- 当前纯 skills，不声明 MCP／apps／hooks，不自动安装另一个 plugin。以后确需外部组件时必须明确依赖、授权及新的验收范围。
- 根 README 与 plugin README 是对外说明，catalog 与 manifest 是发现／版本事实源；新增、删除或改名需同步这些入口和 CHANGELOG。
- `LICENSE` 原授权保持不变。每个 plugin 携带实体 LICENSE，校验必须与根 LICENSE 一致；不把可安装等同于开源授权。

字段以[官方插件打包说明](https://developers.openai.com/plugins/build/plugins)及实际宿主为准。官方也提供 portable root manifest 路线；本仓库当前仅使用其仍支持的 Codex scaffold 路线，不能同时维护两份版本 authority。

## 路径与完整性

marketplace `source.path` 是相对于仓库根的 `./plugins/<plugin>`，不是相对于 `.agents/plugins/`。manifest 的 `skills` 是相对于 plugin 根的 `./skills/`。

发布包不允许目录或文件软链接，即使目标在同一仓库内也不允许。2026-09-24 在 Codex CLI `0.155.0-alpha.16` 实测：plugin 内指向根 `skills/` 的目录软链接被安装器跳过，但命令仍返回安装成功，结果是空 skills 目录。因此不能只校验开发目录可读、manifest 合法或退出码为零。

教材转写、结构化词表、图像、脚本、模板和来源说明都必须留在其 skill 内；`research/`、OCR 底稿、临时日志、用户档案、账号配置与本地 `AGENTS.override.md` 不得进入包。不得用开发机绝对路径补偿漏装资源。

脚本按实际安装位置解析资源；技能之间按名称和职责协作，不硬编码另一个 plugin 的缓存路径。安装命名空间不改变 skill 的 frontmatter 名称。

## 验证层次

从仓库根运行：

```bash
python3 scripts/validate_marketplace.py
python3 -m unittest discover -s tests -v
gh skill publish --dry-run
```

`validate_marketplace.py` 仅验证本仓库约定的 JSON／路径／分组／版本 authority／基本技能发现子集，不是完整 YAML 或通用 plugin schema 校验器。结构回归覆盖重名、漏注册、越界、重复版本、空包、symlink 和资源／执行权限变化等失败情况。

本地 Finder 生成的普通 `.DS_Store` 文件不作为 skill 发现，且已由 `.gitignore` 排除出干净分发快照；这个例外不包括符号链接、同名目录或其他未知文件。根 `skills/` 仍是硬错误：未发布的独立源码应保存在仓库外，不能藏在另一个可扫描目录里或混入无关发布。

可用时还应运行当前 Plugin Creator 自带的 `scripts/validate_plugin.py` 检查两个 plugin，以及 Skill Creator 的 `quick_validate.py`／官方 `skills-ref validate` 检查变动 skill。按实际安装位置解析工具；不要把维护机上的路径写进用户技能，也不要静默安装缺失工具。

### 隔离原生安装验收

```bash
python3 scripts/smoke_marketplace.py --codex /absolute/path/to/verified/codex --test-skills
```

需要 Python 3.11+、Git、支持 `gh skill` 的 GitHub CLI，以及有 `plugin`／`app-server` 能力的 Codex CLI。先运行 CLI 的 `--version` 和 `plugin --help`；故障 wrapper 的排查见根 README。

验收脚本：

1. 从 Git 跟踪文件与未忽略的新文件制作干净快照，保留执行权限，不复制忽略的开发产物；也支持尚未暂存的目录迁移。
2. 仅对子进程设置临时 Codex 配置目录，添加本地 marketplace 并安装两个 plugin；不改当前用户 profile，不复制登录凭据，不建立任务或调用模型。
3. 核对版本、启用状态、**安装后的完整文件集合、逐文件 SHA-256 与执行权限**；空壳安装必须失败。
4. 通过只读 app-server `skills/list`，确认当前 catalog 的全部 skill 真正从临时安装缓存加载，而不是从源码、旧独立安装或当前工作目录碰巧被找到。
5. 用 `gh skill install --from-local` 按名称逐个安装当前 catalog 中的全部 skill 到另一临时目录，核对资源；仅允许 `gh` 注入自己的 frontmatter 来源元数据及边界空行规范化。`gh 2.98.0` 本地安装实测丢失部分脚本执行位，因此额外报告 `gh_executable_mode_warnings`，不将独立安装的内容完整性表述为运行通过。原生 plugin 安装的执行权限变化仍是硬失败。
6. 带 `--test-skills` 时，从安装包运行各既有离线测试套件（合成资料／mock，不读取用户 Apple 或学习数据）。两份独立断言脚本按其 main 入口执行，不把 unittest 的零测试结果记为通过。所有临时安装在退出时清理。

这覆盖本地 catalog → native install → runtime discovery 和独立安装。**不证明** GitHub 远端已有这些文件、桌面 UI 所有版本均兼容、真实 Voice／Apple 权限／iCloud／iPhone 链路正常或教学效果达标。

## 版本与更新

- 版本对象、`0.x`／稳定期分级、预发布、不可变性与回滚统一遵循[SemVer 规则](maintenance-policy.md#2-semver)。两个 plugin 的首次分发版本均为 `0.1.0`；数值唯一 authority 仍是各自 `.codex-plugin/plugin.json`。
- 用户可安装的 `main` 也属于对外分发，不能推送已发布版本的变更后再补升版；日常未发布迭代不要求每次保存／commit 升版。只改包外治理文档且不影响安装契约时不升 plugin。
- 已装的本地开发包若需反复测试，遵循当前 Plugin Creator 的 `read_marketplace_name.py` → `update_plugin_cachebuster.py` → 原生重新安装流程。cachebuster 只用于获准的本地开发，不代替正式发布版本递增。
- 安装缓存不是源码。不要手改缓存、用删除缓存掩盖版本问题，或把 upgrade catalog 当作所有 plugin 都更新完毕。
- Git marketplace 使用 README 中 `marketplace upgrade` → 按需 `plugin add` → 读回版本和启用状态的流程；在新 Codex 任务验证发现。先核对来源，不能把本地同名 marketplace 当成 Git 来源。
- 旧 `gh skill` 来源记录可能仍指向根 `skills/`；目录迁移不应承诺透明原地 update。用户备份改动后按名称重新安装，或选择迁到 plugin。不得自动删除旧副本／个人数据。

## 首轮本地验收记录（2026-09-24）

以下为首次发布前的本地验收快照，不代表之后的 Git／PR 状态。远端合并与 GitHub 来源安装的后续证据保存在对应 PR。

- Codex CLI `0.155.0-alpha.16`：两个 `0.1.0` plugin 安装、启用与只读运行时发现通过，8 个 skill 均来自隔离安装缓存。
- 学习包 103 个文件、效率包 102 个文件，安装后文件集合、SHA-256 与执行位逐项一致。
- 24 项分发层回归通过；安装包内 IELTS 35 项、上海高考英语 109 项、晨间简报 236 项、周复盘 110 项，共 490 项 unittest 通过且无跳过；日程 executor 与周复盘 EventKit reader 的两组独立离线断言也通过。
- 两份 plugin manifest 校验、8 个 skill 的官方 skills-ref 校验、`gh skill publish --dry-run` 和分发文档本地链接检查通过。gh dry-run 的现有 tag protection 提示不表示发布失败，也未据此修改远端规则。
- 原 199 个 skill 文件全部迁入新路径，197 个字节级相同，另两份仅更新维护命令路径。未修改教学指令、脚本、教材、学情或授权。
- `gh 2.98.0` 本地按名称安装 8 个 skill 的发现与内容检查通过；4 个个人效率 skill 存在执行位丢失，已单独报告，未宣称这条安装路线运行验收通过。
- 未进行 GitHub marketplace 远端安装、桌面 UI 全流程或真实账号／设备 UAT；未改用户正式 Codex 安装、commit、push、tag 或 release。

## 发布门槛与边界

1. 检查 diff 和 Git 状态，确认没有无关改动、重复技能、忽略文件或公司内容。
2. 按[维护规范](maintenance-policy.md)记录每个 plugin 的生命周期操作、版本分级依据和迁移影响；运行上述校验与相应离线测试，记录工具版本、通过项与未覆盖项，更新对外文档／CHANGELOG。退役时检查新发现集合不再包含目标，旧引用与保留组件仍完整。
3. commit、push、tag、release、用户 profile 安装各自需要授权。本地验收不触发这些操作。
4. 获准推送后读回远端 SHA，再以远端 `sylviachenxy/sylvia-agent-skills --ref main` 做单独的隔离安装验收。未完成前不要声称“其他人已能从 GitHub marketplace 安装”。
5. 仓库 marketplace 分发与官方公共 Plugins Directory 上架是不同事项；本仓库不申请公共目录上架，也不改变访问权限或许可证。

只读运行时发现协议参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。
