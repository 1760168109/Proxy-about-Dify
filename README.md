# lan · 本地代理（Claude Code ↔ Dify「岚」）

把 Dify 上的「岚」接进 Claude Code：代理在本机监听，把 Anthropic Messages 请求翻译成
Dify chat-messages，再把岚的回答（含思考与工具调用）翻译回去。

```powershell
lan        # 启动 → http://127.0.0.1:7272；改 .py 后须重启
```

机制说明见 **[架构.md](./架构.md)**；教训与守则见 **[经验.md](./经验.md)**。

---

## 安装（一次）

需要：Windows · Python 3（勾选 Add to PATH）· Claude Code。

```powershell
cd <仓库>/proxy
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

然后**新开**终端输入 `lan`。首次会从 `.env.example` 生成 `.env`——把 `DIFY_USER_ID`
改成你自己的名字（会话与缓存按它分桶，分享时勿相同）。

可选状态栏（底栏显示按次账本）：`install.ps1 -StatusLine`。

## 接入 Claude Code

两种方式任选：

**CC Switch**：Base URL `http://127.0.0.1:7272` · API Key `app-xxxx`（岚的应用密钥）·
格式 Anthropic Messages · Routing 关。

**settings.json env**（无 CC Switch 时）：

```json
"ANTHROPIC_BASE_URL": "http://127.0.0.1:7272",
"ANTHROPIC_AUTH_TOKEN": "app-xxxx（岚的应用密钥，勿入库勿外传）",
"ANTHROPIC_DEFAULT_OPUS_MODEL": "alan[1M]"
```

日常 model 填 **`alan`** → 主档；model 名含 `haiku` → 快速档。
话里带口令 **`testandlife`** 可强制快速档冒烟（勿用短词 test）。

## 环境变量（`.env`）

| 键 | 默认 | 含义 |
|:---|:---|:---|
| `DIFY_BASE_URL` | `https://api.dify.ai/v1` | 上游 |
| `DIFY_USER_ID` | `Liu Sheng` | Dify user + 本地状态分桶 |
| `DIFY_API_KEY` | 空 | 兜底 Key（优先用请求头） |
| `HOST` / `PORT` | `127.0.0.1` / `7272` | 监听 |
| `LOG_REQUESTS` | `1` | 每枪落盘 `data/request_logs/` |
| `TOOL_STRUCTURED` | `0` | 结构化工具出口（当前插件 prefill 与 opus-4-6 不兼容；插件改用 tool 模式后再验证） |
| `OPUS_USD_PER_CALL` / `HAIKU_USD_PER_CALL` | `1.0` / `0.0` | 按次单价 |

本地状态固定在 `data/`：`sessions.json`（会话绑定）· `usage.json`（账本）·
`read_cache.json`（Read 缓存）· `terminal_pending.json`（Write/Edit 待决成功答复）·
`request_logs/`（每枪日志）。`.env` 与 `data/` 勿入库。

## 端点

| Path | 作用 |
|:---|:---|
| `POST /v1/messages` | 主业务（Anthropic Messages 门面） |
| `GET /health` | 存活 + 开关状态 + terminal 待决数 |
| `GET /v1/usage` · `…/statusline` · `POST …/reset` | 按次账本 |
| `GET/POST /sessions` · `/sessions/new` · `/sessions/switch` | 会话绑定管理 |
| `GET /debug/last-request` | 最近一枪（排障首选） |
| `GET /v1/models` | 模型发现（alan / dify-lan / lan） |

## 验收（改动后最小回归）

1. `python -m pytest -q` → 全绿
2. `GET /health` → ok
3. 主聊一句 → 思考 + 正文；日志 `route=opus`、`attach_main=true`
4. 同时读取两个互不依赖的文件 → 首枪日志 `tool_count=2`；同文件连续 Edit 或 Write 后测试须串行
5. 让阿岚写一个**长文件**（内容故意含 `"引号"`、`C:\路径`、代码围栏）→ 落盘完整；
   日志 `stop_reason=tool_use`、`envelope=true`（结构化出口）或 tool_inputs 完整（文本协议）
6. 若该 Write 是任务终点 → 结果枪日志 `gun_kind=terminal_local`、`skipped_dify=true`，usage
   不增加；故意拒绝写入 → 回 Dify 正常解释并计续写枪
7. 新开 CC 对话再聊 → 日志 `session_bind=miss` 且不带旧 `conversation_id_out`；同窗续聊 → `hit`
8. `/compact` → `gun_kind=compact`、haiku、不续主会话
9. 贴图提问 → `dify_files≥1`（岚须允许 image 上传）；失败须见 `[[cc_images:failed]]`
10. `GET /v1/usage` 次数与实际进入 Dify 的枪数对得上

排障链：UI 现象 → `GET /debug/last-request` → 最新 `data/request_logs/*.json`
（`summary` 看路由与出站，`response` 看 `stop_reason` / `envelope` / `workflow_error`）。

## 代码地图

| 文件 | 职责 |
|:---|:---|
| `main.py` | HTTP 入口与编排 |
| `plan.py` | 判枪：旁路 / 子代理 / 路由 / 流式 / 结构化 |
| `parse.py` | CC 请求折叠 → inputs / query / History；图抽取 |
| `outbound.py` | 出站装配：缓存重放、标记、注入、附图 |
| `tools.py` | 工具通道：协议文本、目录、三通道解析、归一 |
| `answer.py` | Dify 流 → Anthropic SSE / JSON |
| `dify.py` | 上游 I/O：chat-messages、图上传 |
| `terminal.py` | terminal-tool 待决状态、成功判定、会话隔离与过期 |
| `cache.py` / `sessions.py` / `meter.py` / `log.py` | Read 缓存 / 会话绑定 / 按次账 / 落盘 |
| `persist.py` | 本地状态共用原语：原子写盘、UTC 时间戳 |
