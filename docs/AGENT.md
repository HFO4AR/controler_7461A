# Agent 接入

上位机提供两种接入方式，agent 做的设置会实时同步到网页界面：

1. **MCP 服务器**（推荐）：`mcp_server.py`，给 Claude Code / Codex / Cursor 等支持 MCP 的 agent 用。
2. **HTTP 接口**：`/api/agent/*`，给脚本或不支持 MCP 的 agent 用。

两种方式都要求上位机 `app.py` 正在运行。MCP 服务器只是把工具调用转成 HTTP 请求，不直接占用串口。

## MCP 服务器

只用 Python 标准库，不需要额外安装依赖。

**Claude Code**：项目根目录已带 `.mcp.json`，在项目目录里启动 `claude` 时会提示启用 `dmm7461a`。也可以手动注册：

```bash
claude mcp add dmm7461a -e DMM_URL=http://127.0.0.1:7461 -- python3 /home/yuki/project/controler_7461A/mcp_server.py
```

**其他客户端**（Cursor、Claude Desktop 等）的 `mcpServers` 配置：

```json
{
  "dmm7461a": {
    "command": "python3",
    "args": ["/home/yuki/project/controler_7461A/mcp_server.py"],
    "env": { "DMM_URL": "http://127.0.0.1:7461" }
  }
}
```

**Codex**（`~/.codex/config.toml`）：

```toml
[mcp_servers.dmm7461a]
command = "python3"
args = ["/home/yuki/project/controler_7461A/mcp_server.py"]
env = { DMM_URL = "http://127.0.0.1:7461" }
```

### 工具一览

| 工具 | 作用 |
|---|---|
| `dmm_status` | 在线状态、功能/量程/速率/触发、NULL、显示模式、报警、记录、最新读数、累计统计 |
| `dmm_options` | 可用的功能/量程/速率/触发代码；`include_advanced` 时附带高级设置项 |
| `dmm_read` | 最新读数；`fresh=true` 时等待一个新样本 |
| `dmm_measure` | 采集 `count` 个样本或 `seconds` 秒，返回 mean/std/min/max/pp/rms |
| `dmm_setup` | 设置 function/range/rate/trigger/display_mode/shunt_ohm；`settle` 秒后回读一次 |
| `dmm_history` | 最近一段时间的历史读数（抽稀到 `max_points`） |
| `dmm_set_limits` | 上下限报警 |
| `dmm_null` | NULL 相对测量：on / off / from_latest / set_const |
| `dmm_reset_stats` | 重置累计统计 |
| `dmm_log` | CSV 记录：start / stop / list |
| `dmm_log_read` | 读 CSV 记录文件的最后若干行 |
| `dmm_advanced_read` / `dmm_advanced_set` / `dmm_advanced_action` | 高级设置（积分时间、显示位数、自动调零、触发参数等） |
| `dmm_reconnect` | 强制重连仪器 |

agent 可以直接这样用：「把万用表切到四线电阻，等 1 秒，测 50 个点告诉我平均值和标准差」。

## HTTP 接口

所有接口都返回 JSON。出错时返回 `{"ok": false, "error": "...", "code": "..."}`，`code` 可能是 `offline`、`busy`、`timeout`、`local_mode`、`bad_value`、`bad_request`、`mode_conflict` 或 `no_data`。

### `GET /api/agent`

自描述清单：列出所有接口、可选参数值和注意事项。agent 第一次接入时先读这个。

### `GET /api/agent/status`

返回设备、设置、报警、记录状态、最新读数和累计统计。

### `GET /api/agent/reading?fresh=1&timeout=5`

返回最新读数。`fresh=1` 时等待一个新样本。

```json
{"ok": true, "reading": {"value": 0.0248, "unit": "V", "overload": false,
  "function": "DCV", "range": "AUTO", "rate": "PR3", "alarm": "off", "age_s": 0.03, "...": "..."}}
```

`value` 是显示值：分流电流模式下是电流（A）。过载时 `overload=true`，`value=null`。

### `POST /api/agent/setup`

只修改请求里给出的字段，顺序为显示模式 → 功能 → 量程 → 速率 → 触发。

```json
{"function": "RES4W", "range": "AUTO", "rate": "PR4", "settle": 1.0}
```

- `display_mode` 取 `voltage` 或 `current`。`current` 会强制切到 DCV，此时请求非 DCV 的 `function` 会返回 409。
- `shunt_ohm` 是分流电阻，范围 1e-6 到 1e6 Ω。
- `settle` 大于 0 时，等待该秒数后在 `reading` 里返回一个新读数。

### `POST /api/agent/measure`

采集新样本并返回统计。

```json
{"count": 50, "settle": 0.5, "include_samples": false}
```

- `count` 为 1 到 5000 个样本。也可以改用 `seconds`（0.05 到 120 秒）。两者都不给时，默认采 10 个样本。
- `timeout` 是最长等待时间。超时会返回已收到的样本，同时 `complete=false` 并带上 `warning`。
- 如果采集途中功能或单位发生变化，只统计变化后的样本。
- 返回内容包括 `summary`（n、n_ol、mean、std、min、max、pp、rms、unit）、`sample_rate_hz` 和 `last`；`include_samples=true` 时还会返回 `samples`。

### 其他接口

网页使用的接口同样可以调用：

- `/api/history`、`/api/limits`、`/api/stats/reset`
- `/api/log/start`、`/api/log/stop`、`/api/logs`、`/api/logs/<name>/download`
- `/api/advanced`、`/api/advanced/read`、`/api/advanced/set`、`/api/advanced/action`
- `/api/device/reconnect`
- `/api/stream`（SSE 实时推送）

### curl 示例

```bash
curl -s localhost:7461/api/agent/setup -H 'Content-Type: application/json' \
     -d '{"function":"DCV","range":"AUTO","settle":0.5}'
curl -s localhost:7461/api/agent/measure -H 'Content-Type: application/json' -d '{"count":20}'
```

## 安全

这些接口**没有鉴权**。默认只监听 127.0.0.1，只有本机能访问。如果用 `HOST=0.0.0.0` 开放到局域网，同一网络里的任何人都能控制仪器，包括复位仪器（`rst`）。
