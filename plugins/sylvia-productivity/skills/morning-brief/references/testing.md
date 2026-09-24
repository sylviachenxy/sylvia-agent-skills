# 维护者测试与用户验收

## 离线验证

维护者从 skill 根目录运行全套检查（包含原生 reader 构建）：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/morning-brief.py doctor
python3 scripts/morning-brief.py validate-config --config assets/config.example.json
scripts/apple-eventkit-reader.sh doctor
scripts/apple-eventkit-reader.sh self-test
```

reader 构建需要 macOS 14+ 与 Swift；doctor/self-test 不读取原生 Calendar/Reminders，也不请求权限。配置管理使用 macOS/POSIX 文件锁，本 skill 的完整运行环境仍是 Mac；其他平台仅运行其确实支持的独立纯校验测试，不把不具备的项目伪称通过。

用户 setup 按启用模块选择检查：基础集合为 `test_core.py`、`test_cli.py`、`test_config_store.py`、`test_config_bindings.py`、`test_notes_publisher.py`、`test_phone_protocol.py`，分别使用 `python3 -m unittest discover -s tests -p 'test_core.py'` 等命令。只有 Calendar/Reminders 启用时才加 `test_sources.py` 和 reader 命令；维护者全套检查不是每个用户的安装依赖。

从仓库根目录运行 `gh skill publish --dry-run` 和系统 skill-creator 的 `quick_validate.py`；有可用 `skills-ref` 时也运行其 validate。没有验证器不偷偷安装依赖，记录缺项并做等价结构/引用检查。dry-run 不是发布。

用例应验证行为而非只匹配措辞：

- 任意有效收集窗、整 24h、零时长、超限、跨午夜、DST、不在执行星期、未来截止与生成缓冲冲突。
- 禁用模块不接受输入、不参与 READY；仅指定主题；空结果与未读取不同；日期待办、事件前瞻、重复实例、未批准/暂停 Goal。
- 原生 wrapper 离线不初始化 store；请求/响应字段、大小、超时、stdout 单一 JSON、异常不泄漏私人内容。
- 渲染确定性、UTF-8/NFC、可见正文哈希、机器标记注入、未知时间、过期与 PARTIAL；URL 与引用保留。
- Notes 离线预检无外部接触；同版重试不重复创建，重复版本/用户注释不覆盖，写后失联不盲重建，隐藏正文不代替可见正文。
- 私有目录权限、账本和本机锁、配置指纹隔离；配置验证记录不等于实际设备验证。
- 固定 registry/自定义 profile 位置、新进程发现、默认选择歧义、导入、无变化保存、旧版本提交拒绝、提交前失败保持旧版、提交后未知结果查证、历史恢复为新版本；测试只用临时 registry，不写真实默认位置。
- 固定手机参数不含偏好/配置版本：内容、窗口、时区、星期、生成/起床时间及恢复均不改手机 handoff 或记录；排程改变只要求 Mac 适配，A→B→A 不复活旧排程绑定。过期指纹不可确认，未适配/损坏记录阻止正常发布，只有显式 setup 试发可跳过尚缺的适配记录。
- 同一日 c1/r99→c2/r1→恢复后的 c3/r1 自动选择；Notes 标题、本地产物和 journal 不碰撞，旧 uncertain/conflict 不能通过提高配置版本绕过。固定身份/Notes 入口不能混进普通偏好更新。
- 无 Mac 配置输入的手机参考实现：hash、十二个唯一元数据字段、绝对适用区间、跨时区/DST、未来生成、过期降 PARTIAL、重复版本、尚未同步新报告、损坏新版时标明旧快照；不声称掌握 Mac 最新配置。

## 本 first cut 的证据边界

纯 Python 测试、原生 reader 编译与合成 self-test 是维护者代码证据，不代表任何用户的真实来源权限。公开资产中的数据完全虚构，不能作为真实新闻、个人偏好或已采集内容。

2026-09-02 初版检查：116 项 unittest 通过（含原生离线桥测试），EventKit reader 21 项合成自测通过；`quick_validate.py`、内部引用/元数据检查、`gh skill publish --dry-run` 通过。`skills-ref` 未安装，未执行该命令，也未为此安装依赖。发布 dry-run 提示仓库未启用 tag protection；本轮未修改远程规则或发布内容。

同日配置持久化迭代曾通过 183 项 unittest 与 41 次独立 CLI 进程前测，覆盖持久化、隐私、并发、损坏记录和发布门槛。该阶段仍把手机与配置 revision 耦合；**此行为已被用户否决，由正文协议 2 和稳定接收器绑定替代，不再是验收标准**。原测试记录不能证明新协议已在手机验收。

同日手机解耦迭代：**236 项 unittest 全部通过**（core 46、存储/绑定 66、CLI 17、publisher 64、phone protocol 23、只读来源 20）。跨组件用实际配置存储、渲染、发布事务和手机参考实现，只将 Apple Events 换成内存 Notes；c1/r99→c2/r1→恢复后的 c3/r1 均自动选中，手机参数和持久绑定文件逐字节不变。结构/内部链接检查和 `gh skill publish --dry-run` 通过。所有测试仅使用合成数据及私有临时 registry；未改真实配置、手机、自动化或真实 Notes。新 v2 原生读回和 iPhone 动作仍须首次 setup 验收，不能用 Python 参考实现代替实机。

独立行为前测另通过 49 个 CLI 进程（含预期拒绝），覆盖主题/新增天气/语言/两时间窗/条数/保留意图、Tokyo 时区与星期/生成/起床时段、再恢复为 c4。手机始终只有首次一条确认记录，文件逐字节不变；Mac 排程按 A→B→A 实际合成适配三次。固定接收器通过跨手机日期/时区、未同步新版、损坏新版降级提示和过期 PARTIAL；Notes 出口变更被拒绝且当前配置不变。未发现要求普通偏好变更重配手机的遗留路径。

此前本地 PoC 已由用户验证 Notes 阅读/外链返回，以及停止闹钟后锁屏 READY 通知出现、点击才解锁；那是单一日期标记版，不是新正文 hash、profile/revision 或动态新鲜度逻辑的完整验证。

此前正文协议 1 的开发机合成集成验证（2026-09-02）：默认 AppleScriptObjC 发布桥在获准的专用测试文件夹中创建一份 Note，完整可见正文、HTML 链接读回一致；独立 verify 与同版重试均返回同一 Note，没有重复创建。未读取真实日历、待办、Goal 或其他笔记；本机验证仍返回 `iphone_sync:"unverified"`。

Mac Shortcuts 此前已实跑协议 1 完整合成正文的锚定正则、Group At Index 1、Text 转换与 SHA256，中文多行正文所得 hash 与生成器一致。这个测试证明本机动作组合可用，不等于协议 2 或 iPhone 的 Find Notes、逐字段校验、版本选择、新鲜度判断和锁屏分支已经全部验收。

新用户必须按 setup guide 完成本机发布读回、手机新协议、真实启用来源和一次定时端到端验收。不能只因开发机离线测试通过或 Note 已生成就标为“全部设置好了”。脚本的生成时 READY 也不代表手机在任意以后时刻仍能宣称新鲜。

## 修改后的回归

维护者修改协议/语义/schema 实现后跑对应单元测试、固定接收器回归和跨组件 package→publisher 校验；修改 Notes 文本转换后做经批准的合成原生写入/读回；修改手机提取、校验或通知实现后在目标设备复测相应分支。普通用户只改偏好不属于协议修改，不要求手机重配或复测。

setup checkpoint 按配置指纹保存原观察；`setup-status.inherited_stages` 引用稳定手机绑定，不写虚构的新设备证据。普通偏好更新不得以 checkpoint 换指纹为由要求重新验手机；来源或 Mac 排程变更只核查受影响部分，不一键伪造全套通过。

正式生成涉及新来源或新发布范围时，先取得相应授权；不要为测试读取所有日历、待办、Notes、邮件或整个 Vault。开发测试生成物保存在仓库外私有目录；无关和旧版本数据保留，不自动清理。
