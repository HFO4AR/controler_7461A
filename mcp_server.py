#!/usr/bin/env python3
"""ADCMT 7461A 上位机的 MCP 服务器（stdio，零依赖）。

让 Claude Code / Codex / Cursor 等支持 MCP 的 agent 控制万用表并读取数据。
它不直接占用串口，而是调用正在运行的上位机 HTTP 接口，因此网页界面会同步显示
agent 做的每一个设置。

用法：
    1. 先启动上位机：.venv/bin/python app.py
    2. 在 agent 里注册本服务器（项目根目录已带 .mcp.json，Claude Code 会自动发现）：
           claude mcp add dmm7461a -- python3 /path/to/mcp_server.py
环境变量：
    DMM_URL   上位机地址，默认 http://127.0.0.1:7461
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("DMM_URL", "http://127.0.0.1:7461").rstrip("/")
SERVER_INFO = {"name": "dmm7461a", "version": "1.0.0"}
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "控制 ADCMT 7461A 数字万用表。建议流程：dmm_status 查看当前状态 → "
    "dmm_setup 设置功能/量程/速率（可带 settle 等待稳定）→ dmm_measure 采集若干样本并取统计。"
    "可选值用 dmm_options 查询。分流电流模式（display_mode=current）只能用 DCV。"
    "所有设置会实时显示在网页上位机上。"
)


class ApiError(Exception):
    pass


def http(method: str, path: str, body: dict | None = None, query: dict | None = None,
         timeout: float = 30.0, raw: bool = False):
    url = BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            err = json.loads(text)
            msg = f"{err.get('error', text)}（code={err.get('code', exc.code)}）"
        except ValueError:
            msg = f"HTTP {exc.code}: {text[:300]}"
        raise ApiError(msg) from None
    except (urllib.error.URLError, OSError) as exc:
        raise ApiError(f"无法连接上位机 {BASE_URL}（{exc}）。请先运行 app.py，"
                       "或用 DMM_URL 指定地址。") from None
    if raw:
        return payload.decode("utf-8", "replace")
    return json.loads(payload.decode("utf-8"))


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def _num(v, lo=None):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else lo


def t_status(_):
    return http("GET", "/api/agent/status")


def t_options(args):
    meta = http("GET", "/api/meta")
    out = {k: meta[k] for k in ("functions", "ranges", "rates", "triggers", "display_modes")}
    if args.get("include_advanced"):
        groups = http("GET", "/api/advanced")["groups"]
        out["advanced"] = [
            {"group": g["label"], "items": [
                {k: it.get(k) for k in ("key", "label", "kind", "unit", "min", "max",
                                         "hint", "options", "applies") if it.get(k) is not None}
                for it in g["items"]]}
            for g in groups
        ]
    return out


def t_read(args):
    timeout = _num(args.get("timeout"), 5)
    q = {"fresh": "1" if args.get("fresh") else "0", "timeout": timeout}
    return http("GET", "/api/agent/reading", query=q, timeout=timeout + 10)


def t_measure(args):
    body = {k: args[k] for k in ("count", "seconds", "settle", "timeout", "include_samples")
            if k in args}
    wait = (_num(args.get("timeout"), 0) or 0) + (_num(args.get("seconds"), 0) or 0) \
        + (_num(args.get("settle"), 0) or 0) + (_num(args.get("count"), 10) or 10) * 2 + 30
    return http("POST", "/api/agent/measure", body, timeout=min(wait, 400))


def t_setup(args):
    body = {k: args[k] for k in ("function", "range", "rate", "trigger", "display_mode",
                                 "shunt_ohm", "cont_threshold", "settle") if k in args}
    if not body:
        raise ApiError("至少给出一个要设置的参数")
    return http("POST", "/api/agent/setup", body, timeout=(_num(args.get("settle"), 0) or 0) + 40)


def t_history(args):
    seconds = _num(args.get("seconds"), 60)
    max_points = int(_num(args.get("max_points"), 500))
    data = http("GET", "/api/history", query={"seconds": seconds}, timeout=30)
    samples = data.get("samples", [])
    total = len(samples)
    if max_points > 0 and total > max_points:
        step = total / max_points
        samples = [samples[int(i * step)] for i in range(max_points - 1)] + [samples[-1]]
    unit = samples[-1]["u"] if samples else None
    return {
        "context_id": data.get("context_id"),
        "unit": unit,
        "function": samples[-1]["fn"] if samples else None,
        "total_samples": total,
        "returned": len(samples),
        "columns": ["t", "value", "overload", "alarm"],
        "rows": [[s["t"], None if s.get("ol") else s.get("d"), bool(s.get("ol")), s.get("alarm")]
                 for s in samples],
    }


def t_limits(args):
    body = {"mode": args.get("mode"), "enabled": bool(args.get("enabled")),
            "low": args.get("low"), "high": args.get("high")}
    return http("POST", "/api/limits", body)


def t_null(args):
    action = args.get("action")
    if action == "on":
        return http("POST", "/api/advanced/set", {"key": "null", "value": "NL1"})
    if action == "off":
        return http("POST", "/api/advanced/set", {"key": "null", "value": "NL0"})
    if action == "from_latest":
        return http("POST", "/api/advanced/action", {"key": "null_const"})
    if action == "set_const":
        if _num(args.get("value")) is None:
            raise ApiError("set_const 需要 value")
        return http("POST", "/api/advanced/set", {"key": "null_const", "value": args["value"]})
    raise ApiError("action 只能是 on / off / from_latest / set_const")


def t_reset_stats(_):
    return http("POST", "/api/stats/reset")


def t_log(args):
    action = args.get("action")
    if action == "start":
        return http("POST", "/api/log/start")
    if action == "stop":
        return http("POST", "/api/log/stop")
    if action == "list":
        return http("GET", "/api/logs")
    raise ApiError("action 只能是 start / stop / list")


def t_log_read(args):
    name = args.get("name")
    if not name:
        files = http("GET", "/api/logs").get("files", [])
        if not files:
            raise ApiError("还没有记录文件")
        name = files[0]["name"]
    text = http("GET", f"/api/logs/{urllib.parse.quote(name)}/download", raw=True, timeout=60)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {"name": name, "columns": [], "rows": [], "total_rows": 0}
    header, body = rows[0], rows[1:]
    tail = int(_num(args.get("tail"), 200))
    cols = args.get("columns") or ["iso_time", "elapsed_s", "function", "display_value",
                                    "display_unit", "is_ol", "alarm_state"]
    idx = [header.index(c) for c in cols if c in header]
    picked = body[-tail:] if tail > 0 else body
    return {
        "name": name,
        "total_rows": len(body),
        "returned": len(picked),
        "all_columns": header,
        "columns": [header[i] for i in idx],
        "rows": [[r[i] if i < len(r) else "" for i in idx] for r in picked],
    }


def t_log_analyze(args):
    name = args.get("name")
    if not name:
        files = http("GET", "/api/logs").get("files", [])
        if not files:
            raise ApiError("还没有记录文件")
        name = files[0]["name"]
    q = {"unit": args.get("unit"), "start": args.get("start"), "end": args.get("end"),
         "max_points": int(_num(args.get("max_points"), 200))}
    d = http("GET", f"/api/logs/{urllib.parse.quote(name)}/analysis", query=q, timeout=120)
    return {
        "name": name,
        "unit": d.get("unit"),
        "functions": d.get("functions"),
        "meta": d.get("meta"),
        "range": d.get("range"),
        "stats": d.get("stats"),
        "downsampled": not d.get("raw"),
        "columns": d.get("columns"),
        "points": d.get("points"),
    }


def t_adv_read(args):
    keys = args.get("keys") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    if not keys:
        raise ApiError("需要 keys，例如 [\"digits\", \"itp\"]；可用 dmm_options(include_advanced=true) 查看")
    return http("GET", "/api/advanced/read", query={"keys": ",".join(keys)})


def t_adv_set(args):
    return http("POST", "/api/advanced/set", {"key": args.get("key"), "value": args.get("value")})


def t_adv_action(args):
    return http("POST", "/api/advanced/action", {"key": args.get("key")})


def t_reconnect(_):
    return http("POST", "/api/device/reconnect")


def t_panel(args):
    action = args.get("action")
    if action == "release":
        return http("POST", "/api/device/release")
    if action == "resume":
        return http("POST", "/api/device/resume", timeout=20)
    raise ApiError("action 只能是 release / resume")


FUNCTION_CODES = ["DCV", "ACV", "RES2W", "RES4W", "DCI", "ACI", "ACV_DC", "ACI_DC",
                  "DCV_BCH", "DIODE", "RES2W_LP", "RES4W_LP", "CONT", "FREQ"]

TOOLS = [
    {
        "name": "dmm_status",
        "description": "查看万用表当前状态：在线状态、功能/量程/速率/触发、NULL、显示模式与分流电阻、"
                       "报警上下限、CSV 记录状态、最新读数和累计统计。",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
        "fn": t_status,
    },
    {
        "name": "dmm_options",
        "description": "列出可用的功能（含每个功能的可选量程 ranges、固定量程 fixed_range、可用设置 features）、"
                       "速率、触发源代码；include_advanced=true 时同时列出高级设置项。",
        "inputSchema": {"type": "object", "properties": {
            "include_advanced": {"type": "boolean", "default": False}}},
        "annotations": {"readOnlyHint": True},
        "fn": t_options,
    },
    {
        "name": "dmm_read",
        "description": "读取最新一个读数。fresh=true 时等待一个新样本（适合刚改完设置时）。",
        "inputSchema": {"type": "object", "properties": {
            "fresh": {"type": "boolean", "default": False},
            "timeout": {"type": "number", "description": "fresh 时最长等待秒数，默认 5"}}},
        "annotations": {"readOnlyHint": True},
        "fn": t_read,
    },
    {
        "name": "dmm_measure",
        "description": "采集一批新样本并返回统计（mean/std/min/max/pp/rms）。给 count（样本数）"
                       "或 seconds（采集时长），二者都不给默认 10 个样本。settle 为采集前等待秒数。",
        "inputSchema": {"type": "object", "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 5000},
            "seconds": {"type": "number", "minimum": 0.05, "maximum": 120},
            "settle": {"type": "number", "minimum": 0, "maximum": 60, "default": 0},
            "timeout": {"type": "number", "description": "最长等待秒数"},
            "include_samples": {"type": "boolean", "default": False,
                                "description": "是否返回每个样本"}}},
        "annotations": {"readOnlyHint": True},
        "fn": t_measure,
    },
    {
        "name": "dmm_setup",
        "description": "设置测量参数（只改给出的字段）。function 功能、range 量程（AUTO 或 R3..R9，"
                       "各功能可选量程不同，见 dmm_options 的 functions[].ranges；DCV_BCH/DIODE/CONT 为固定量程）、"
                       "rate 速率（PR0..PR5）、trigger 触发（TRS0..TRS5）、display_mode "
                       "（voltage 直接电压 / current 分流电流，后者强制 DCV）、shunt_ohm 分流电阻、"
                       "cont_threshold 通断阈值 1~1000 Ω（仅 CONT 功能）。settle>0 时等待后返回一个新读数。",
        "inputSchema": {"type": "object", "properties": {
            "function": {"type": "string", "enum": FUNCTION_CODES},
            "range": {"type": "string", "enum": ["AUTO", "R3", "R4", "R5", "R6", "R7", "R8", "R9"]},
            "rate": {"type": "string", "enum": ["PR0", "PR1", "PR2", "PR3", "PR4", "PR5"]},
            "trigger": {"type": "string", "enum": ["TRS0", "TRS1", "TRS2", "TRS3", "TRS4", "TRS5"]},
            "display_mode": {"type": "string", "enum": ["voltage", "current"]},
            "shunt_ohm": {"type": "number", "minimum": 1e-6, "maximum": 1e6},
            "cont_threshold": {"type": "integer", "minimum": 1, "maximum": 1000},
            "settle": {"type": "number", "minimum": 0, "maximum": 60}}},
        "fn": t_setup,
    },
    {
        "name": "dmm_history",
        "description": "取最近一段时间的历史读数（当前测量上下文内），按 max_points 均匀抽稀。",
        "inputSchema": {"type": "object", "properties": {
            "seconds": {"type": "number", "default": 60, "description": "0 表示全部"},
            "max_points": {"type": "integer", "default": 500}}},
        "annotations": {"readOnlyHint": True},
        "fn": t_history,
    },
    {
        "name": "dmm_set_limits",
        "description": "设置上下限报警。mode=voltage 或 current；enabled=true 时必须同时给 low<high。",
        "inputSchema": {"type": "object", "required": ["mode", "enabled"], "properties": {
            "mode": {"type": "string", "enum": ["voltage", "current"]},
            "enabled": {"type": "boolean"},
            "low": {"type": "number"},
            "high": {"type": "number"}}},
        "fn": t_limits,
    },
    {
        "name": "dmm_null",
        "description": "NULL 相对测量：on 开启、off 关闭、from_latest 以当前读数为参考并开启、"
                       "set_const 设置参考常数（value）。",
        "inputSchema": {"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ["on", "off", "from_latest", "set_const"]},
            "value": {"type": "number"}}},
        "fn": t_null,
    },
    {
        "name": "dmm_reset_stats",
        "description": "重置上位机的累计统计（MIN/MAX/AVG 等）。",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_reset_stats,
    },
    {
        "name": "dmm_log",
        "description": "CSV 记录：start 开始、stop 停止、list 列出记录文件。",
        "inputSchema": {"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ["start", "stop", "list"]}}},
        "fn": t_log,
    },
    {
        "name": "dmm_log_read",
        "description": "读取 CSV 记录文件的最后 tail 行（不给 name 则读最新文件）。",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "tail": {"type": "integer", "default": 200, "description": "0 表示全部"},
            "columns": {"type": "array", "items": {"type": "string"}}}},
        "annotations": {"readOnlyHint": True},
        "fn": t_log_read,
    },
    {
        "name": "dmm_log_analyze",
        "description": "分析 CSV 记录文件（不给 name 则用最新文件）：返回文件概况（时长、行数、单位、功能分段）、"
                       "指定时间段的统计（mean/std/min/max/pp/rms/median/OL/超限次数）和降采样曲线"
                       "（每点 [t, min, max, mean, ol, alarm]）。start/end 为 epoch 秒；"
                       "文件含多个单位时用 unit 选择。",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "unit": {"type": "string"},
            "start": {"type": "number"},
            "end": {"type": "number"},
            "max_points": {"type": "integer", "default": 200, "minimum": 10, "maximum": 20000}}},
        "annotations": {"readOnlyHint": True},
        "fn": t_log_analyze,
    },
    {
        "name": "dmm_advanced_read",
        "description": "读取高级设置的仪器回读值，例如 keys=[\"digits\",\"itp\",\"azero\"]。",
        "inputSchema": {"type": "object", "required": ["keys"], "properties": {
            "keys": {"type": "array", "items": {"type": "string"}}}},
        "annotations": {"readOnlyHint": True},
        "fn": t_adv_read,
    },
    {
        "name": "dmm_advanced_set",
        "description": "写高级设置。select 类给选项代码（如 digits=RE5），number 类给数值（如 itp=10）。",
        "inputSchema": {"type": "object", "required": ["key", "value"], "properties": {
            "key": {"type": "string"},
            "value": {"type": ["string", "number"]}}},
        "fn": t_adv_set,
    },
    {
        "name": "dmm_advanced_action",
        "description": "执行高级动作，例如 manual_trigger、icl（清空存储）、cls、rst（仪器复位，慎用）。",
        "inputSchema": {"type": "object", "required": ["key"], "properties": {
            "key": {"type": "string"}}},
        "annotations": {"destructiveHint": True},
        "fn": t_adv_action,
    },
    {
        "name": "dmm_panel_control",
        "description": "release：完全释放控制给仪器面板（之后不发任何命令，只接收数据，记录/测量仍可用；"
                       "设置类工具会返回 local_mode 错误）；resume：恢复上位机控制。"
                       "用户正在手动操作面板时不要 resume。",
        "inputSchema": {"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ["release", "resume"]}}},
        "fn": t_panel,
    },
    {
        "name": "dmm_reconnect",
        "description": "强制重新连接仪器。",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_reconnect,
    },
]
TOOL_MAP = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP
# ---------------------------------------------------------------------------
def send(msg: dict):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req: dict):
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        want = params.get("protocolVersion")
        return {
            "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{k: v for k, v in t.items() if k != "fn"} for t in TOOLS]}
    if method == "tools/call":
        tool = TOOL_MAP.get(params.get("name"))
        if not tool:
            raise LookupError(f"未知工具：{params.get('name')}")
        args = params.get("arguments") or {}
        try:
            result = tool["fn"](args)
        except ApiError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # noqa: BLE001 - 把意外错误返回给 agent 而不是崩溃
            return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True}
        text = json.dumps(result, ensure_ascii=False, indent=1)
        out = {"content": [{"type": "text", "text": text}]}
        if isinstance(result, dict):
            out["structuredContent"] = result
        return out
    raise NotImplementedError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "Parse error"}})
            continue
        if not isinstance(req, dict):
            continue
        rid = req.get("id")
        is_notification = "id" not in req
        try:
            result = handle(req)
        except NotImplementedError as exc:
            if not is_notification:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32601, "message": f"Method not found: {exc}"}})
            continue
        except LookupError as exc:
            if not is_notification:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32602, "message": str(exc)}})
            continue
        if not is_notification:
            send({"jsonrpc": "2.0", "id": rid, "result": result})


if __name__ == "__main__":
    main()
