# 配置持久化、发现与变更生效

配置文件是偏好的事实源，context 只用于本轮讨论。每次运行重新定位并读取磁盘上的当前版本；不能用对话记忆、仓库示例、setup checkpoint 或已生成的晨报反推配置。

## 固定入口与私有存储

默认注册目录为当前 macOS 用户的 `~/Library/Application Support/morning-brief/`。这是本 skill 的约定，不是 Codex 自带配置目录。配置、版本与绑定记录独立于 skill 安装位置，更新 skill 不应覆盖它们。

```text
morning-brief/
├── registry.json                         # 默认 profile、目录与已提交版本索引
├── profiles/<config_id>/revisions/        # 私有、不可变 JSON 配置快照
│   └── rNNNNNN-<fingerprint>.json
└── bindings/<config_id>/                 # 手机/排程的已观察适配记录
```

`config list` 从固定位置发现；`config show --profile <id>` 返回当前完整 `config`、版本、指纹及实际文件路径。没有指定 profile 时选显式默认项；只有一个已登记项时可定位唯一项，多个而无默认则要求选择，不猜偏好。`config use --profile <id> --apply` 设置默认项。定时任务必须固定自己的 profile，不能跟随交互默认项改变。

允许首次保存时用 `--profile-dir` 选择另一仓库外私有目录，固定 registry 仍保存其位置。`--registry-dir` 是测试/明确隔离环境的高级覆盖，不是普通自定义配置位置；用了它就必须在后续调用和自动化中固定该绝对路径，不能期待默认入口发现另一份 registry。不要通过环境变量或扫描磁盘寻找配置。

受管目录为 0700、文件为 0600；拒绝仓库、skill 安装目录、用户控制的 symlink 与损坏索引。不自动放宽已有目录权限，也不把配置同步到 iCloud。`config show` 会输出私人偏好，只在用户配置工作所需范围内读取，不贴到公共日志。索引、快照与正文都不是凭据存储。

## 首次保存、导入与更新

下面的 `BRIEF_DRAFT` 是用户确认的私有绝对 JSON 路径；`BRIEF_PROFILE` 是该配置的 `config_id`。所有变更命令没有 `--apply` 都只预览，不创建目录或改变当前配置。

```bash
python3 scripts/morning-brief.py config list
python3 scripts/morning-brief.py config show --profile "$BRIEF_PROFILE"
python3 scripts/morning-brief.py config save --profile "$BRIEF_PROFILE" --input "$BRIEF_DRAFT" --expect-revision 0 --make-default
python3 scripts/morning-brief.py config save --profile "$BRIEF_PROFILE" --input "$BRIEF_DRAFT" --expect-revision 0 --make-default --apply
```

初次登记的 `--expect-revision 0` 表示尚未登记，不表示配置内版本为零。新配置通常为 revision 1；导入既有私有 JSON 时保留其正版本号、身份和原 `state_dir`，不清空发布账本。输入必须是完整配置对象，不能把 `config show` 含路径/状态的整个响应直接当配置；使用响应内的 `config`。

以后用户说“关闭天气”或“改关注对象”时：

1. `config show` 读取当前文件，记下当前 revision 和指纹，基于它准备私有草稿；不编辑受管快照。
2. 展示用户能理解的变更，确认范围。草稿保留当前 `config_id/config_revision`；身份不能变，现有 `state_dir` 也不能借更新换掉。
3. `config save --profile ... --input ... --expect-revision <当前版>` 预览；先修正错误，再加 `--apply`。
4. 工具在锁内确认当前版本未变，实质变更自动递增 revision。若其他会话已更新，拒绝旧提交；重新读取并合并用户意图，不加“强制覆盖”。无实质变化不制造版本。
5. 保存后检查 `configuration_saved` 和独立的 `deployment`。已验证的手机入口保持有效，无需手机操作；若 Mac 排程受影响，在 Mac 完成更新。不能仅凭 `ok:true` 就说新报告已经同步。

写入先生成不可变快照，再原子替换 registry 作为唯一提交点，并同步目录。提交前失败不改变当前版本；未被索引引用的中间快照不会自动当成新配置。提交后同步或返回结果失败则可能已经保存，异常后用 `config show/history` 查证再决定重试，不能直接声称旧版仍生效；不要盲目递增或删除文件。索引或快照损坏时停止，不悄悄回退旧偏好。

`config_id`、`storage.notes` 与 `state_dir` 属于首次安装确定的身份、分发入口和发布账本，不是阅读偏好。`config save/restore` 不得更换 Notes 账号/直属文件夹或 state_dir；不能先保存一个手机收不到的新出口，再要求手机追着改。换设备、换 iCloud 账号/分发文件夹或迁移 registry/profile/state 是另行确认的安装迁移，需先处理未知发布、暂停相关自动化并保留完整账本；first cut 没有自动迁移命令，不能复制部分文件或换个 profile 绕过保护。

## 历史与恢复

```bash
python3 scripts/morning-brief.py config history --profile "$BRIEF_PROFILE"
python3 scripts/morning-brief.py config restore --profile "$BRIEF_PROFILE" --revision 1 --expect-revision 3
python3 scripts/morning-brief.py config restore --profile "$BRIEF_PROFILE" --revision 1 --expect-revision 3 --apply
```

历史列表只包含已经提交的版本。恢复旧偏好会产生新的递增版本，不把版本号倒退，也不覆盖旧快照；恢复后仍检查适配状态。没有自动清理历史、删除 profile 或强制修复索引的命令。备份应保留 registry、所有指向的 profile 目录、bindings 以及 `state_dir`，不能只备份当前 JSON 就认为保留了全部执行状态。

## 稳定手机入口与 Mac 端偏好更新

```bash
python3 scripts/morning-brief.py config status --profile "$BRIEF_PROFILE"
python3 scripts/morning-brief.py config handoff --profile "$BRIEF_PROFILE"
```

`handoff` 生成确定的适配参数和自动化提示，不操作手机或创建定时任务：

- **iPhone**：仅首次接收 `protocol_version:2`、稳定 `config_id` 和 Notes 账号/文件夹。A/B 是通用接收器，不存配置 revision、指纹、时区、星期或时间参数；也不复制主题、城市、日历/列表 ID、Vault 路径或 Mac 本地路径。每份简报的正文带完整性 hash、适用时间区间、生成时刻、新鲜度及版本。手机自动读取、校验和排序；这些动态值不是要写入快捷指令的参数。
- **Mac 排程**：固定 registry 绝对路径与 profile，每次读取当前配置；不要绑定某个历史快照，也不把整份偏好复制进提示。变更时区、星期、生成时间等排程参数，要在 Mac 更新实际自动化并读回核对。只改内容、窗口、条数等而排程参数未变时，直接沿用最近一次相同参数的已验记录。

手机绑定签名只取稳定接收器参数，不包含配置版本、配置指纹或配置文件位置。因此内容模块、关注对象、时间窗、时区、生成/起床时间、星期、保留意图的更新及历史恢复都不改变手机参数或记录，不要求重新 acknowledge。第一次安装、换接收器/设备、修复已被用户改变的快捷指令，以及正文协议不兼容升级，才需要设备验证；**配置版本与接收协议版本是不同概念**。旧协议 1 的证据可保留查阅，但不能伪装成协议 2 已通过。

Mac 排程只比较最近一次观察到的绑定。A 时段→B 时段→恢复 A 时，不能因为历史上验证过 A 就认为外部排程已经恢复；需在 Mac 复原，手机仍无需改变。配置被手工绕过工具编辑、手机或自动化被用户另行修改时，既有记录不能证明外部状态仍正确，需重新查证。

适配过程中若需暂停现有 Mac/手机自动化，先说明影响并获准；用当前产品提供的自动化工具或官方界面更新精确既有任务，保留无关字段，不重复新建、不写私有 cron/launchd。官方支持从对话创建/更新排程：[OpenAI Scheduled tasks](https://learn.chatgpt.com/docs/automations)。本地文件仍要求 Mac 和应用在相应时段可运行。

首次 setup 完成实际操作后，记录手机报告/观察结果，以及自动化读回确认的真实 ID。将 `BRIEF_FP` 设为当前 `config show` 返回的指纹；这是防止确认期间配置被并发修改的 Mac 端检查，不是手机参数：

```bash
python3 scripts/morning-brief.py config acknowledge --profile "$BRIEF_PROFILE" --target iphone --expect-fingerprint "$BRIEF_FP" --binding-id "$BRIEF_PHONE_BINDING" --evidence "$BRIEF_PHONE_EVIDENCE" --apply
python3 scripts/morning-brief.py config acknowledge --profile "$BRIEF_PROFILE" --target automation --expect-fingerprint "$BRIEF_FP" --binding-id "$BRIEF_AUTOMATION_ID" --evidence "$BRIEF_AUTOMATION_EVIDENCE" --apply
python3 scripts/morning-brief.py config status --profile "$BRIEF_PROFILE" --require-ready
```

这些是带身份、签名与时间的 operator evidence，不是工具自动验证手机或服务端。没有实际证据不能 acknowledge。`--require-ready` 在缺少匹配记录时失败；日常自动化先做此检查，失败就停止采集/发布并报告待适配。正常 `publish --apply` 也有同样门槛，并在发布期间锁定配置版本，防止旧候选抢在新配置后写出。

首次 setup 有依赖顺序：需要先发布一份才能验手机。经用户批准可用 **`publish --profile ... --candidate ... --apply --setup-test`** 做一次明确的试发；它仍校验配置、授权范围、正文和发布事务，只允许绕过尚未完成的适配记录，不绕过损坏记录，未就绪时输出明确 `deployment_ready:false`。不得将此开关写入定时任务，或用它跳过日常失败。

全部绑定匹配只说明已记录下游适配；完整 setup 还需要启用来源、本机读回、手机闹钟和真正的定时端到端验证。后续改偏好不是重做 setup；`setup-status` 以 `inherited_stages` 和原绑定证据说明手机验证可沿用，不伪造新手机测试记录。

保存新配置后，下一次采集只按新偏好执行；手机从下一份成功生成并同步的报告自然获得新内容。旧已同步 Notes 不会被删除，尚未生成/同步时手机可能仍显示当下适用的旧快照，须展示它实际的配置/正文版本和生成时间，不能声称已经看到 Mac 最新配置。同步后的新配置版本优先于旧版，即使新报告的正文 revision 从 1 开始。没有合格当前报告时提示 NOT_READY；不承诺跨设备原子切换，也不要求手机刷新一份配置文件。

## 旧入口与恢复兼容

`--config <明确JSON路径>` 继续用于离线校验、预览和 `verify` 某次结果未知的历史版本；它不自动登记 profile，也不允许正常真实发布。导入后日常使用 `--profile`。发生未知发布时优先用原候选与原配置快照查证；不把新配置强塞给旧候选，不用换 state/profile/revision 的方式绕过发布冲突。
