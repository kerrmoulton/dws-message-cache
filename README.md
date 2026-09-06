# ddmsg — 给 AI 用的本地钉钉工作消息工具

本机 DWS → Collector → SQLite＋媒体文件 → CLI → Skill。支持分页采集、消息去重和基于同步进度的增量更新。采集与下载不调用模型，查询默认没有网络请求。

## 安装和开始使用

支持 Python 3.10+、macOS 和 Windows，无第三方 Python 运行时依赖。前提是 DWS 已安装并登录目标组织。

```sh
uv tool install --editable /path/to/dws-message-cache
ddmsg init --profile corpId:userId
ddmsg doctor
ddmsg status
```

也可在独立虚拟环境中 `python -m pip install -e .`，或执行 `python ddmsg_cli.py ...`。Windows 可用 `py ddmsg_cli.py ...`。profile 使用 DWS profile list 返回的真实值；省略时 init 选择 DWS 当前账号。

默认配置路径为 ~/.ddmsg/config.json；项目目录存在 config.json 时会优先使用。`--config PATH` 或 DDMSG_CONFIG 可指定配置路径。关注状态保存在 SQLite 中，通过 ddmsg source 命令管理，无需手工编辑 JSON。

优先调用 npm DWS 包内的原生 vendor/dws 或 vendor/dws.exe，避免 Node 启动开销和 Windows .cmd 参数解释。其他安装位置可设置 DDMSG_DWS 为原生程序路径。

## 查询

```sh
ddmsg query --conversation performance --sender 示例同事 --limit 10
ddmsg query --conversation performance --text 复制 --since '2026-09-01T00:00:00+08:00'
ddmsg recent --conversation performance --fresh --limit 10
ddmsg at-me --limit 10
ddmsg context 103 --before 15 --after 10
```

performance、103 仅为 source 和消息 ref 示例；请使用实际查询结果。默认 20 条，每条正文最多 1200 字符。has_more=true 时把 next_before 传给 --before；truncated=true 可增大 --max-chars 或补 context。

普通查询只查 SQLite，输出 freshness。--fresh 定向刷新指定会话；at-me --fresh 刷新 @来源并补上下文。context --fresh 补原消息前后 30 分钟，使用独立水位，不误推进整个会话的同步进度。未关注会话的 targeted fresh 只产生 adhoc 缓存。

支持按会话、精确发送人名称／ID、正文、起止时间查询。无时区时间按 Asia/Shanghai 解释。FTS5 trigram 可用时用于 3 个以上字符的子串搜索，短中文词或旧 SQLite 使用限范围 instr 搜索。

## 关注管理

```sh
ddmsg discover 绩效 --kind group --fresh
ddmsg discover 示例同事 --kind person --fresh
ddmsg source add <cid> --mode permanent --sync
ddmsg source add <cid> --mode temporary --hours 72 --sync
ddmsg source remove <cid或source>
ddmsg source ignore <cid或source>
ddmsg source list
```

discover 本地优先，加 --fresh 时使用 DWS。group 搜群；person 按姓名找人并解析单聊；conversation 获取 DWS 可见会话。重名返回候选，不默认选第一个。发现不会自动关注。

| mode | 行为 |
|---|---|
| permanent | 持续同步 |
| temporary | 到期前同步，默认 72 小时 |
| adhoc | 仅显式查询刷新 |
| disabled | 用户取消，停止同步并阻止自动恢复，历史保留 |
| ignored | 排除采集，正常查询默认隐藏，历史保留 |

source add 可以恢复来源。source remove mentions 暂停 @来源，source add mentions 恢复。所有修改通过 CLI，无需改配置文件。

## 后台采集

```sh
ddmsg scheduler install --mode hybrid --interval 600
ddmsg scheduler status
ddmsg scheduler run-now
ddmsg scheduler uninstall
```

默认 hybrid：一个 @事件订阅及时触发补查；每 10 分钟补齐关注会话和本人回复。不是每个群都启动一个进程。普通群消息最多等待一次周期；需要马上看时 targeted --fresh。@事件合并，最多每 20 秒刷新一次，并有延迟复查容纳索引延迟。

纯短生命周期方式：`ddmsg scheduler install --mode interval --interval 300`。macOS 使用用户 LaunchAgent，Windows 使用用户 Task Scheduler（InteractiveToken、LeastPrivilege、IgnoreNew）。无需管理员权限／保存密码；Windows 需要用户已登录。配置改变在下一次采集生效。

预览：`ddmsg scheduler install --dry-run`，或加 --platform win32 检查 Windows XML。短时运行：`ddmsg listen --duration 30`。手动增量：`ddmsg collect`；历史补查可加 --source、--since、--until。

Collector 只在全部页成功后推进该来源水位，默认回看 5 分钟。失败保留已缓存消息，重试去重。单轮有时间和页数上限，单实例锁跨平台。SQLite WAL 支持查询并发读取。日志每份 256 KiB，2 个备份；运行历史保留 100 次。消息不自动删除。

睡眠、断线或失效登录时不保证实时，恢复后从水位补查。DWS 的晚索引超过重叠窗口、被撤回或服务端已不保留的消息仍可能需要显式重扫或无法恢复。

## @我策略

当前 DWS 没有明确结构化个人／全员 mention 字段。采用已验证的保守规则：DWS --at-me 返回且正文精确出现 @当前账号姓名才为 direct；明确 @所有人为 all；其他不可核实为 unknown。只检查正文，不把引用或表情中的姓名当作 @。全员＋个人混合也保守归入 all。

at-me 默认只看 direct，可用 --kind all 或 --kind unknown。此规则不保证识别所有群昵称／富文本格式。

新 direct 自动补上下文；未知且未禁用／忽略的群可临时关注。到期时间基于消息发生时间，旧消息回放不续期，同一消息不重复触发；默认最多 10 个自动临时来源。采集不进行模型语义分类，少量直接点名的生活消息仍可能进入候选，可 ignore。

```sh
ddmsg settings show
ddmsg settings set temporary_hours 72
ddmsg settings set max_auto_sources 10
ddmsg settings set overlap_seconds 300
```

## 媒体和分析队列

```sh
ddmsg media list --message <ref>
ddmsg media get <media-id>
ddmsg media annotate <media-id> --file interpretation.txt --model <实际模型名>
ddmsg inbox --limit 20
ddmsg ack <batch-id>
ddmsg export
```

图片／附件使用 DWS 可下载的 mediaId；本体按 SHA-256 保存在文件系统，SQLite 记录关联、路径、MIME、大小及解读。已下载不重复获取，内容相同共享文件。没有可用资源标识时先 context --fresh，仍没有则报告接口限制。当前只按需下载，不默认下载全部附件，也没有远端大小预检。

annotate 保存 Agent 已生成的解读，不调用 OCR／模型。下一次 media list 返回已有解读。下载与理解分离。

inbox 返回有限批次和不透明 batch ID；分析结果成功保存后才 ack，后续新消息不会被跳过。普通 query 不清分析队列。export 显式生成 JSONL，日常 collect 不重写全部文本；SQLite 是事实源。

输出 JSON，返回码 0 成功、1 失败、2 部分采集／刷新失败。部分失败时保留本地结果和错误，不把它说成最新。

输出信封包含 schema_version=1。`ddmsg schema` 提供机器可读的命令／参数说明，不访问 DWS 或缓存。Windows 后台优先 pythonw.exe，DWS 子进程隐藏控制台窗口。

## Skill 和验证

可分发 Skill 在 skill/ddmsg。复制该目录到 Agent 技能目录（例如 ~/.codex/skills/ddmsg），安装 CLI 后即可使用。

`python -m unittest -v` 运行测试；附 macOS/Windows CI 矩阵。Windows 核心和任务 XML 有自动测试，但当前环境为 macOS，Windows 实机运行仍需独立验证。设计见 [docs/DESIGN.md](docs/DESIGN.md)，实测见 [VERIFICATION.md](VERIFICATION.md)。
