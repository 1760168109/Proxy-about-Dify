# lan · 本地代理（Claude Code ↔ Dify「岚」）

lan 在本机监听 Anthropic Messages 请求，将其转换为 Dify `chat-messages`，再把岚的回答、思考与工具调用转换回 Claude Code。工具执行、权限与文件读写仍由 Claude Code 负责。

```powershell
lan        # 启动 → http://127.0.0.1:7272；运行窗口须保持开启
```

系统机制与当前契约见 **[架构.md](./架构.md)**；故障教训、保护门与待办见 **[经验.md](./经验.md)**。

## 第一次安装

### 准备与取得文件

需要 Windows、PowerShell 7、Python 3 和 Claude Code：

- 在终端输入 `pwsh` 可检查 PowerShell 7；找不到命令时先从 Microsoft Store 安装，随后重开终端。
- 安装 Python 3 时须勾选 **Add Python to PATH**。
- 若取得的是 GitHub 仓库，使用 **Code → Download ZIP** 下载；若取得的是别人发送的 ZIP，直接解压即可。
- 建议解压到简单路径，例如 `C:\lan-proxy`，并进入真正包含 `install.ps1`、`lan.cmd`、`main.py`、`requirements.txt` 与 `.env.example` 的那一层。

上游的 `app-xxxx` 是额度凭据，不要转发或随截图外传。

### 执行安装

在上述目录中打开终端，运行：

```powershell
pwsh -ExecutionPolicy Bypass -File .\install.ps1
```

必须使用 PowerShell 7 的 `pwsh`，不要替换成旧版 `powershell`。安装成功时会看到类似：

```text
Installed.
1) Open a NEW PowerShell 7 window
2) Type: lan
3) Keep the window open while chatting
URL: http://127.0.0.1:7272
```

安装后新开终端。首次安装会从 `.env.example` 生成 `.env`；用文本编辑器将
`DIFY_USER_ID=Liu Sheng` 改成自己的名字。该值用于 Dify user、本地会话与缓存分桶，分享给多人时不可共用；使用 CC Switch 时，`.env` 中的 `DIFY_API_KEY` 可以留空。

安装脚本还会把 `SubagentStart` / `SubagentStop` 两个 Claude Code hook 合并进
`%USERPROFILE%\.claude\settings.json`。它只替换本代理自己的 `claude_hook.py` 条目，保留已有 hooks、env 和权限；hook 故障会 fail-open，不阻断 Claude Code。

可选状态栏（底栏显示按次账本）：

```powershell
pwsh -File .\install.ps1 -StatusLine
```

## 接入 Claude Code

两种方式任选其一。

### CC Switch

```text
Base URL: http://127.0.0.1:7272
API Key: app-xxxx（岚的应用密钥）
格式: Anthropic Messages
Routing: 关闭
Model: alan
```

若使用“获取模型”，选择显示为“岚”的 `anthropic/alan`。

CC Switch 不代替 Claude Code 自身的等待设置。在 Claude Code 的 `settings.json` 的 `env` 中同时保留：

```json
"API_TIMEOUT_MS": "2333333",
"CLAUDE_STREAM_IDLE_TIMEOUT_MS": "2333333"
```

### `settings.json` 环境变量

不使用 CC Switch 时配置：

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

日常 model 填 `alan` 走主档；model 名含 `haiku` 走快速档。话里带口令 `testandlife` 可强制快速档冒烟，勿用短词 `test`。

## 日常使用

1. 新开 PowerShell 7 或终端，输入 `lan`。
2. 保持 lan 窗口开启。
3. 打开 Claude Code，选择或填写模型 `alan`，正常工作。
4. 任务结束后先退出 Claude Code，再关闭 lan 窗口。

可在浏览器打开 `http://127.0.0.1:7272/health` 检查本机服务。它只证明 lan 能响应，不证明密钥、Dify 已发布应用或上游模型可用。

子代理的动作状态由 lan 本地生成，不访问 Dify；真正的审视、分析请求仍走模型链。主会话与子代理分别绑定 Dify conversation，详细身份与完成报告边界见 [架构.md](./架构.md)「子代理身份、CID 与报告」。

## 输入边界与 Dify 部署

lan 不宣称或改写 Claude Code 的上下文窗口。Dify inputs 有两道不同边界：Start 表单按已发布的 `/parameters.max_length` 计 Unicode 字符；conversation 持久变量在下一轮恢复时另受当前路径实测的 `sys.getsizeof <= 204800` 约束。

代理只在确实降低内存时使用可逆 Unicode 线缆，并只向 `/parameters` 已发布的同名编号槽无损分片。已知越界返回 Anthropic 形状的 `400 invalid_request_error`，不裁剪、不发送、不计费，也不伪报为会诱发 Claude Code 压缩的 `413`。完整契约见 [架构.md](./架构.md)「Dify 两道输入边界」与「单变量无损分片」。

本机私有的 `岚.yml` 承载 Dify 部署基线，不随仓库分发。DSL 更新后仍须在 Dify 导入并**发布**，再重启 lan 使参数缓存失效；代理不会猜测未发布的编号槽存在。已经被旧持久变量卡住的会话不能原地自愈，发布后须 `POST /sessions/new` 解绑一次，或新开 Claude Code 会话。

## 长任务与传输取证

Claude Code 的流式空闲看门狗独立于总 API timeout。只有轨迹证明确有长时间无事件空窗时，才按实测最长空窗设置 `CLAUDE_STREAM_IDLE_TIMEOUT_MS`，并让 `API_TIMEOUT_MS` 覆盖总时长；不要把固定的“五分钟”当成当前版本结论。

日志以 `req=<请求号>` 和 `flight=start|join|replay` 区分请求。每份请求 JSON 旁的同名 `.trace.jsonl` 记录 Dify 首末事件、异常、Anthropic SSE 事件类型与 ASGI 发送结果，不保存 SSE data 正文或鉴权头。`GET /debug/last-request` 附带最近 40 条轨迹；完整文件名见 `summary.transport_trace_file`。请求摘要仍可能包含短路径、query 或回答头部，分享前须检查。

机制说明见 [架构.md](./架构.md)「长任务、断线与同枪重试」。修改 `.py` 后必须重启运行中的 lan；保存 Claude Code 的超时配置不要求重启 lan。

## 常见问题

### 输入 `lan` 后提示找不到命令

安装后先关闭旧终端并新开 PowerShell 7。仍不行时进入代理目录运行：

```powershell
.\lan.cmd
```

### 提示 `python not found`

重新安装或修复 Python 3，勾选 **Add Python to PATH**，随后重开终端。

### Claude Code 连接失败

依次检查：lan 窗口仍开启、`/health` 可访问、Base URL 为 `http://127.0.0.1:7272`、API Key 正确、CC Switch 的 Routing 已关闭、Model 为 `alan` 或 `anthropic/alan`。

### 回答串到旧会话

先检查 `.env` 的 `DIFY_USER_ID` 是否与其他使用者重名。需要放弃旧 conversation 时使用 `POST /sessions/new`，或新开 Claude Code 会话。

### 长任务中途断开

先查对应请求的 `.trace.jsonl`，区分上游无事件、代理未转译与下游发送失败；不要只凭经过时长判断。Dify 已接受的任务即使下游断开仍可能完成并产生额度消耗。

### 子代理显示完成，主模型仍说在运行

检查请求日志中的 `agent_report_source`、`agent_archive_reports` 与 `hook_identity_status`。可信完成通知优先来自消息链；fork 后缺失旧 transcript 时，lan 才从有界档案恢复一次。身份缺失或无效会回落普通模型链，详细规则见 [架构.md](./架构.md) 对应章节。

### 向柳生反馈错误

优先提供 Claude Code 报错截图、lan 终端截图，以及 `http://127.0.0.1:7272/debug/last-request` 的页面截图。该页面隐藏原始请求正文和鉴权信息，但摘要可能含短路径、query 或回答头部，发送前仍须检查。

## 配置与端点

### `.env`

| 键 | 默认 | 含义 |
|:---|:---|:---|
| `DIFY_BASE_URL` | `https://api.dify.ai/v1` | 上游 |
| `DIFY_USER_ID` | `Liu Sheng` | Dify user + 本地状态分桶；必须改为使用者自己的名字 |
| `DIFY_API_KEY` | 空 | 兜底 Key（优先用请求头） |
| `HOST` / `PORT` | `127.0.0.1` / `7272` | 监听 |
| `ADMIN_TOKEN` | 空（仅本机免 token） | 外部监听时保护会话、账本重置和调试管理端点 |
| `LOG_REQUESTS` | `1` | 每枪落盘 `data/request_logs/`（只留最近 200 份） |
| `TOOL_STRUCTURED` | `0` | 结构化工具出口的出站注入；重开条件见 [经验.md](./经验.md) Backlog |
| `OPUS_USD_PER_CALL` / `HAIKU_USD_PER_CALL` | `1.0` / `0.0` | 按次单价 |
| `LAN_HOOK_BASE_URL` | `http://127.0.0.1:7272` | Claude Code hook 回调地址 |

`.env` 与 `data/` 不入库。`data/` 保存有界的会话、账本、Read 缓存、terminal 待决、子代理档案与请求日志；它们是内部状态，不是稳定 API，结构见 [架构.md](./架构.md)「落盘」。

### HTTP 端点

| Path | 作用 |
|:---|:---|
| `POST /v1/messages` | 主业务（Anthropic Messages 门面） |
| `GET /health` | 存活、开关、模型身份及内部计数摘要 |
| `GET /v1/models` | 模型发现（首项 `anthropic/alan`，保留旧别名兼容） |
| `GET /v1/usage` · `…/statusline` · `POST …/reset` | 按次账本 |
| `GET/POST /sessions` · `/sessions/new` · `/sessions/switch` | 会话绑定管理 |
| `POST /hooks/subagent-start` · `/hooks/subagent-stop` | Claude Code 子代理 hook |
| `GET /debug/last-request` | 最近一枪的脱敏摘要与传输轨迹 |

外部监听（`HOST` 非 `127.0.0.1`）时必须配置 `ADMIN_TOKEN`；它保护会话管理、账本重置和调试端点。

## 维护入口

自动回归入口：

```powershell
python -m pytest -q
```

具体端到端验收按受影响契约选择，不在 README 复制完整机制清单。修改代码前先读 [架构.md](./架构.md) 的对应控制链与 [经验.md](./经验.md) 的保护原因；删除编码、兼容、边界、重试、缓存、会话或恢复机制前，必须先完成证据链。`GET /health` 不是上游联调证明，真实 Dify / Claude Code 验证会消耗额度或改变外部状态，须由任务明确授权。
