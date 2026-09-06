# ddmsg v0.2 设计

## 已验证的 DWS 契约

本机 v1.0.54 已查 help、leaf schema 和实际返回：

| 能力 | 命令 | 输出 |
|---|---|---|
| 会话／时间／@过滤 | chat message search-advanced | result.conversationMessagesList, hasMore, nextCursor |
| 群发现 | chat search | result.groups |
| 会话发现 | chat list-all-conversations | result.conversations |
| 找人 | aisearch person --dimension name | result 数组，author/userId/openDingTalkId |
| 单聊解析 | chat conversation-info | result.conversationInfo |
| 身份 | profile list | profiles[].profile/userName/corpName |
| 下载 | chat message download-media | 退出码和实际文件；即使 format=json 仍输出 INFO |
| @事件 | event consume user_im_message_receive_at --flatten | type/message_id/conversation_id/content |

list-all-conversations 在当前 help 和实际运行存在，但 leaf schema 返回 unknown；适配器记录这一差异并使用验证过的 Cobra 参数。只承认实际输出，不从源码推测运行时字段。

所有远端访问经 DWS，不调用 HTTP、不使用 shell 解释消息。账号显式固定。原生 vendor/dws(.exe) 可减少 Node 开销，Windows 不执行不明 .cmd shim。

## 采集选择

默认 hybrid：一个 @流触发即时补查，定期补齐关注会话和本人回复。普通群仍是周期增量；单群马上查询用 fresh。interval 是独立可用替代。

DWS bus 短时订阅成功，RSS 约 30 MiB，不等于整个应用的占用。事件流没有本人消息，不能仅靠它判断工作是否已处理。Collector 不做 AI 调用；后续模型分析是独立 inbox 消费者。

## 数据存储

SQLite 保存消息、变更记录、同步进度、分析确认和关注配置：

| 表 | 用途 |
|---|---|
| messages/changes | 消息和变更记录 |
| checkpoints/acknowledgements | 同步进度和分析确认 |
| settings | 身份、策略、初始化及后台状态 |
| sources | permanent/temporary/adhoc/disabled/ignored、到期、原因、稳定水位键 |
| conversations/contacts | 本地名称和 ID 解析 |
| message_index/message_fts | 本地 ref、时间／会话／发送人索引、正文搜索和 mention 分类 |
| mention_actions/context_jobs | 单次 @触发去重、有限上下文补查 |
| media/assets | 消息资源关联、内容哈希、文件路径和解读 |
| runs/batches | 有限运行记录、分析批次确认 |

关注模式变更不改变水位。历史上下文补查用独立水位，不能推进整个会话。图片不存 BLOB。JSONL 仅显式导出，不成为第二个事实源。

## @与临时关注

正文精确个人 @＋DWS @结果才确认 direct。全员 all、无法确认 unknown 均不自动扩大范围；引用和表情不计。此规则依赖当前渲染格式，不能声称支持全部昵称／富文本。

temporary 必须是已确认群聊、未禁用／忽略、消息仍在 TTL 内。期限从事件发生时间算，历史回放不续期；同一事件不重复触发。disabled 表达用户停止关注意图，优先于自动策略。

## 并发与失败

SQLite WAL；跨平台文件锁（POSIX flock / Windows msvcrt），listener 单实例锁独立，不阻塞本地读。后台线程只排空 stdout/stderr，SQLite 在主线程访问。事件队列有界，溢出安排补查。

固定每轮截止时间、按游标翻页、5 分钟重叠、消息 ID 去重。失败和未知结构不推进水位。按较旧水位优先并限制单轮时间。采集日志轮转，运行记录只留 100 次。

## 平台调度

macOS 用用户 LaunchAgent，绝对路径参数数组、原生 DWS 路径；hybrid KeepAlive，interval StartInterval。Windows 用 UTF-16 Task XML，Command/Arguments 分离和 Windows 参数编码，InteractiveToken/LeastPrivilege/IgnoreNew。

Windows stop 文件通知 Python 关闭 DWS stdin；macOS SIGTERM。工具不保存密码、不要求管理员权限。当前 Windows 有逻辑／XML 测试，未提供 Windows 实机验证。

## 参考

- [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [Apple launchd](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
- [Windows Task Scheduler repetition](https://learn.microsoft.com/en-us/windows/win32/taskschd/repeating-a-task)

## 边界

不发送消息、不标记已读、不生成自动 AI 简报、不引入向量库或 Web 服务。资源必须由 DWS 暴露可用 mediaId。下载与理解分离。睡眠、断线、晚索引或服务端保留范围可能影响新鲜度，查询必须保留 freshness／错误信息。
