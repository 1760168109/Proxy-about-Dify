# AGENTS.md

## 作用域与事实源

本文件适用于整个仓库。根目录是 `lan` 本地代理本体；`借鉴/仓库/` 是下载的外部参考源码，不是本体的子模块或运行依赖。外部仓库自己的 `AGENTS.md` 只在明确研究该来源时说明其内部结构，不能把外部项目的命令、约定或产品边界带回 lan。

判断当前行为时，以代码和对应测试为事实源。文档分工如下：

- `README.md`：首次安装、接入、日常使用、排障、配置与维护入口；
- `架构.md`：跨模块机制、协议契约和状态边界；
- `经验.md`：真实故障换来的保护门及尚未执行的 Backlog。

实现与文档冲突时先核对代码和测试，再修订负责该事实的文档。`远期备案`、`Backlog` 和其他标为“未执行”的内容不是当前能力，不得按现状实现或描述。

## 系统边界与主链

lan 是供 Claude Code / CC Switch 使用的本地 FastAPI 模型门面：对客户端暴露 Anthropic Messages 形态，把请求转换为 Dify `chat-messages`，再把 Dify 事件转换回 Anthropic SSE 或 JSON。Dify 只提供模型工作流；工具执行、权限、UI 和文件读取仍由 Claude Code 负责。一次用户意图经过多枪 HTTP 和 `tool_result` 续写是正常工具环，不要把它误压成一次请求。

主业务顺序是：入口验签并剥除 hook marker，`plan.build_plan` 判枪，主窗口查询 terminal 待决，`parse.parse_payload` 折叠消息，`outbound` 物化 inputs、注入工具协议、处理线缆与分片，`dify` 调用上游，`answer` 译流并收尾。`main.py` 负责 HTTP 与编排；不要把判枪、消息折叠、出站协议或答复解析的新真源重新堆回入口。

## 不能拆开的契约

### 枪型、路由与会话

- `kind`、`route`、`attachment_scope`、`enable_tools` 各有独立来源。枪型决定旁路、inputs 形态和会话副作用；路由只决定模型档。不得由 `route` 反推主窗口、旁路或会话附着。
- 只有 `attachment_scope=main` 使用 `by_cc[session_id]`；可信子代理使用独立的 `by_agent[parent][agent]`；`none` 不查会话。工作流失败或 fence 已变化时不得写入 CID。
- 子代理身份只来自 `SubagentStart` hook 登记并验签的 transport marker。marker 缺失、无效、歧义或父 session 不符时，身份绑定 fail-closed，但普通模型链仍可 fail-open；不得从 prompt、文本哈希或请求顺序猜身份。
- 判枪必须先于 terminal 查询。terminal 仅在同一主会话、同一批显式 Write/Edit、全部 tool id 精确匹配且结果命中已知成功形态时释放 `after_success`；任何不确定都回到 Dify。

### 请求、上下文与工具

- 对客户端是否流式，`body.stream` 优先，省略时才看 `Accept: text/event-stream`，否则返回 JSON。Dify 恒 streaming 不得改变客户端契约。
- 主枪的 13 个 `INPUT_KEYS` 必须全键物化；空串用于覆盖 Dify conversation variables 的旧值，不是缺失值。
- 正文只归一个逻辑载体：当前 `tool_result` 在 query，本轮 user 后的预载在 `Current_Context`，既往仍有效工具正文在 `Tool_invocation`，`History` 只留引用。不要为“保险”复制到多个字段；编号槽只能承载同名基键的连续分片。
- `@路径` 不代表代理读取文件。正文只能来自 Claude Code 的预读 system 轨迹或 Read 的 `tool_result`；文档不进入 Dify `files[]`，只有 image 使用上传通道。
- Dify 不透传 Anthropic `tools[]`。工具目录和协议由 `outbound.py` 注入，`answer.py` / `tools.py` 解析后交 Claude Code 执行；解析失败的可执行块必须保留为可见正文，不能生成半个调用或静默丢弃。

### 输入边界与传输事实

- Start 表单 `max_length` 按 Unicode 字符计，conversation variable 的恢复边界按当前实测 `sys.getsizeof <= 204800` 计；两者不是 HTTP body 限制。已知越界返回 Anthropic 形状的 `400 invalid_request_error`，不裁剪、不发送、不计费，绝不能改成会诱发 Claude Code 压缩的 `413`。
- 非 BMP 线缆、字面量转义、模型侧声明、正文/思考/工具参数还原、持久变量预检是一个跨模块契约。分片只使用 `/parameters` 已发布的 `Tool_invocation`、`History`、`Current_Context` 同名编号槽，并在每枪清空未用槽；没有已发布槽时不得猜测。
- Dify 接受 HTTP 流时才计一枪；本地短路、已知边界拒绝及 single-flight 的 join/replay 不新增上游调用。非流等待者断开不得取消共享任务。
- Dify 终端事件、lan 生成 `message_stop`、ASGI `send` 成功、Claude Code 实际消费是四层不同事实。排障按此前三层顺序取证，不得用任一层替代下一层。

## 持久状态、隐私与外部副作用

- `.env`、`岚.yml`、`岚-CC switch.md`、`data/` 与 `借鉴/仓库/` 已被 Git 忽略。默认不要读取或改写这些本机文件；需要真实配置、请求正文或运行数据时，须由当前任务明确授权。使用 `.env.example` 了解公开配置形状。
- `data/*.json` 是有界、原子写入的内部状态，不是稳定 API。不要让新代码依赖其偶然字段，也不要在测试中写入用户的真实账本、会话、缓存、terminal 或日志。
- `岚.yml` 是本机私有的 Dify 部署基线，不代表 Cloud 草稿已经发布。涉及输入槽、conversation variables、LLM 提示或节点接线的改动，必须把“本地代码完成”“DSL 已更新”“Dify 已导入并发布”分开报告。
- `install.ps1` 要求 PowerShell 7，并会安装 Python 包、修改用户 PATH、创建 `.env`、合并 Claude Code hooks，还可改写用户的 statusLine；只在安装或更新本机接入被明确要求时用 `pwsh` 运行。`lan-stop.cmd` 会强制停止占用 7272 或命令行含 `main.py` 的进程，也不得作为普通验证步骤。
- 真实 Dify 调用可能消耗额度；连接 Dify、Claude Code 或修改已发布应用属于外部状态操作，只有任务明确需要时才执行。
- 请求日志可能包含原始请求与短路径/query/回答摘要。不得把日志正文、鉴权头、用户 transcript 或私有路径复制到文档、测试夹具或回复中。

## 修改与验证边界

- 自动回归入口是 `python -m pytest -q`。按受影响契约选择对应测试；涉及输入边界、Unicode、分片、交付状态、路径身份或端点行为时，同时检查 `tests/test_precepts.py` 的端点级保护。
- 端点测试必须使用 `tests/conftest.py` 的 `isolated_main` 或提供等价的完整隔离。`main` 在模块导入时绑定真实 store、meter、cache、terminal 和日志路径；漏隔离会静默写入用户的 `data/`，还可能访问真实 Dify。
- 删除、替换、合并或短路编码、兼容、边界、重试、缓存、会话或恢复机制前，先按 `触发条件 → 所防故障 → 外部行为 → 当前实现 → 测试锚点 → 等价替代 → 未证部分` 建立证据链。证据不足时只登记候选；不能把“代码绕”当作可删除理由。
- 修改 `.py` 后，运行中的 `lan` 不会热更新；需要运行验证时必须明确重启。`GET /health` 只证明本机服务可响应，不证明密钥、Dify 应用或上游模型可用。
- 改变外部行为或承重契约时，同步更新负责该事实的 `README.md`、`架构.md` 或 `经验.md`；不要另建一次性说明来替代权威文档。

## `借鉴` 区域

`借鉴/` 保存参考仓库内化的方法、执行计划和结果；`借鉴/仓库/` 中的 GitHub ZIP 源码仅供只读研究。普通 lan 开发不得从这些目录导入代码、运行它们的安装器、修改其源码或把它们纳入本仓库测试。只有任务明确进入内化流程时，才按 `借鉴/漏斗内化.md`、项目镜面、操作手册与当轮施行计划读取指定来源，并把认识写入 `借鉴/内化结果/`，而不是外部仓库目录。
