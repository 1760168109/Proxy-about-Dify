# lan · 本地代理（Claude Code ↔ Dify「岚」）

把 Dify 上的「岚」接进 Claude Code：代理在本机监听，把 Anthropic Messages 请求翻译成
Dify chat-messages，再把岚的回答（含思考与工具调用）翻译回去。

```powershell
lan        # 启动 → http://127.0.0.1:7272；改 .py 后须重启
```

机制说明见 **[架构.md](./架构.md)**；教训与守则见 **[经验.md](./经验.md)**。

---

## 安装（一次）

需要：Windows · PowerShell 7 · Python 3（勾选 Add to PATH）· Claude Code。

```powershell
cd <仓库>
pwsh -ExecutionPolicy Bypass -File .\install.ps1
```

然后**新开**终端输入 `lan`。首次会从 `.env.example` 生成 `.env`——把 `DIFY_USER_ID`
改成你自己的名字（会话与缓存按它分桶，分享时勿相同）。

可选状态栏（底栏显示按次账本）：`pwsh -File .\install.ps1 -StatusLine`。

## 接入 Claude Code

两种方式任选：

**CC Switch**：Base URL `http://127.0.0.1:7272` · API Key `app-xxxx`（岚的应用密钥）·
格式 Anthropic Messages · Routing 关 · Model **`alan`**。若使用“获取模型”，选择代理
发布的 **岚**（id `anthropic/alan`）。

**settings.json env**（无 CC Switch 时）：

```json
{
  "model": "alan",
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:7272",
    "ANTHROPIC_AUTH_TOKEN": "app-xxxx（岚的应用密钥，勿入库勿外传）",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "alan",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "alan",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "alan",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    "API_TIMEOUT_MS": "2333333",
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "2333333"
  }
}
```

日常 model 填 **`alan`** → 主档；model 名含 `haiku` → 快速档。
话里带口令 **`testandlife`** 可强制快速档冒烟（勿用短词 test）。

代理不宣称或改写 Claude Code 的上下文窗口。Dify inputs 有两道独立边界：Start 表单按
`GET /parameters` 发布的 `max_length` 计 Unicode 字符；conversation 持久变量在下一轮恢复时
受当前 Dify 路径实测的 `sys.getsizeof <= 204800` 约束。代理仅在确实降低内存时，把非 BMP
字符转换为可逆 Unicode 线缆表示，并在答复返回前还原；随后分别预检两道边界。已知越界均
返回 `400 invalid_request_error`，不裁剪、不发送，绝不伪报成会诱发 Claude Code 压缩的
`413`。参数读取失败时沿用缓存或仅对表单边界放行。

若 `Tool_invocation`、`Current_Context` 或 `History` 在线缆处理后仍超限，代理只使用 Dify
已发布的同名编号槽无损分片。仓库内的 [`岚.yml`](./岚.yml) 已完成四槽配置：
`Tool_invocation_1…4`、`History_1…4`、`Current_Context_1…4` 均为可选 Paragraph，
`max_length=233333`，并已接入 conversation variables、开始节点、变量赋值器及三个 LLM
提示词。导入 Dify 后仍须**发布**应用；发布后重启 `lan`，使五分钟参数缓存立即失效。

普通请求仍走原字段，分片请求清空原字段；所有未用编号槽每枪也会发送空串，覆盖上轮残留。
`GET /parameters` 未出现这些槽时，代理不会猜测它们存在，仍以 400 拒绝并给出接线提示。
已经因旧持久变量超限而无法恢复的 conversation 不能原地自愈；发布后须通过
`POST /sessions/new` 解绑一次，或改用新的 Claude Code 会话，让完整请求建立新的 Dify
conversation。

### 长任务与自动重试

机制见 [架构.md](./架构.md)「长任务、断线与同枪重试」。此处只讲要配什么：

Claude Code 的字节空闲看门狗默认约 5 分钟，且独立于总 API timeout，故 6–10 分钟的工作流
须把上表 JSON 里的 `CLAUDE_STREAM_IDLE_TIMEOUT_MS` 与 `API_TIMEOUT_MS` **同时**设到大于
最长任务的值。这两个环境变量保存后会热应用到当前会话；lan 的代码改动仍须重启 lan 进程。

日志以 `req=<请求号>` 和 `flight=start|join|replay` 区分交错请求，并分别记录
`upstream done` 与 `delivery_status`——前者成功不等于 Claude Code 已收到。

## 环境变量（`.env`）

| 键 | 默认 | 含义 |
|:---|:---|:---|
| `DIFY_BASE_URL` | `https://api.dify.ai/v1` | 上游 |
| `DIFY_USER_ID` | `Liu Sheng` | Dify user + 本地状态分桶 |
| `DIFY_API_KEY` | 空 | 兜底 Key（优先用请求头） |
| `HOST` / `PORT` | `127.0.0.1` / `7272` | 监听 |
| `ADMIN_TOKEN` | 空（仅本机免 token） | 外部监听时保护会话、账本重置和调试管理端点 |
| `LOG_REQUESTS` | `1` | 每枪落盘 `data/request_logs/`（只留最近 200 份） |
| `TOOL_STRUCTURED` | `0` | 结构化工具出口的出站注入；重开条件见 [经验.md](./经验.md) Backlog |
| `OPUS_USD_PER_CALL` / `HAIKU_USD_PER_CALL` | `1.0` / `0.0` | 按次单价 |

本地状态固定在 `data/`：`sessions.json`（CC session ↔ Dify conversation 的映射）·
`usage.json`（账本）· `read_cache.json`（Read 缓存）· `terminal_pending.json`
（Write/Edit 待决成功答复）· `request_logs/`（每枪日志）。均有上界，不会无限增长。
`.env` 与 `data/` 勿入库。

## 端点

| Path | 作用 |
|:---|:---|
| `POST /v1/messages` | 主业务（Anthropic Messages 门面） |
| `GET /health` | 存活 + 开关状态 + 模型身份 + terminal 待决数 + 非流共享任务数 |
| `GET /v1/usage` · `…/statusline` · `POST …/reset` | 按次账本 |
| `GET/POST /sessions` · `/sessions/new` · `/sessions/switch` | 会话绑定管理 |
| `GET /debug/last-request` | 最近一枪（排障首选） |
| `GET /v1/models` | 模型发现（首项 `anthropic/alan`，旧别名兼容保留） |

## 验收（改动后最小回归）

1. `python -m pytest -q` → 全绿
2. `GET /health` → ok
3. 主聊一句 → 思考 + 正文；日志 `route=opus`、`attach_main=true`
4. 同时读取两个互不依赖的文件 → 首枪日志 `tool_count=2`；同文件连续 Edit 或 Write 后测试须串行
5. 让阿岚写一个**长文件**（内容故意含 `"引号"`、`C:\路径`、代码围栏）→ 落盘完整；
   日志 `stop_reason=tool_use`、`envelope=true`（结构化出口）或 `tool_inputs[].input_head`
   出现该文件路径（文本协议）。`input_head` 是截断头部——每项 400 字符、最多 8 项，
   验收看的是落盘文件完整，不是日志字段完整
6. 若该 Write 是任务终点 → 结果枪日志 `gun_kind=terminal_local`、`skipped_dify=true`，usage
   不增加；故意拒绝写入 → 回 Dify 正常解释并计续写枪
7. 新开 CC 对话再聊 → 日志 `session_bind=miss` 且不带旧 `conversation_id_out`；同窗续聊 → `hit`
8. `/compact` → `gun_kind=compact`、`route=opus`（压缩质量优先）、`attach_main=false`、`trim=empty`
9. 贴图提问 → `dify_files≥1`（岚须允许 image 上传）；失败须见 `[[cc_images:failed]]`
10. 并发发送两个完全相同的非流请求 → 日志一条 `start`、一条 `join`，Dify 与 usage 都只增 1
11. `GET /v1/usage` 次数与实际进入 Dify 的枪数对得上
12. 删除编码、兼容、边界、重试、缓存或恢复机制前 → 按 [经验.md](./经验.md) 守则 22
    建立故障复现与反事实对照；证据不足只登记候选，不直接删除

排障链：UI 现象 → `GET /debug/last-request` → 最新 `data/request_logs/*.json`
（`summary` 看路由与出站，`response` 看 `stop_reason` / `envelope` / `workflow_error`）。

外部监听（`HOST` 非 `127.0.0.1`）时必须配置 `ADMIN_TOKEN`；它保护会话管理、账本重置
和调试端点。调试端点只返回日志摘要，原始请求正文默认脱敏。

## 代码地图

| 文件 | 职责 |
|:---|:---|
| `main.py` | HTTP 入口与编排 |
| `protocol.py` | 模型身份发布与兼容别名 |
| `plan.py` | 判枪：旁路 / 子代理 / 路由 / 流式 / 结构化 |
| `parse.py` | CC 请求折叠 → inputs / query / History；图抽取 |
| `outbound.py` | 出站装配：缓存重放、标记、注入、附图 |
| `unicode_wire.py` | 非 BMP 可逆线缆、流式还原与 Dify 持久变量内存边界 |
| `singleflight.py` | 非流请求指纹、并发合并、断线续跑与短期结果回放 |
| `tools.py` | 工具通道：协议文本、目录、三通道解析、归一 |
| `answer.py` | Dify 流 → Anthropic SSE / JSON |
| `dify.py` | 上游 I/O：应用参数缓存、chat-messages、图上传 |
| `岚.yml` | Dify「岚」的可导入 DSL；含三组四槽持久变量分片完整接线 |
| `terminal.py` | terminal-tool 待决状态、成功判定、会话隔离与过期 |
| `cache.py` / `sessions.py` / `meter.py` / `log.py` | Read 缓存 / 会话绑定 / 按次账 / 落盘与日志字段表 |
| `persist.py` | 本地状态共用原语：原子写盘、JSON 装载容错、UTC 时间约定 |
