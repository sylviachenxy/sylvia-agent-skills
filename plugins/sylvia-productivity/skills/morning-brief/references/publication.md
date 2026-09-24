# Notes 发布、恢复与手机读取

## Mac 发布事务

使用 `morning-brief.py publish` 离线预检；只有用户批准精确输出范围后才加 `--apply`。目标是 Notes 中一个精确账号的直属、非共享文件夹；first cut 不递归搜索同名嵌套文件夹，不自动创建/改用文件夹。

真实发布使用受管 `--profile`，检查当前配置与下游适配；配置已保存但手机/排程未适配时，正常发布被阻止。初次本机/手机验收可明确使用 `--apply --setup-test` 做一次试发，不作为日常替代。配置指针在真实发布期间被锁定；若候选形成后配置已变，停止并重新确认，不把旧候选写到新配置之下。

生成包包含稳定 `brief_id`、配置身份/版本、适用日及正文 revision。标题是 `晨间简报 · YYYY-MM-DD · mb-… · cNN · rNN`。配置 schema 和 package schema 仍为 1，手机正文协议为独立的 2；不要混同这些版本。每个配置版本可从正文 revision 1 开始；本地文件、Note 标题和发布账本都区分 c/r。同版重试检查同一完整标题：

- 零条且没有未知写入记录：允许创建；先持久记录 pending，再写，最后重新读回。
- 一条且所有可见正文、HTML 中的链接和包一致：已有版本，幂等返回，不再创建。
- 一条但内容不同、用户已注释、共享/加锁或正文不完整：冲突，保留原 Note，不覆盖。
- 多条同版、scope 不明确、读取不完整：停止，不任取第一条。

内部原生 Apple Events 桥仅访问确认账号/文件夹及匹配标题的 Note，公开命令不回显正文；输出给出 `local_verified` 与 `iphone_sync:"unverified"`。校验检查 plaintext 和 HTML 实际可见内容，不靠隐藏附件或成功标记。只允许已测试的文本布局；不支持的原生转换应失败，不宽松吞掉差异。

发布器在明确 state_dir 内保存私有锁和最小 journal。所有本机发布必须共用该目录；不能用第二个目录或另一台机器绕过锁。同步本身不是事务，单机锁不协调多机。

### 失败恢复

初次 lookup 失败是 read_error，不是“没有该 Note”，不能创建。写入可能开始后的超时/断线是 uncertain：先 `verify` 同一候选，找到完整同版即恢复 verified；没找到也不证明未写入，不自动重建、换 revision 或清掉 journal。

尚有未知写入时先查证该版本，不发布更高配置/正文版本；改偏好不能绕过原账本的 uncertain/conflict。只有用户在明确范围内核实并选择恢复方式后才处理冲突/账本；当前工具没有“强制覆盖”或“清状态重来”命令。保留旧完整版；历史或不符合固定入口的版本不能冒充当前。

verify 不修改 Notes，但会更新本地查证记录。原生超时有界；用户首次授权可能超过超时，因此在场完成授权后再查证，不能通过无限等待绕过权限问题。

若恢复协议 1 的既有 Note，使用当时留存的**完整原 request/package**（原授权范围、state_dir、标题、正文和 hash），通过 stdin 交给 `python3 scripts/notes_publisher.py verify`。旧包仅允许 verify，不允许重新 publish；结果明确 `protocol_version:1`、`legacy_verification_only:true`。主入口根据候选重新渲染的是协议 2，不能用它重建旧协议正文查旧 Note。没有留存原包时先查证并说明限制，不能猜旧 hash 或清除旧账本。

## 正文完整性格式

`body_sha256` 覆盖包的完整 `body_text`，用于 Mac 读回；内部 `content_sha256` 覆盖实际可见 content_text，供手机校验。它不是数字签名，也不证明事实、授权或“全网最新”。

正文结构：

```text
晨间简报 · 日期 · brief_id · cNN · rNN
MB:BEGIN
MB:CONTENT-BEGIN
这里是带适用日期、时区、来源、覆盖和时间的完整可见正文。

校验信息（供快捷指令读取）
MB:SCHEMA=2
MB:CONFIG=配置ID
MB:CONFIG-REVISION=配置修订
MB:DATE=适用日期
MB:TIMEZONE=IANA时区
MB:BRIEF=brief_id
MB:REVISION=版本
MB:STATUS=READY或PARTIAL
MB:GENERATED=带偏移的生成时间
MB:FRESH-UNTIL=带偏移的新鲜度截止
MB:VALID-FROM=适用日当地零点的带偏移时间
MB:VALID-UNTIL=次日当地零点的带偏移时间
MB:CONTENT-END
MB:CONTENT-SHA256=content_text的SHA256小写十六进制
MB:END
```

哈希输入为两个 CONTENT 标记之间的完整文本，不含边界换行，不含 hash 行；所有元数据与 substantive 内容在同一受校验区内。编码 UTF-8，NFC，LF 换行且无尾换行。用户文本不能注入 `MB:` 保留标记。Notes 传输只允许明确的换行、NBSP 和一个末尾换行规范化，不能 trim 任意空白、删行或忽略不认识的段落以求通过。

生成时 READY 是快照质量判断。`fresh_until` 是适用日结束与已启用必需来源的已知 `as_of + max_age_hours`、必需天气有效期终点的最小值；读取超过此时刻，通知降级 PARTIAL，不再宣称仍然新鲜。原本 PARTIAL 不能因等待而升成 READY。未知时间不补造；具体缺口留在正文。

`valid_from/valid_until` 由 Mac 按这份报告的时区解析为带明确偏移的适用日区间，终点不含。手机只比较绝对时刻，不保存配置时区，不按手机系统日期筛掉跨时区的正确报告。适用日因 DST 可以长 23/25 小时；这不是信息收集窗口，不能套用收集跨度 ≤24h 的限制。`CONFIG-REVISION` 只用于选择实际已同步的报告，不与手机常量比较；`TIMEZONE/DATE` 是报告声明和显示信息，生成时间及区间一致性在 Mac 先校验，手机再按传输协议检查。

## iPhone setup：建立两个独立入口

由 Codex 按当前设备的实际 Shortcuts 界面帮助设置，下列是动作逻辑，不是已经安装的快捷指令模板。动作名可能显示英文或中文；缺少动作或验证失败就停在该步骤，不擅自改成只检查日期。

### A. 通知检查

1. 只固定 `config handoff` 给出的 Notes 账号/直属文件夹、稳定 config ID 和支持的正文协议 2，不沿用开发者 PoC 的名字或 ID。不固定任何偏好、配置版本、时区、星期或钟点；这两个入口后续不随偏好修改。
2. Find Notes：限固定专用文件夹及晨报标题前缀；先取得候选，不能用手机“今天”或 Notes 创建/修改日预先筛选，不能开启 Limit 1。仍需正文中的身份、适用区间与版本检查，不能只信名称。不能读完整就报错，不静默截断后假称最新版；历史过多的清理需另行授权。
3. 对每份候选取明确的 **Note.Body** 文本。统一 CRLF/CR→LF、NBSP→普通空格，内容须为 NFC；不能删掉任意段落。如果该系统 Body 不含标题，接受无标题形式，但实际正文必须完整。
4. 用 Match Text 校验整份受管格式，恰好一次匹配。可使用下面的 ICU 正则：

   ```regex
   (?s)\A(?:[^\n]*\n)?MB:BEGIN\nMB:CONTENT-BEGIN\n(.*?)\nMB:CONTENT-END\nMB:CONTENT-SHA256=([0-9a-f]{64})\nMB:END\n?\z
   ```

5. 使用 **Get Group from Matched Text / Group At Index 1** 得到 content，转为 Text 后 **Generate Hash / SHA256**，与 Group 2 的摘要相等。不要把 Match 的“Get: Text”误当分组文本：它可能返回整个输入。禁止使用 MD5 默认值。
6. 只从已验 hash 的 content 解析上述十二个 `MB:` 元数据行；每类恰好一条，未知/重复行拒绝。schema=2、config ID 匹配固定入口；配置/正文 revision 都是正整数。将 GENERATED、FRESH-UNTIL、VALID-FROM、VALID-UNTIL 转成带偏移的绝对日期，不按手机时区重新拼日期；转换失败停止。起止分别为 DATE 及次日的 00:00、允许 DST 两端偏移不同；起点 < 终点，生成时刻在区间内且不晚于当前时刻，fresh-until 不得超过区间终点。
7. 当前时刻必须在 `[valid_from, valid_until)` 内；完整历史或未来适用区间不作为当前候选。保留当前候选中**最高数字 config revision，再取最高数字正文 revision**。同一逻辑 c/r 出现多份当前 Note，无论 hash 相同或 brief_id 不同都 READ_ERROR；不取第一条、不按标题字典序或修改时间排序。c2/r1 优先于 c1/r99，恢复偏好产生 c3 后也自动胜出。
8. 按选中正文的生成时状态和读取当时的 `fresh_until` 得到 READY/PARTIAL；没有合格当前报告且只有完整历史、未来适用日、其他入口或旧协议报告时 NOT_READY；只有损坏/不可解释候选，或当前重复版本冲突时 READ_ERROR。有完整当前报告又看到该入口的损坏候选时可显示该完整快照，但注明“另有版本校验失败，显示已验证快照”；无法识别归属的损坏候选单独报读取警告，不猜其入口。任何情况不能声称是最新配置。部分简报通知明确“部分内容可用”。
9. Show Notification 后结束，不串联 Open Note、Quick Look 或需要停留交互的预览。默认只含报告适用日期、状态、配置/正文版本和生成时刻、独立阅读入口名称，不带私人内容。

系统动作、权限或进程错误可能直接中止，来不及发 READ_ERROR 通知。必须验证实际失败表现；没有通知不能证明没有简报。Find Notes 在某设备/锁屏状态能否工作是该用户 setup 的验收项，不是协议承诺。

`scripts/phone_protocol.py` 的 `select_note(config_id, note_bodies, now)` 是上述读取/选择规则的无副作用参考实现，用于离线测试，不是手机 Python 运行依赖。手机 A/B 按同样逻辑实现；缺少必要原生动作时不能降低校验。其输入没有 Mac 配置、配置 revision 常量、时区或 schedule。版本校验保留在每份报告内，手机只固定协议版本与入口身份。

配置保存不等于报告已经生成或 iCloud 已同步。手机看不到尚未同步的新版，因此可以继续显示仍适用的旧快照，但必须带实际 c/r 与生成时间；任何情况下都不宣称“已确认 Mac 最新版”。新报告同步后自然选择更高配置版本，不刷新快捷指令、不拉取偏好副本、不重新要求手机确认。旧协议 1 或日期 PoC 不算协议 2 验收；协议升级是一次安装升级，不是每次改偏好的步骤。

### B. 正文阅读

使用 A 的同样校验、身份/适用区间/c/r 选择及**读取当时的新鲜度判断**，不能直接沿用早上的通知结果。若原本 PARTIAL 或当前时刻已超过 `fresh_until`，先显示“PARTIAL：部分内容缺失或已过新鲜度截止”，再允许 Open Note 到持久 Notes 页面。Note 内的“生成时状态”是历史快照，不改写成当前状态。例如截止 08:05 的简报在 09:00 阅读必须先提示 PARTIAL，即使 07:00 曾通知 READY。READ_ERROR/NOT_READY 时不要打开不合格内容冒充当前版。用户可以明确选择“查看历史”，但必须显示历史日期、c/r 与生成时间。

初版不承诺点击通知必然直接打开指定 Note。设置一个独立易找到的阅读快捷指令入口；按用户选择放在主屏幕/小组件。打开外链后通过 App 切换回 Notes，可重新打开同一篇。

普通快捷指令可经 iCloud 同步；个人自动化必须在每台手机创建：[Apple 个人自动化](https://support.apple.com/en-ca/guide/shortcuts/apd690170742/ios)。将 A 绑定用户确认的闹钟停止/起床触发，并在实际界面选择允许自动运行的方式；不能替所有 iOS 版本承诺完全相同的选项。

### 必须实际验证

- 中文、多行、链接、hash 与 package 相同；正文删改一行、重复标记、缺少末尾、错入口/协议、不一致的适用区间、未来生成均不能 READY。
- 保持手机参数不变，读取不同偏好/窗口/时区/生成时段及恢复后的配置版本；c2/r1 胜过 c1/r99。更改配置无需重新保存手机参数。
- 跨日窗口、DST、手机与报告时区不同、重复版本、源过旧降 PARTIAL、过期报告不能当前 READY。
- 正常运行、锁屏起床触发、手机尚未同步新版本、读取权限/动作失败的实际可见结果。
- Notes 阅读、外链往返、重开；只验证会受新实现影响的部分，不重复要求 Mac 合盖后再读已同步内容。

Mac 的原生动作测试不等于 iPhone 已通过；手机 hash 或版本选择没有验收前，不把自动就绪检查记成完成。通知去重若需要跨执行持久状态，须另选获准的状态存储并测试；本 first cut 不承诺恰好一次通知，重复闹钟触发可能重复通知。
