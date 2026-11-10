#!/usr/bin/env python3
"""ADCMT 7461A 网页上位机（Flask + SSE）。

启动：
    .venv/bin/python app.py                # 监听 127.0.0.1:7461
    HOST=0.0.0.0 .venv/bin/python app.py   # 允许局域网访问
    DMM_SIM=1 .venv/bin/python app.py      # 无硬件模拟器
"""

from __future__ import annotations

import atexit
import csv
import json
import logging
import math
import os
import queue
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

import dmm7461a

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
SETTINGS_FILE = DATA_DIR / "settings.json"

PORT = int(os.environ.get("PORT", "7461"))
HOST = os.environ.get("HOST", "127.0.0.1")
SIM = os.environ.get("DMM_SIM", "") == "1"
RING_SIZE = 30000
LOG_NAME_RE = re.compile(r"^7461A_\d{8}_\d{6}\.csv$")
SHUNT_MIN = 1e-6
SHUNT_MAX = 1e6

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("app")

app = Flask(__name__)
app.json.ensure_ascii = False

UNIT_LABEL = {"V": "V", "A": "A", "OHM": "Ω", "Hz": "Hz"}

FUNCTION_NOTES = {
    "DCV": "直流电压测量。分流电流模式会强制切换到此档。",
    "ACV": "交流电压测量，速率档位对应不同的滤波/积分时间。",
    "RES2W": "二线电阻测量，结果包含测试线电阻。",
    "RES4W": "四线电阻测量，使用四线接法消除引线电阻。",
    "DCI": "直接直流电流测量，注意电流档内阻带来的 burden 电压。",
    "ACI": "直接交流电流测量，注意电流档内阻带来的 burden 电压。",
    "ACV_DC": "交流+直流耦合电压测量（AC+DC）。",
    "ACI_DC": "交流+直流耦合电流测量（AC+DC）。",
    "DCV_BCH": "直流电压 Bch 模式。",
    "DIODE": "二极管正向压降测量。",
    "RES2W_LP": "低功耗二线电阻测量，限制测试开路电压。",
    "RES4W_LP": "低功耗四线电阻测量，限制测试开路电压。",
    "CONT": "通断测量，以电阻值显示。",
    "FREQ": "频率测量，单位 Hz。",
}

META = {
    "functions": [
        {"code": code, "label": meta[3], "unit": UNIT_LABEL.get(meta[2], meta[2]),
         "note": FUNCTION_NOTES.get(code, "")}
        for code, meta in dmm7461a.FUNCTIONS.items()
    ],
    "ranges": [
        {"code": code, "label": meta[1]} for code, meta in dmm7461a.RANGES.items()
    ],
    "rates": [
        {"code": code, "label": meta[1]} for code, meta in dmm7461a.RATES.items()
    ],
    "triggers": [
        {"code": code, "label": meta[1]} for code, meta in dmm7461a.TRIGGERS.items()
    ],
    "display_modes": [
        {"code": "voltage", "label": "直接电压"},
        {"code": "current", "label": "分流电流"},
    ],
}


# ---------------------------------------------------------------------------
# 高级设置命令表（ADC 命令语言，USB 模式可用）
# ---------------------------------------------------------------------------
def _adv(key, label, kind, set_cmd="", query=None, **extra):
    item = {"key": key, "label": label, "kind": kind, "set": set_cmd, "query": query}
    item.update(extra)
    return item


ADVANCED_GROUPS = [
    {
        "key": "acq",
        "label": "采样与显示",
        "items": [
            _adv("itp", "积分时间", "number", "ITP{}", "ITP?", unit="PLC",
                 min=0.02, max=100, step=0.01, fmt="num", hint="0.02 – 100 PLC",
                 state_after=True),
            _adv("its", "积分时间", "number", "ITS{}", "ITS?", unit="s",
                 min=0.00001, max=10, step=0.00001, fmt="sci", hint="0.00001 – 10 s",
                 state_after=True),
            _adv("digits", "显示位数", "select", "RE{}", "RE?", options=[
                ["RE3", "3½ 位"], ["RE4", "4½ 位"], ["RE5", "5½ 位"], ["RE6", "6½ 位"]]),
            _adv("filter", "滤波（AC）", "select", "FL{}", "FL?", options=[
                ["FL1", "快速"], ["FL2", "中速"], ["FL3", "慢速"]],
                applies=["ACV", "ACI", "ACV_DC", "ACI_DC"]),
            _adv("azero", "自动调零", "select", "AZ{}", "AZ?", options=[
                ["AZ0", "关"], ["AZ1", "开"], ["AZ2", "执行一次"]]),
            _adv("impedance", "DCV 输入阻抗", "select", "IMP{}", "IMP?", options=[
                ["IMP0", "高阻 HiZ"], ["IMP1", "10 MΩ"]], applies=["DCV", "DCV_BCH"]),
            _adv("cont_limit", "通断阈值", "number", "KOM{}", "KOM?", unit="Ω",
                 min=1, max=1000, step=1, fmt="int", applies=["CONT"], hint="1 – 1000 Ω"),
        ],
    },
    {
        "key": "calc",
        "label": "计算",
        "items": [
            _adv("null", "NULL 相对值", "select", "NL{}", "NL?", options=[
                ["NL0", "关"], ["NL1", "开"]]),
            _adv("null_const", "NULL 常数", "number", "KNL{}", "KNL?", fmt="sci",
                 action="null_from_latest", action_label="取当前读数",
                 hint="NULL 关闭时常数无效；「取当前读数」会同时打开 NULL"),
            _adv("smooth", "平滑", "select", "SM{}", "SM?", options=[
                ["SM0", "关"], ["SM1", "开"]]),
            _adv("smooth_count", "平滑次数", "number", "TI{}", "TI?",
                 min=2, max=100, step=1, fmt="int", hint="2 – 100 次"),
            _adv("scale", "缩放", "select", "SC{}", "SC?", options=[
                ["SC0", "关"], ["SC1", "开"]]),
            _adv("ka", "缩放常数 A", "number", "KA{}", "KA?", fmt="sci",
                 action="KAM", action_label="取当前读数"),
            _adv("kb", "缩放常数 B", "number", "KB{}", "KB?", fmt="sci",
                 action="KBM", action_label="取当前读数"),
            _adv("kc", "缩放常数 C", "number", "KC{}", "KC?", fmt="sci",
                 action="KCM", action_label="取当前读数"),
            _adv("db", "dB 计算", "select", "DB{}", "DB?", options=[
                ["DB0", "关"], ["DB1", "dB"], ["DB2", "dBm"]]),
            _adv("kd", "dB 常数 D", "number", "KD{}", "KD?", fmt="sci",
                 action="KDM", action_label="取当前读数"),
            _adv("maxmin", "MAX/MIN 计算", "select", "MN{}", "MN?", options=[
                ["MN0", "关"], ["MN1", "开"]]),
            _adv("max", "MAX 值", "readout", query="MAX?", hint="需先开启 MAX/MIN 计算"),
            _adv("min", "MIN 值", "readout", query="MIN?", hint="需先开启 MAX/MIN 计算"),
            _adv("ave", "平均值", "readout", query="AVE?", hint="需先开启 MAX/MIN 计算"),
            _adv("avn", "平均次数", "readout", query="AVN?", hint="需先开启 MAX/MIN 计算"),
        ],
    },
    {
        "key": "comp",
        "label": "比较器（后面板输出）",
        "items": [
            _adv("hi", "HIGH 常数", "number", "HI{}", "HI?", fmt="sci",
                 action="HIM", action_label="取当前读数"),
            _adv("lo", "LOW 常数", "number", "LO{}", "LO?", fmt="sci",
                 action="LOM", action_label="取当前读数"),
            _adv("lop", "LOW 计入 PASS", "select", "LOP{}", "LOP?", options=[
                ["LOP0", "不计入"], ["LOP1", "计入"]]),
            _adv("mip", "GO 计入 PASS", "select", "MIP{}", "MIP?", options=[
                ["MIP0", "不计入"], ["MIP1", "计入"]]),
            _adv("hip", "HIGH 计入 PASS", "select", "HIP{}", "HIP?", options=[
                ["HIP0", "不计入"], ["HIP1", "计入"]]),
        ],
    },
    {
        "key": "trig",
        "label": "触发与采样参数",
        "items": [
            _adv("trs", "触发源", "select", "TRS{}", "TRS?", options=[
                ["TRS0", "立即"], ["TRS1", "手动"], ["TRS2", "外部"],
                ["TRS3", "总线"], ["TRS4", "电平"], ["TRS5", "差值"]],
                state_after=True),
            _adv("manual_trigger", "手动触发", "action", "*TRG", action_label="立即触发"),
            _adv("trt", "采样间隔", "number", "TRT{}", "TRT?", unit="s",
                 min=0.00005, max=3600, step=0.00001, fmt="sci", hint="50 µs – 3600 s"),
            _adv("trd", "触发延时", "number", "TRD{}", "TRD?", unit="s",
                 min=0, max=3600, step=0.001, fmt="sci", hint="0 – 3600 s"),
            _adv("trn", "触发次数", "number", "TRN{}", "TRN?",
                 min=1, max=50000, step=1, fmt="int"),
            _adv("spn", "采样次数", "number", "SPN{}", "SPN?",
                 min=1, max=16000, step=1, fmt="int"),
            _adv("ptl", "预触发", "select", "PTL{}", "PTL?", options=[
                ["PTL0", "0%（关）"], ["PTL1", "25%"], ["PTL2", "50%"], ["PTL3", "75%"]]),
            _adv("trcm", "触发模式", "select", "TRCM{}", "TRCM?", options=[
                ["TRCM0", "单次"], ["TRCM1", "多次"]]),
            _adv("cw", "完成信号宽度", "select", "CW{}", "CW?", options=[
                ["CW0", "5 µs"], ["CW1", "100 µs"]]),
            _adv("tlv", "电平阈值", "number", "TLV{}", "TLV?",
                 min=-1199999, max=1199999, step=1, fmt="int"),
            _adv("tls", "电平斜率", "select", "TLS{}", "TLS?", options=[
                ["TLS0", "下降沿"], ["TLS1", "上升沿"]]),
            _adv("tdl", "差值阈值", "number", "TDL{}", "TDL?",
                 min=1, max=600000, step=1, fmt="int"),
            _adv("tds", "差值斜率", "select", "TDS{}", "TDS?", options=[
                ["TDS0", "负"], ["TDS1", "正"], ["TDS2", "绝对值"]]),
            _adv("tdc", "差值方向", "select", "TDC{}", "TDC?", options=[
                ["TDC0", "收敛"], ["TDC1", "发散"]]),
        ],
    },
    {
        "key": "store",
        "label": "内部存储与状态",
        "items": [
            _adv("store", "内部存储", "select", "ST{}", "ST?", options=[
                ["ST0", "关"], ["ST1", "开"]]),
            _adv("irpo", "存储数量", "readout", query="IRPO?"),
            _adv("irno", "存储范围", "readout", query="IRNO?"),
            _adv("icl", "清空存储", "action", "ICL", "IRPO?", action_label="清空"),
            _adv("scnt", "统计样本数", "readout", query="SCNT?"),
            _adv("smax", "统计最大值", "readout", query="SMAX?"),
            _adv("smin", "统计最小值", "readout", query="SMIN?"),
            _adv("stb", "状态字节 STB", "readout", query="*STB?"),
            _adv("esr", "事件状态 ESR", "readout", query="*ESR?"),
            _adv("cls", "清除状态", "action", "*CLS", "*STB?", action_label="清除"),
            _adv("rst", "仪器复位", "action", "*RST", action_label="复位",
                 confirm="确定要复位仪器吗？所有设置将恢复默认。", state_after=True),
        ],
    },
]

ADVANCED_ITEMS = {item["key"]: item for group in ADVANCED_GROUPS for item in group["items"]}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _fmt_num(value, digits: int = 12) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}g}"


def _valid_number(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.reset(None, 0)

    def reset(self, unit, context_id):
        self.unit = unit
        self.context_id = context_id
        self.n = 0
        self.n_total = 0
        self.n_ol = 0
        self.min = None
        self.max = None
        self.mean = 0.0
        self.m2 = 0.0
        self.since = time.time()

    def add(self, value, ol: bool):
        self.n_total += 1
        if ol or value is None:
            self.n_ol += 1
            return
        self.n += 1
        if self.min is None or value < self.min:
            self.min = value
        if self.max is None or value > self.max:
            self.max = value
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)

    def as_dict(self):
        std = math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0
        pp = (self.max - self.min) if self.n else 0.0
        return {
            "ctx": self.context_id,
            "n": self.n,
            "n_total": self.n_total,
            "n_ol": self.n_ol,
            "min": self.min,
            "max": self.max,
            "avg": self.mean if self.n else None,
            "pp": pp,
            "std": std,
            "unit": self.unit,
            "since": self.since,
        }


# ---------------------------------------------------------------------------
# CSV 记录
# ---------------------------------------------------------------------------
CSV_FIELDS = [
    "iso_time", "epoch_s", "elapsed_s", "seq", "context_id", "function",
    "function_unit", "range", "rate", "reading_value", "current_a",
    "display_value", "display_unit", "is_ol", "shunt_ohm", "display_mode",
    "alarm_state", "raw",
]


class CsvLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self._fh = None
        self._writer = None
        self.file = None
        self.rows = 0
        self.started_at = None
        self.started_mono = None
        self._last_flush = 0.0
        self.error = None

    @property
    def active(self) -> bool:
        return self._fh is not None

    def start(self):
        if self.active:
            return self.state()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("7461A_%Y%m%d_%H%M%S.csv")
        path = self.log_dir / name
        fh = open(path, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        fh.flush()
        self._fh, self._writer = fh, writer
        self.file = name
        self.rows = 0
        self.started_at = time.time()
        self.started_mono = time.monotonic()
        self._last_flush = self.started_mono
        self.error = None
        return self.state()

    def stop(self):
        fh, self._fh = self._fh, None
        self._writer = None
        if fh is not None:
            try:
                fh.flush()
                fh.close()
            except OSError as exc:
                self.error = str(exc)
        return self.state()

    def write(self, row: dict):
        if not self.active:
            return
        try:
            self._writer.writerow(row)
            self.rows += 1
            now = time.monotonic()
            if now - self._last_flush >= 0.5 or self.rows % 100 == 0:
                self._fh.flush()
                self._last_flush = now
            self.error = None
        except (OSError, csv.Error) as exc:
            self.error = str(exc)
            LOG.exception("写 CSV 失败，停止记录")
            self.stop()

    def state(self):
        return {
            "active": self.active,
            "file": self.file,
            "rows": self.rows,
            "started_at": self.started_at,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 数据中枢
# ---------------------------------------------------------------------------
class Hub:
    def __init__(self, sim: bool = False):
        self.sim = sim
        self.lock = threading.RLock()
        self.samples = deque(maxlen=RING_SIZE)
        self.seq = 0
        self.context_id = 0
        self.context_key = None
        self.stats = Stats()
        self._rate_times = deque(maxlen=8192)
        self.config = {"display_mode": "voltage", "shunt_ohm": 0.1}
        self.limits = {
            "voltage": {"enabled": False, "low": None, "high": None},
            "current": {"enabled": False, "low": None, "high": None},
        }
        self.instrument: dict = {}
        self.last_alarm = "off"
        self.logger = CsvLogger(LOG_DIR)
        self.clients: list[queue.Queue] = []
        self.last_stats_push = 0.0
        self.last_log_push = 0.0
        self.started_at = time.time()
        self.dmm = None

    # -- 生命周期 ----------------------------------------------------------
    def attach(self, dmm):
        self.dmm = dmm

    # -- 上下文 ------------------------------------------------------------
    def _context_for(self, mode: str, shunt, function, unit) -> tuple:
        return (mode, round(float(shunt), 12) if shunt else shunt, function, unit)

    def _reset_context(self, unit, reason: str = ""):
        self.context_id += 1
        self.samples.clear()
        self._rate_times.clear()
        self.stats.reset(unit, self.context_id)
        LOG.info("测量上下文切换 #%s %s", self.context_id, reason)

    def _sample_rate(self) -> float:
        if not self._rate_times:
            return 0.0
        now = time.time()
        cutoff = now - 2.0
        n = sum(1 for t in self._rate_times if t >= cutoff)
        return n / 2.0

    def _stats_payload(self) -> dict:
        payload = self.stats.as_dict()
        payload["rate_hz"] = round(self._sample_rate(), 1)
        return payload

    def _bump_if_needed(self, mode, shunt, function, unit):
        key = self._context_for(mode, shunt, function, unit)
        if key != self.context_key:
            self.context_key = key
            self._reset_context(unit, f"mode={mode} shunt={shunt} fn={function}")

    # -- 样本处理 ----------------------------------------------------------
    def on_sample(self, raw_sample: dict):
        with self.lock:
            self.seq += 1
            voltage = raw_sample.get("v")
            ol = bool(raw_sample.get("ol"))
            tag = raw_sample.get("tag")
            # 以仪器回读状态为准；数据表头仅在启动初期作为兜底
            function = (self.instrument.get("function")
                        or dmm7461a.FUNCTION_BY_TAG.get(tag or ""))
            shunt = self.config["shunt_ohm"]
            mode = self.config["display_mode"]

            fn_meta = dmm7461a.FUNCTIONS.get(function or "", (None, None, "V", None))
            base_unit = UNIT_LABEL.get(fn_meta[2], fn_meta[2])

            # 分流电流只对 DCV 有意义；其它功能读数是该功能的基本单位（Ω/Hz/A）
            current = None
            if function == "DCV" and voltage is not None and shunt:
                current = voltage / float(shunt)

            if mode == "current":
                display, unit = current, "A"
            else:
                display, unit = voltage, base_unit

            self._bump_if_needed(mode, shunt, function, unit)

            if mode == "current" or unit == "A":
                limit_key = "current"
            elif unit == "V":
                limit_key = "voltage"
            else:
                limit_key = None
            alarm = (self._alarm_for(display, ol, self.limits[limit_key])
                     if limit_key else ("ol" if ol else "off"))

            sample = {
                "seq": self.seq,
                "t": raw_sample.get("t", time.time()),
                "ctx": self.context_id,
                "v": voltage,
                "fu": base_unit,
                "i": current,
                "d": display,
                "u": unit,
                "ol": ol,
                "fn": function,
                "rng": self.instrument.get("range"),
                "rate": self.instrument.get("rate"),
                "alarm": alarm,
                "raw": raw_sample.get("raw", ""),
            }
            self.last_alarm = alarm
            self.samples.append(sample)
            self._rate_times.append(raw_sample.get("t", time.time()))
            self.stats.add(display, ol)

            if self.logger.active:
                elapsed = (time.monotonic() - self.logger.started_mono) if self.logger.started_mono else 0.0
                self.logger.write({
                    "iso_time": _now_iso(),
                    "epoch_s": f"{sample['t']:.6f}",
                    "elapsed_s": f"{elapsed:.3f}",
                    "seq": sample["seq"],
                    "context_id": self.context_id,
                    "function": function or "",
                    "function_unit": base_unit,
                    "range": self.instrument.get("range") or "",
                    "rate": self.instrument.get("rate") or "",
                    "reading_value": _fmt_num(voltage),
                    "current_a": _fmt_num(current),
                    "display_value": _fmt_num(display),
                    "display_unit": unit,
                    "is_ol": 1 if ol else 0,
                    "shunt_ohm": shunt,
                    "display_mode": mode,
                    "alarm_state": alarm,
                    "raw": raw_sample.get("raw", ""),
                })

            self._broadcast_locked("sample", sample)
            now = time.time()
            if now - self.last_stats_push >= 0.25:
                self.last_stats_push = now
                self._broadcast_locked("stats", self._stats_payload())
            if now - self.last_log_push >= 1.0:
                self.last_log_push = now
                self._broadcast_locked("log", self.logger.state())

    @staticmethod
    def _alarm_for(value, ol: bool, limit: dict) -> str:
        if ol or value is None:
            return "ol"
        if not limit.get("enabled"):
            return "off"
        low, high = limit.get("low"), limit.get("high")
        if low is not None and value < low:
            return "low"
        if high is not None and value > high:
            return "high"
        return "ok"

    # -- 状态 --------------------------------------------------------------
    def on_status(self, status: dict):
        with self.lock:
            self.instrument = status
        self._broadcast("status", {
            "state": status.get("state"),
            "message": status.get("message"),
            "device": status,
        })
        self._broadcast("state", self.state())

    def state(self) -> dict:
        with self.lock:
            device = self.dmm.status_snapshot() if self.dmm else {}
            return {
                "server_time": time.time(),
                "uptime": time.time() - self.started_at,
                "sim": self.sim,
                "device": device,
                "instrument": {
                    "function": device.get("function"),
                    "function_label": device.get("function_label"),
                    "range": device.get("range"),
                    "range_label": device.get("range_label"),
                    "rate": device.get("rate"),
                    "rate_label": device.get("rate_label"),
                    "trigger": device.get("trigger"),
                    "trigger_label": device.get("trigger_label"),
                    "idn": device.get("idn"),
                    "serial": device.get("serial"),
                },
                "config": dict(self.config),
                "limits": json.loads(json.dumps(self.limits)),
                "alarm": self.last_alarm,
                "logging": self.logger.state(),
                "stats": self._stats_payload(),
                "context_id": self.context_id,
            }

    def snapshot_payload(self):
        with self.lock:
            cutoff = time.time() - 60
            samples = [s for s in self.samples
                       if s["ctx"] == self.context_id and s["t"] >= cutoff]
            payload = self.state()
            payload["samples"] = samples
            payload["context_id"] = self.context_id
            return payload

    def history(self, seconds: float):
        with self.lock:
            if seconds and seconds > 0:
                cutoff = time.time() - seconds
                return [s for s in self.samples
                        if s["ctx"] == self.context_id and s["t"] >= cutoff]
            return [s for s in self.samples if s["ctx"] == self.context_id]

    def reset_stats(self):
        with self.lock:
            unit = self.stats.unit
            self.stats.reset(unit, self.context_id)
            payload = self._stats_payload()
        self._broadcast("stats", payload)
        return payload

    def set_config(self, display_mode=None, shunt_ohm=None):
        with self.lock:
            if display_mode is not None:
                self.config["display_mode"] = display_mode
            if shunt_ohm is not None:
                self.config["shunt_ohm"] = float(shunt_ohm)
            mode = self.config["display_mode"]
            unit = "A" if mode == "current" else "V"
            fn = self.instrument.get("function")
            self._bump_if_needed(mode, self.config["shunt_ohm"], fn, unit)
        self._save_settings()
        self._broadcast("state", self.state())
        return self.state()

    def set_limits(self, mode: str, enabled: bool, low, high):
        with self.lock:
            self.limits[mode] = {"enabled": bool(enabled), "low": low, "high": high}
        self._save_settings()
        self._broadcast("state", self.state())
        return self.limits[mode]

    # -- SSE ---------------------------------------------------------------
    def add_client(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=512)
        with self.lock:
            self.clients.append(q)
        return q

    def remove_client(self, q: queue.Queue):
        with self.lock:
            try:
                self.clients.remove(q)
            except ValueError:
                pass

    def _broadcast_locked(self, event: str, data):
        for q in list(self.clients):
            try:
                q.put_nowait((event, data))
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait((event, data))
                except queue.Full:
                    pass

    def _broadcast(self, event: str, data):
        with self.lock:
            self._broadcast_locked(event, data)

    # -- 设置持久化 --------------------------------------------------------
    def _save_settings(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"config": self.config, "limits": self.limits}
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, SETTINGS_FILE)
        except OSError:
            LOG.exception("保存设置失败")


def _load_settings(hub: Hub):
    try:
        payload = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    config = payload.get("config") or {}
    mode = config.get("display_mode")
    if mode in ("voltage", "current"):
        hub.config["display_mode"] = mode
    shunt = config.get("shunt_ohm")
    if _valid_number(shunt) and SHUNT_MIN <= float(shunt) <= SHUNT_MAX:
        hub.config["shunt_ohm"] = float(shunt)
    for mode in ("voltage", "current"):
        lim = (payload.get("limits") or {}).get(mode)
        if isinstance(lim, dict):
            hub.limits[mode] = {
                "enabled": bool(lim.get("enabled")),
                "low": lim.get("low") if _valid_number(lim.get("low")) else None,
                "high": lim.get("high") if _valid_number(lim.get("high")) else None,
            }


# ---------------------------------------------------------------------------
# SSE 辅助
# ---------------------------------------------------------------------------
def _sse(event: str, data, event_id=None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"


def _api_error(message: str, code: str = "error", status: int = 400):
    return jsonify({"ok": False, "error": message, "code": code}), status


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------
_hub: Hub | None = None
_dmm = None


def build(sim: bool | None = None) -> Flask:
    global _hub, _dmm
    sim = SIM if sim is None else sim
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    hub = Hub(sim=sim)
    _load_settings(hub)
    dmm = dmm7461a.create_dmm(sim=sim, on_sample=hub.on_sample, on_status=hub.on_status)
    hub.attach(dmm)
    _hub, _dmm = hub, dmm
    dmm.start()
    atexit.register(shutdown)
    return app


def shutdown():
    global _dmm
    if _dmm is not None:
        try:
            _dmm.stop()
        except Exception:
            LOG.exception("停止设备线程失败")
        _dmm = None
    if _hub is not None:
        _hub.logger.stop()


def hub() -> Hub:
    if _hub is None:
        raise RuntimeError("应用尚未初始化")
    return _hub


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def api_health():
    h = hub()
    return jsonify({
        "ok": True,
        "port": PORT,
        "sim": h.sim,
        "uptime": time.time() - h.started_at,
        "device_state": h.dmm.status_snapshot().get("state") if h.dmm else None,
    })


@app.get("/api/meta")
def api_meta():
    return jsonify(META)


@app.get("/api/state")
def api_state():
    return jsonify(hub().state())


@app.get("/api/history")
def api_history():
    try:
        seconds = float(request.args.get("seconds", "60"))
    except ValueError:
        return _api_error("seconds 参数无效", "bad_request")
    if seconds < 0:
        return _api_error("seconds 不能为负数", "bad_request")
    samples = hub().history(seconds)
    return jsonify({"context_id": hub().context_id, "samples": samples})


@app.get("/api/stream")
def api_stream():
    h = hub()

    def gen():
        q = h.add_client()
        try:
            yield _sse("snapshot", h.snapshot_payload())
            while True:
                try:
                    event, data = q.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                event_id = data.get("seq") if event == "sample" and isinstance(data, dict) else None
                yield _sse(event, data, event_id)
        except GeneratorExit:
            return
        finally:
            h.remove_client(q)

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.post("/api/control")
def api_control():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _api_error("请求必须是 JSON", "bad_request")
    action = data.get("action")
    value = data.get("value")
    h = hub()
    dmm = h.dmm

    if h.config["display_mode"] == "current" and action == "function" and value != "DCV":
        return _api_error("分流电流模式必须使用直流电压档（DCV），请先关闭分流模式", "mode_conflict", 409)

    try:
        if action == "function":
            result = dmm.set_function(str(value))
        elif action == "range":
            result = dmm.set_range("AUTO" if value in ("AUTO", "R0") else str(value))
        elif action == "rate":
            result = dmm.set_rate(str(value))
        elif action == "trigger":
            result = dmm.set_trigger(str(value))
        elif action == "sync":
            result = dmm.sync_state()
        else:
            return _api_error("未知的控制动作", "bad_request")
    except ValueError as exc:
        return _api_error(str(exc), "bad_value")
    except dmm7461a.DeviceOffline as exc:
        return _api_error(str(exc), "offline", 503)
    except dmm7461a.QueryTimeout as exc:
        return _api_error(str(exc), "timeout", 504)
    except dmm7461a.BusyError as exc:
        return _api_error(str(exc), "busy", 429)

    return jsonify({"ok": True, "instrument": result,
                    "applied": {k: result.get(k) for k in ("function", "range", "rate", "trigger")}})


@app.post("/api/config")
def api_config():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _api_error("请求必须是 JSON", "bad_request")
    h = hub()
    dmm = h.dmm

    mode = data.get("display_mode", h.config["display_mode"])
    if mode not in ("voltage", "current"):
        return _api_error("display_mode 只能是 voltage 或 current", "bad_value")

    shunt = data.get("shunt_ohm", h.config["shunt_ohm"])
    if not _valid_number(shunt) or not (SHUNT_MIN <= float(shunt) <= SHUNT_MAX):
        return _api_error(f"分流电阻必须在 {SHUNT_MIN:g} ~ {SHUNT_MAX:g} Ω 之间", "bad_value")

    device_state = dmm.status_snapshot().get("state") if dmm else "offline"
    switching_mode = mode != h.config["display_mode"]
    if mode == "current":
        if device_state != "online":
            return _api_error("设备离线，无法进入分流电流模式", "offline", 503)
        try:
            dmm.set_function("DCV")
        except dmm7461a.DeviceOffline as exc:
            return _api_error(str(exc), "offline", 503)
        except dmm7461a.QueryTimeout as exc:
            return _api_error(str(exc), "timeout", 504)
        except dmm7461a.BusyError as exc:
            return _api_error(str(exc), "busy", 429)

    state = h.set_config(display_mode=mode, shunt_ohm=float(shunt))
    if switching_mode:
        LOG.info("显示模式切换为 %s", mode)
    return jsonify({"ok": True, "config": state["config"], "context_id": state["context_id"]})


@app.post("/api/limits")
def api_limits():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _api_error("请求必须是 JSON", "bad_request")
    mode = data.get("mode")
    if mode not in ("voltage", "current"):
        return _api_error("mode 只能是 voltage 或 current", "bad_value")
    enabled = bool(data.get("enabled"))
    low, high = data.get("low"), data.get("high")
    if low is not None and not _valid_number(low):
        return _api_error("下限无效", "bad_value")
    if high is not None and not _valid_number(high):
        return _api_error("上限无效", "bad_value")
    if enabled:
        if low is None or high is None:
            return _api_error("启用报警时必须同时设置上下限", "bad_value")
        if float(low) >= float(high):
            return _api_error("下限必须小于上限", "bad_value")
    lim = hub().set_limits(mode, enabled,
                           float(low) if low is not None else None,
                           float(high) if high is not None else None)
    return jsonify({"ok": True, "limits": {mode: lim}})


@app.post("/api/stats/reset")
def api_stats_reset():
    return jsonify({"ok": True, "stats": hub().reset_stats()})


@app.post("/api/log/start")
def api_log_start():
    h = hub()
    if h.dmm and h.dmm.status_snapshot().get("state") != "online":
        return _api_error("设备离线，无法开始记录", "offline", 503)
    st = h.logger.start()
    h._broadcast("log", st)
    return jsonify({"ok": True, "logging": st})


@app.post("/api/log/stop")
def api_log_stop():
    st = hub().logger.stop()
    hub()._broadcast("log", st)
    return jsonify({"ok": True, "logging": st})


def _log_file_info(path: Path):
    st = path.stat()
    return {
        "name": path.name,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "active": hub().logger.file == path.name and hub().logger.active,
    }


@app.get("/api/logs")
def api_logs():
    files = []
    if LOG_DIR.exists():
        for path in sorted(LOG_DIR.glob("7461A_*.csv"), reverse=True):
            if LOG_NAME_RE.match(path.name):
                try:
                    files.append(_log_file_info(path))
                except OSError:
                    continue
    return jsonify({"files": files, "logging": hub().logger.state()})


@app.get("/api/logs/<name>/download")
def api_log_download(name):
    if not LOG_NAME_RE.fullmatch(name):
        return _api_error("文件名无效", "bad_name", 404)
    path = (LOG_DIR / name).resolve()
    if path.parent != LOG_DIR.resolve() or not path.is_file():
        return _api_error("文件不存在", "not_found", 404)
    try:
        size = path.stat().st_size
    except OSError:
        return _api_error("文件不可读", "not_found", 404)

    def gen():
        remaining = size
        with open(path, "rb") as fh:
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return Response(gen(), mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Content-Length": str(size),
    })


@app.post("/api/device/reconnect")
def api_reconnect():
    try:
        hub().dmm.force_reconnect()
    except dmm7461a.DeviceOffline as exc:
        return _api_error(str(exc), "offline", 503)
    return jsonify({"ok": True})


def _dmm_http_error(exc):
    if isinstance(exc, dmm7461a.DeviceOffline):
        return _api_error(str(exc), "offline", 503)
    if isinstance(exc, dmm7461a.QueryTimeout):
        return _api_error(str(exc), "timeout", 504)
    if isinstance(exc, dmm7461a.BusyError):
        return _api_error(str(exc), "busy", 429)
    if isinstance(exc, ValueError):
        return _api_error(str(exc), "bad_value")
    return _api_error(str(exc), "device_error", 500)


@app.get("/api/advanced")
def api_advanced():
    return jsonify({"groups": ADVANCED_GROUPS})


@app.get("/api/advanced/read")
def api_advanced_read():
    keys = [k for k in request.args.get("keys", "").split(",") if k]
    if not keys:
        return _api_error("缺少 keys 参数", "bad_request")
    dmm = hub().dmm
    values = {}
    for key in keys:
        item = ADVANCED_ITEMS.get(key)
        if not item or not item.get("query"):
            values[key] = {"ok": False, "error": "该设置不支持读取"}
            continue
        try:
            values[key] = {"ok": True, "value": dmm.query_command(item["query"])}
        except dmm7461a.DMMError as exc:
            values[key] = {"ok": False, "error": str(exc),
                           "code": getattr(exc, "code", "error")}
    return jsonify({"values": values})


@app.post("/api/advanced/set")
def api_advanced_set():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _api_error("请求必须是 JSON", "bad_request")
    key = data.get("key")
    item = ADVANCED_ITEMS.get(key)
    if not item:
        return _api_error("未知的高级设置项", "bad_request")
    if item["kind"] in ("readout", "action"):
        return _api_error("请使用专门的动作接口", "bad_request")

    value = data.get("value")
    if item["kind"] == "select":
        allowed = [opt[0] for opt in item.get("options", [])]
        if value not in allowed:
            return _api_error(f"可选值：{', '.join(allowed)}", "bad_value")
        formatted = str(value)
        cmd = formatted          # 选项值即完整命令（如 RE5 / FL2 / TRS1）
    elif item["kind"] == "number":
        if not _valid_number(value):
            return _api_error("请输入有效数字", "bad_value")
        num = float(value)
        if item.get("min") is not None and num < float(item["min"]):
            return _api_error(f"数值不能小于 {item['min']}", "bad_value")
        if item.get("max") is not None and num > float(item["max"]):
            return _api_error(f"数值不能大于 {item['max']}", "bad_value")
        fmt = item.get("fmt", "num")
        if fmt == "int":
            formatted = str(int(round(num)))
        elif fmt == "sci":
            formatted = f"{num:.9E}"
        else:
            formatted = f"{num:.9g}"
        cmd = item["set"].format(formatted)
    else:
        return _api_error("该设置项类型不支持", "bad_value")

    dmm = hub().dmm
    try:
        dmm.send_command(cmd)
        reply = dmm.query_command(item["query"]) if item.get("query") else None
        if item.get("state_after"):
            dmm.sync_state()
    except (dmm7461a.DMMError, ValueError) as exc:
        return _dmm_http_error(exc)
    return jsonify({"ok": True, "sent": cmd, "value": reply})


@app.post("/api/advanced/action")
def api_advanced_action():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _api_error("请求必须是 JSON", "bad_request")
    key = data.get("key")
    item = ADVANCED_ITEMS.get(key)
    if not item:
        return _api_error("未知的高级设置项", "bad_request")
    action = item.get("action") or (item["set"] if item["kind"] == "action" else None)
    if not action:
        return _api_error("该设置项没有动作", "bad_request")

    h = hub()
    dmm = h.dmm
    if action == "null_from_latest":
        with h.lock:
            latest = h.samples[-1] if h.samples else None
        if not latest or latest.get("ol") or latest.get("v") is None:
            return _api_error("当前没有有效读数，无法取当前值", "no_value", 409)
        # 手册：NULL 计算关闭时 KNL 常数设置无效，先打开 NULL
        try:
            dmm.send_command("NL1")
        except (dmm7461a.DMMError, ValueError) as exc:
            return _dmm_http_error(exc)
        cmd = f"KNL{latest['v']:.9E}"
    else:
        cmd = action

    try:
        dmm.send_command(cmd)
        reply = dmm.query_command(item["query"]) if item.get("query") else None
        if item.get("state_after"):
            dmm.sync_state()
    except (dmm7461a.DMMError, ValueError) as exc:
        return _dmm_http_error(exc)
    return jsonify({"ok": True, "sent": cmd, "value": reply})


@app.post("/api/dev/raw")
def api_dev_raw():
    """仅开发诊断用：直接发 ADC 命令，不更新内部状态（用于验证外部改动同步）。"""
    if os.environ.get("DMM_DEV") != "1":
        return _api_error("未启用（需要 DMM_DEV=1）", "not_found", 404)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _api_error("请求必须是 JSON", "bad_request")
    cmd = data.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return _api_error("缺少 command", "bad_request")
    cmd = cmd.strip()
    try:
        hub().dmm.send_command(cmd)
    except (dmm7461a.DMMError, ValueError) as exc:
        return _dmm_http_error(exc)
    return jsonify({"ok": True, "sent": cmd})


@app.post("/api/dev/sim")
def api_dev_sim():
    if not hub().sim:
        return _api_error("仅在模拟器模式可用", "not_found", 404)
    dmm = hub().dmm
    data = request.get_json(silent=True) or {}
    result = {}
    if "ol" in data:
        result["ol"] = dmm.inject_ol(bool(data["ol"]))
    if "disconnect" in data:
        result["disconnect"] = dmm.inject_disconnect(bool(data["disconnect"]))
    return jsonify({"ok": True, "result": result})


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    build(SIM)
    print(f"ADCMT 7461A 上位机: http://{HOST}:{PORT}  (模拟器={SIM})")
    try:
        app.run(host=HOST, port=PORT, threaded=True, use_reloader=False, debug=False)
    except OSError as exc:
        print(f"启动失败：端口 {PORT} 可能被占用（{exc}）")
        raise
    finally:
        shutdown()


if __name__ == "__main__":
    main()
