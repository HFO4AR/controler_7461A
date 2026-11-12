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
import io
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

# ---------------------------------------------------------------------------
# 功能配置：每个测量功能可用的量程与设置（依据手册 Table 5-2 / 6.7.3 R 命令表）
#   features: shunt 分流测电流 · limits 软件限值 · integration 积分时间 · null NULL 相对值
#             cont_threshold 通断阈值；AC 滤波/输入阻抗等由高级设置项的 applies 决定
# ---------------------------------------------------------------------------
_RNG_DCV = [("R3", "100 mV"), ("R4", "1000 mV"), ("R5", "10 V"), ("R6", "100 V"), ("R7", "1000 V")]
_RNG_ACV = _RNG_DCV[:4] + [("R7", "700 V")]
_RNG_RES = [("R3", "100 Ω"), ("R4", "1000 Ω"), ("R5", "10 kΩ"), ("R6", "100 kΩ"),
            ("R7", "1000 kΩ"), ("R8", "10 MΩ"), ("R9", "100 MΩ")]
_RNG_RES_LP = _RNG_RES[:6]
_RNG_I = [("R4", "1000 µA"), ("R5", "10 mA"), ("R6", "100 mA"), ("R7", "1000 mA"), ("R8", "3 A")]
_RNG_FREQ = [(c, "输入 " + label) for c, label in _RNG_ACV]

_F_DC = ["limits", "integration", "null"]
_F_AC = ["limits", "null"]
_F_RES = ["integration", "null"]

FUNCTION_PROFILES = {
    "DCV": {"ranges": _RNG_DCV, "features": ["shunt"] + _F_DC},
    "DCV_BCH": {"fixed_range": "10 V", "features": _F_DC},
    "ACV": {"ranges": _RNG_ACV, "features": _F_AC},
    "ACV_DC": {"ranges": _RNG_ACV, "features": _F_AC},
    "RES2W": {"ranges": _RNG_RES, "features": _F_RES},
    "RES4W": {"ranges": _RNG_RES, "features": _F_RES},
    "RES2W_LP": {"ranges": _RNG_RES_LP, "features": _F_RES},
    "RES4W_LP": {"ranges": _RNG_RES_LP, "features": _F_RES},
    "CONT": {"fixed_range": "1000 Ω", "features": ["integration", "cont_threshold"]},
    "DCI": {"ranges": _RNG_I, "features": _F_DC},
    "ACI": {"ranges": _RNG_I, "features": _F_AC},
    "ACI_DC": {"ranges": _RNG_I, "features": _F_AC},
    "DIODE": {"fixed_range": "1000 mV", "features": _F_DC},
    "FREQ": {"ranges": _RNG_FREQ, "features": ["null"]},
}


def function_ranges(function):
    """该功能可选的量程 [{code, label}]；固定量程功能返回空列表。"""
    prof = FUNCTION_PROFILES.get(function or "")
    if not prof or prof.get("fixed_range"):
        return []
    return [{"code": "AUTO", "label": "自动"}] + [
        {"code": c, "label": label} for c, label in prof["ranges"]]


def range_label(function, code):
    prof = FUNCTION_PROFILES.get(function or "")
    if prof and prof.get("fixed_range"):
        return prof["fixed_range"]
    if code == "AUTO":
        return "自动"
    for item in function_ranges(function):
        if item["code"] == code:
            return item["label"]
    return dmm7461a.RANGES.get(code, (None, code))[1] if code else None


def has_feature(function, feature: str) -> bool:
    return feature in FUNCTION_PROFILES.get(function or "", {}).get("features", [])


def check_range_for_function(function, code):
    """量程是否适用于当前功能；不适用时抛 ValueError。"""
    prof = FUNCTION_PROFILES.get(function or "")
    if not prof:
        return
    if prof.get("fixed_range"):
        raise ValueError(f"{dmm7461a.FUNCTIONS[function][3]}为固定量程（{prof['fixed_range']}）")
    allowed = [r["code"] for r in function_ranges(function)]
    if code not in allowed:
        raise ValueError(f"{dmm7461a.FUNCTIONS[function][3]}可选量程：{', '.join(allowed)}")


META = {
    "functions": [
        {"code": code, "label": meta[3], "unit": UNIT_LABEL.get(meta[2], meta[2]),
         "note": FUNCTION_NOTES.get(code, ""),
         "ranges": function_ranges(code),
         "fixed_range": FUNCTION_PROFILES.get(code, {}).get("fixed_range"),
         "features": FUNCTION_PROFILES.get(code, {}).get("features", [])}
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
                ["NL0", "关"], ["NL1", "开"]], state_after=True),
            _adv("null_const", "NULL 常数", "number", "KNL{}", "KNL?", fmt="sci",
                 action="null_from_latest", action_label="取当前读数",
                 hint="NULL 关闭时常数无效；「取当前读数」会同时打开 NULL",
                 state_after=True),
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
            _adv("max", "MAX 值", "readout", query="MAX?", hint="需先开启 MAX/MIN 计算",
                 first_message=True),
            _adv("min", "MIN 值", "readout", query="MIN?", hint="需先开启 MAX/MIN 计算",
                 first_message=True),
            _adv("ave", "平均值", "readout", query="AVE?", hint="需先开启 MAX/MIN 计算",
                 first_message=True),
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
            _adv("stb", "状态字节 STB", "readout", query="*STB?", first_message=True),
            _adv("esr", "事件状态 ESR", "readout", query="*ESR?", first_message=True),
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
        self.min_t = None
        self.max_t = None
        self.mean = 0.0
        self.m2 = 0.0
        self.sum = 0.0
        self.sum_sq = 0.0
        self.sum_t = 0.0
        self.sum_tt = 0.0
        self.sum_tx = 0.0
        self.first = None
        self.last = None
        self.first_t = None
        self.last_t = None
        self.since = time.time()

    def add(self, value, ol: bool, t: float | None = None):
        now = time.time() if t is None else t
        self.n_total += 1
        if ol or value is None:
            self.n_ol += 1
            return
        self.n += 1
        if self.min is None or value < self.min:
            self.min = value
            self.min_t = now
        if self.max is None or value > self.max:
            self.max = value
            self.max_t = now
        if self.first is None:
            self.first = value
            self.first_t = now
        self.last = value
        self.last_t = now
        self.sum += value
        self.sum_sq += value * value
        rt = now - self.since
        self.sum_t += rt
        self.sum_tt += rt * rt
        self.sum_tx += rt * value
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)

    def as_dict(self):
        n = self.n
        std = math.sqrt(self.m2 / (n - 1)) if n > 1 else 0.0
        std_pop = math.sqrt(self.m2 / n) if n else 0.0
        var_sample = self.m2 / (n - 1) if n > 1 else 0.0
        rms = math.sqrt(self.sum_sq / n) if n else None
        pp = (self.max - self.min) if n else 0.0
        denom = n * self.sum_tt - self.sum_t * self.sum_t
        slope = ((n * self.sum_tx - self.sum_t * self.sum) / denom
                 if n > 1 and abs(denom) > 1e-12 else 0.0)
        max_abs = max(abs(self.min), abs(self.max)) if n else None
        crest = (max_abs / rms) if (rms and rms > 0) else None
        duration = ((self.last_t - self.first_t)
                    if (self.first_t is not None and self.last_t is not None) else 0.0)
        return {
            "ctx": self.context_id,
            "n": n,
            "n_total": self.n_total,
            "n_ol": self.n_ol,
            "min": self.min,
            "max": self.max,
            "avg": self.mean if n else None,
            "pp": pp,
            "std": std,
            "std_pop": std_pop,
            "var_sample": var_sample,
            "rms": rms,
            "sum": self.sum if n else None,
            "first": self.first,
            "last": self.last,
            "min_t": self.min_t,
            "max_t": self.max_t,
            "slope": slope,
            "crest": crest,
            "ol_ratio": (self.n_ol / self.n_total) if self.n_total else 0.0,
            "duration": duration,
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
        self._ext_cache = None
        self._pending_samples: list = []
        self._last_sample_push = 0.0
        self.config = {"display_mode": "voltage", "shunt_ohm": 0.1}
        self.cont_threshold = None   # 通断阈值（Ω），读/写 KOM 时更新
        self.preset_released = False  # 上次退出时是否处于“完全释放”状态
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
        self._ext_cache = None
        self._pending_samples.clear()
        self.stats.reset(unit, self.context_id)
        LOG.info("测量上下文切换 #%s %s", self.context_id, reason)

    def _sample_rate(self) -> float:
        if not self._rate_times:
            return 0.0
        now = time.time()
        cutoff = now - 2.0
        n = sum(1 for t in self._rate_times if t >= cutoff)
        return n / 2.0

    def _extended_stats(self) -> dict:
        """中位数/MAD/平均绝对偏差需要排序，缓存 1 秒，避免每个样本都计算。"""
        now = time.time()
        if self._ext_cache and now - self._ext_cache[0] < 1.0:
            return self._ext_cache[1]
        vals = [s["d"] for s in self.samples
                if s["ctx"] == self.context_id and s["d"] is not None and not s["ol"]]
        # 限制排序规模，避免高速采样时周期性卡顿
        if len(vals) > 10000:
            vals = vals[-10000:]
        if vals:
            vals_sorted = sorted(vals)
            n = len(vals_sorted)
            median = (vals_sorted[n // 2] if n % 2
                      else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2)
            absdev = sorted(abs(v - median) for v in vals)
            mad = (absdev[n // 2] if n % 2
                   else (absdev[n // 2 - 1] + absdev[n // 2]) / 2)
            mean = sum(vals) / n
            mean_abs_dev = sum(abs(v - mean) for v in vals) / n
        else:
            median = mad = mean_abs_dev = None
        data = {"median": median, "mad": mad, "mean_abs_dev": mean_abs_dev}
        self._ext_cache = (now, data)
        return data

    def _stats_payload(self) -> dict:
        payload = self.stats.as_dict()
        payload.update(self._extended_stats())
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
            sample_t = raw_sample.get("t", time.time())
            self._rate_times.append(sample_t)
            self.stats.add(display, ol, sample_t)

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

            self._pending_samples.append(sample)
            now = time.time()
            # 低速批量推送（5Hz），优先保证主机流畅
            if now - self._last_sample_push >= 0.2:
                self._flush_samples_locked(now)
            if now - self.last_stats_push >= 0.5:
                self.last_stats_push = now
                self._flush_samples_locked(now)
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
                    "range_label": range_label(device.get("function"), device.get("range")),
                    "rate": device.get("rate"),
                    "rate_label": device.get("rate_label"),
                    "trigger": device.get("trigger"),
                    "trigger_label": device.get("trigger_label"),
                    "idn": device.get("idn"),
                    "serial": device.get("serial"),
                },
                "null": {
                    "enabled": device.get("null") == "NL1",
                    "const": device.get("null_const"),
                    "raw": device.get("null"),
                },
                "config": dict(self.config),
                "limits": json.loads(json.dumps(self.limits)),
                "alarm": self.last_alarm,
                "cont_threshold": self.cont_threshold,
                "released": bool(self.dmm and getattr(self.dmm, "released", False)),
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
            self._ext_cache = None
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

    def _flush_samples_locked(self, now: float | None = None):
        if not self._pending_samples:
            return
        batch = self._pending_samples
        self._pending_samples = []
        self._last_sample_push = now or time.time()
        self._broadcast_locked("samples", batch)

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
            payload = {"config": self.config, "limits": self.limits,
                       "released": bool(self.dmm and getattr(self.dmm, "released", False))}
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
    hub.preset_released = bool(payload.get("released"))
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
    if hub.preset_released:
        dmm.preset_released(True)
        LOG.info("按上次设置保持释放状态：不控制仪器，只接收数据")
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
def _asset_version() -> str:
    """静态资源版本号（取 CSS/JS 修改时间），避免浏览器混用新旧前端文件。"""
    static = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    stamps = []
    for name in ("app.js", "style.css"):
        try:
            stamps.append(int(os.path.getmtime(os.path.join(static, name))))
        except OSError:
            pass
    return str(max(stamps)) if stamps else "0"


@app.get("/")
def index():
    resp = app.make_response(render_template("index.html", asset_v=_asset_version()))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


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
                if event == "samples" and isinstance(data, list) and data:
                    event_id = data[-1].get("seq")
                elif event == "sample" and isinstance(data, dict):
                    event_id = data.get("seq")
                else:
                    event_id = None
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
            code = "AUTO" if str(value).upper() in ("AUTO", "R0") else str(value).upper()
            check_range_for_function(dmm.status_snapshot().get("function"), code)
            result = dmm.set_range(code)
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
    except dmm7461a.LocalMode as exc:
        return _api_error(str(exc), "local_mode", 409)

    result = dict(result)
    result["range_label"] = range_label(result.get("function"), result.get("range"))
    if result.get("function") == "CONT" and action in ("function", "sync"):
        refresh_cont_threshold()
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

    snap = dmm.status_snapshot() if dmm else {}
    device_state = snap.get("state", "offline")
    switching_mode = mode != h.config["display_mode"]
    released = bool(getattr(dmm, "released", False))
    if mode == "current" and released and snap.get("function") != "DCV":
        return _api_error("已释放给仪器面板：请在面板切到 DCV，或先恢复控制", "local_mode", 409)
    if mode == "current" and not (released and snap.get("function") == "DCV"):
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
        except dmm7461a.LocalMode as exc:
            return _api_error(str(exc), "local_mode", 409)

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
    if h.dmm and h.dmm.status_snapshot().get("state") not in ("online", "local"):
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
    if request.args.get("start") or request.args.get("end"):
        return _log_download_range(path, name)
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


# ---------------------------------------------------------------------------
# 记录分析 / 回放：解析 CSV（带缓存），按时间范围降采样并统计
# ---------------------------------------------------------------------------
_LOG_CACHE: dict = {}
_LOG_CACHE_LOCK = threading.Lock()
LOG_CACHE_MAX = 3
ANALYSIS_MAX_POINTS = 20000


class ParsedLog:
    """一个 CSV 记录文件的列式数据（按 epoch 时间排序）。"""

    def __init__(self, path: Path):
        self.t: list = []
        self.v: list = []          # 显示值；OL 或空值为 None
        self.ol: list = []
        self.alarm: list = []      # 0 正常 / 1 超限（low/high）
        self.unit: list = []
        self.fn: list = []
        units: dict = {}
        segments: list = []
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            header = next(reader, None) or []
            idx = {name: i for i, name in enumerate(header)}
            need = ("epoch_s", "display_value", "display_unit")
            if any(k not in idx for k in need):
                raise ValueError("不是本程序生成的记录文件（缺少必要列）")
            i_t, i_v, i_u = idx["epoch_s"], idx["display_value"], idx["display_unit"]
            i_ol, i_al = idx.get("is_ol"), idx.get("alarm_state")
            i_fn, i_ctx = idx.get("function"), idx.get("context_id")
            width = len(header)
            last_key = None
            for row in reader:
                if len(row) < width:
                    continue   # 正在写入的最后一行可能不完整
                try:
                    t = float(row[i_t])
                except ValueError:
                    continue
                raw = row[i_v]
                ol = (row[i_ol] == "1") if i_ol is not None else False
                try:
                    v = None if (ol or raw == "") else float(raw)
                except ValueError:
                    v = None
                if v is not None and not math.isfinite(v):
                    v = None
                unit = row[i_u]
                fn = row[i_fn] if i_fn is not None else ""
                al = row[i_al] if i_al is not None else ""
                self.t.append(t)
                self.v.append(v)
                self.ol.append(ol or v is None)
                self.alarm.append(1 if al in ("low", "high") else 0)
                self.unit.append(unit)
                self.fn.append(fn)
                units[unit] = units.get(unit, 0) + 1
                key = (row[i_ctx] if i_ctx is not None else "", fn, unit)
                if key != last_key:
                    segments.append({"function": fn, "unit": unit, "start": t, "end": t, "rows": 0})
                    last_key = key
                segments[-1]["end"] = t
                segments[-1]["rows"] += 1
        self.units = units
        self.segments = segments
        self.rows = len(self.t)

    def meta(self) -> dict:
        start = self.t[0] if self.t else None
        end = self.t[-1] if self.t else None
        duration = (end - start) if self.t else 0.0
        return {
            "rows": self.rows,
            "start": start,
            "end": end,
            "duration": duration,
            "rate_hz": (self.rows - 1) / duration if duration > 0 else None,
            "units": [{"unit": u, "rows": n} for u, n in
                      sorted(self.units.items(), key=lambda kv: -kv[1])],
            "segments": self.segments[:200],
            "segments_total": len(self.segments),
        }


def _parsed_log(path: Path) -> ParsedLog:
    st = path.stat()
    key = str(path)
    stamp = (st.st_size, st.st_mtime_ns)
    with _LOG_CACHE_LOCK:
        hit = _LOG_CACHE.get(key)
        if hit and hit[0] == stamp:
            hit[2] = time.monotonic()
            return hit[1]
    parsed = ParsedLog(path)
    with _LOG_CACHE_LOCK:
        _LOG_CACHE[key] = [stamp, parsed, time.monotonic()]
        while len(_LOG_CACHE) > LOG_CACHE_MAX:
            oldest = min(_LOG_CACHE, key=lambda k: _LOG_CACHE[k][2])
            del _LOG_CACHE[oldest]
    return parsed


def _log_path(name: str):
    if not LOG_NAME_RE.fullmatch(name or ""):
        return None
    path = (LOG_DIR / name).resolve()
    if path.parent != LOG_DIR.resolve() or not path.is_file():
        return None
    return path


def _range_stats(values: list, n_ol: int, n_alarm: int, t0, t1) -> dict:
    n = len(values)
    out = {"n": n, "n_ol": n_ol, "n_alarm": n_alarm,
           "start": t0, "end": t1, "duration": (t1 - t0) if (t0 is not None and t1 is not None) else 0.0,
           "mean": None, "std": None, "min": None, "max": None, "pp": None, "rms": None,
           "median": None, "first": None, "last": None}
    if not n:
        return out
    mean = math.fsum(values) / n
    var = math.fsum((x - mean) ** 2 for x in values) / (n - 1) if n > 1 else 0.0
    lo, hi = min(values), max(values)
    srt = sorted(values) if n <= 2_000_000 else None
    median = None
    if srt is not None:
        mid = n // 2
        median = srt[mid] if n % 2 else (srt[mid - 1] + srt[mid]) / 2
    out.update({
        "mean": mean, "std": math.sqrt(var), "min": lo, "max": hi, "pp": hi - lo,
        "rms": math.sqrt(math.fsum(x * x for x in values) / n),
        "median": median, "first": values[0], "last": values[-1],
    })
    return out


def analyze_log(parsed: ParsedLog, unit: str | None, start: float | None,
                end: float | None, max_points: int) -> dict:
    """返回 [start, end] 内指定单位的降采样数据（每桶 min/max/mean）和统计。"""
    import bisect
    t = parsed.t
    if unit is None:
        unit = max(parsed.units, key=parsed.units.get) if parsed.units else ""
    lo_i = bisect.bisect_left(t, start) if start is not None else 0
    hi_i = bisect.bisect_right(t, end) if end is not None else len(t)
    if hi_i < lo_i:
        hi_i = lo_i
    idx = [i for i in range(lo_i, hi_i) if parsed.unit[i] == unit]

    values = []
    n_ol = n_alarm = 0
    for i in idx:
        if parsed.ol[i]:
            n_ol += 1
        else:
            values.append(parsed.v[i])
        n_alarm += parsed.alarm[i]
    t0 = t[idx[0]] if idx else start
    t1 = t[idx[-1]] if idx else end
    stats = _range_stats(values, n_ol, n_alarm, t0, t1)

    # 降采样：数量不多时原样返回；否则按时间等分桶，每桶保留 min/max/mean 与 OL/超限标记
    points = []
    raw = len(idx) <= max_points
    if raw:
        for i in idx:
            v = parsed.v[i]
            points.append([t[i], v, v, v, 1 if parsed.ol[i] else 0, parsed.alarm[i]])
    elif idx:
        span = (t1 - t0) or 1e-9
        nb = max_points
        buckets = [None] * nb
        for i in idx:
            b = min(nb - 1, int((t[i] - t0) / span * nb))
            cur = buckets[b]
            if cur is None:
                cur = buckets[b] = [t[i], None, None, 0.0, 0, 0, 0]   # t,min,max,sum,n,ol,alarm
            if parsed.ol[i]:
                cur[5] = 1
            else:
                v = parsed.v[i]
                cur[1] = v if cur[1] is None or v < cur[1] else cur[1]
                cur[2] = v if cur[2] is None or v > cur[2] else cur[2]
                cur[3] += v
                cur[4] += 1
            if parsed.alarm[i]:
                cur[6] = 1
        for cur in buckets:
            if cur is None:
                continue
            mean = cur[3] / cur[4] if cur[4] else None
            points.append([cur[0], cur[1], cur[2], mean, cur[5], cur[6]])
    fns = sorted({parsed.fn[i] for i in idx[:: max(1, len(idx) // 500)]} if idx else set())
    return {
        "unit": unit,
        "functions": fns,
        "range": {"start": t0, "end": t1, "rows": len(idx)},
        "raw": raw,
        "columns": ["t", "min", "max", "mean", "ol", "alarm"],
        "points": points,
        "stats": stats,
    }


def _float_arg(name):
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(name)
    return value


@app.get("/api/logs/<name>/analysis")
def api_log_analysis(name):
    path = _log_path(name)
    if path is None:
        return _api_error("文件不存在", "not_found", 404)
    try:
        start, end = _float_arg("start"), _float_arg("end")
        max_points = int(request.args.get("max_points", "2000"))
    except ValueError:
        return _api_error("参数无效", "bad_value")
    max_points = max(10, min(ANALYSIS_MAX_POINTS, max_points))
    try:
        parsed = _parsed_log(path)
    except (OSError, ValueError) as exc:
        return _api_error(f"无法解析记录文件：{exc}", "bad_file", 422)
    unit = request.args.get("unit")
    if unit is not None and unit not in parsed.units:
        return _api_error("文件中没有该单位的数据", "bad_value")
    result = analyze_log(parsed, unit, start, end, max_points)
    result.update({
        "ok": True,
        "name": name,
        "active": hub().logger.active and hub().logger.file == name,
        "meta": parsed.meta(),
    })
    return jsonify(result)


def _log_download_range(path: Path, name: str):
    """导出 [start, end] 时间范围内的行（按 epoch_s 过滤），保持原始列。"""
    try:
        start, end = _float_arg("start"), _float_arg("end")
    except ValueError:
        return _api_error("参数无效", "bad_value")

    def gen():
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header:
                return
            i_t = header.index("epoch_s") if "epoch_s" in header else None
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerow(header)
            for row in reader:
                if i_t is None or len(row) <= i_t:
                    continue
                try:
                    t = float(row[i_t])
                except ValueError:
                    continue
                if (start is not None and t < start) or (end is not None and t > end):
                    continue
                writer.writerow(row)
                if buf.tell() > 65536:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

    stem = name[:-4]
    return Response(gen(), mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{stem}_selection.csv"',
    })


@app.post("/api/device/reconnect")
def api_reconnect():
    try:
        hub().dmm.force_reconnect()
    except (dmm7461a.DeviceOffline, dmm7461a.LocalMode) as exc:
        return _dmm_http_error(exc)
    return jsonify({"ok": True})


@app.post("/api/device/release")
def api_release():
    """完全释放控制：不再发送任何命令，仪器面板完全可用；数据仍被动接收。"""
    h = hub()
    try:
        h.dmm.release_to_local()
    except dmm7461a.DMMError as exc:
        return _dmm_http_error(exc)
    h._save_settings()
    LOG.info("已完全释放控制给仪器面板")
    h._broadcast("state", h.state())
    return jsonify({"ok": True, "released": True, "state": h.state()})


@app.post("/api/device/resume")
def api_resume():
    """恢复上位机控制：重新同步仪器状态，回到面板优先模式。"""
    h = hub()
    try:
        h.dmm.resume_remote()
    except dmm7461a.DMMError as exc:
        return _dmm_http_error(exc)
    h._save_settings()
    LOG.info("已恢复上位机控制")
    h._broadcast("state", h.state())
    return jsonify({"ok": True, "released": False, "state": h.state()})


def _dmm_http_error(exc):
    if isinstance(exc, dmm7461a.LocalMode):
        return _api_error(str(exc), "local_mode", 409)
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
            values[key] = {"ok": True, "value": dmm.query_command(
                item["query"], first_message=item.get("first_message", False))}
            if key == "cont_limit" and _parse_kom(values[key]["value"]) is not None:
                hub().cont_threshold = _parse_kom(values[key]["value"])
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
    except (dmm7461a.DMMError, ValueError) as exc:
        return _dmm_http_error(exc)

    reply = None
    warning = None
    try:
        if item.get("query"):
            reply = dmm.query_command(item["query"],
                                      first_message=item.get("first_message", False))
    except dmm7461a.DMMError:
        warning = "设置已发送，但回读超时；稍后会自动同步"
    if item.get("state_after"):
        try:
            dmm.sync_state()
        except dmm7461a.DMMError:
            warning = warning or "设置已发送，状态回读超时；稍后会自动同步"
    if key == "cont_limit":
        h = hub()
        h.cont_threshold = _parse_kom(reply) or int(formatted)
        h._broadcast("state", h.state())
    return jsonify({"ok": True, "sent": cmd, "value": reply, "warning": warning})


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
    warning = None
    reference = None
    if action == "null_from_latest":
        # NULL 开启时数据流输出的是相对值，先关闭 NULL 取一次绝对读数，
        # 再以该绝对读数作为参考重新开启 NULL。
        try:
            dmm.send_command("NL0")
        except (dmm7461a.DMMError, ValueError) as exc:
            return _dmm_http_error(exc)
        with h.lock:
            before_seq = h.seq
        time.sleep(0.8)
        with h.lock:
            latest = h.samples[-1] if h.samples else None
            latest_seq = latest["seq"] if latest else 0
        if not latest or latest.get("ol") or latest.get("v") is None:
            try:
                dmm.send_command("NL1")
            except (dmm7461a.DMMError, ValueError):
                pass
            return _api_error("当前没有有效读数，无法取当前值", "no_value", 409)
        reference = latest["v"]
        cmd = f"KNL{latest['v']:.9E}"
        try:
            dmm.send_command("NL1")
            dmm.send_command(cmd)
        except (dmm7461a.DMMError, ValueError) as exc:
            return _dmm_http_error(exc)
        if latest_seq <= before_seq:
            warning = "等待绝对读数超时，已按最近一次读数设置"
    else:
        cmd = action
        try:
            dmm.send_command(cmd)
        except (dmm7461a.DMMError, ValueError) as exc:
            return _dmm_http_error(exc)

    reply = None
    try:
        if item.get("query"):
            reply = dmm.query_command(item["query"],
                                      first_message=item.get("first_message", False))
    except dmm7461a.DMMError:
        warning = warning or "动作已执行，但回读超时；稍后会自动同步"
    if item.get("state_after"):
        try:
            dmm.sync_state()
        except dmm7461a.DMMError:
            warning = warning or "动作已执行，状态回读超时；稍后会自动同步"
    return jsonify({"ok": True, "sent": cmd, "value": reply,
                    "warning": warning, "reference": reference})


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
        # host=False：模拟面板按键造成的外部改动
        hub().dmm.send_command(cmd, host=False)
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
# Agent 接口：为 AI agent / 脚本设计的高层接口（一次调用完成“设置 → 等待 → 取数”）
# 详见 docs/AGENT.md；MCP 服务器 mcp_server.py 也是基于这些接口。
# ---------------------------------------------------------------------------
def _parse_kom(reply):
    m = re.search(r"KOM\s*\+?(\d+(?:\.\d+)?)", str(reply or ""), re.I)
    return int(float(m.group(1))) if m else None


def refresh_cont_threshold():
    """从仪器回读通断阈值并缓存；失败时返回缓存值。"""
    h = hub()
    try:
        value = _parse_kom(h.dmm.query_command("KOM?"))
    except dmm7461a.DMMError:
        return h.cont_threshold
    if value is not None and value != h.cont_threshold:
        h.cont_threshold = value
        h._broadcast("state", h.state())
    return h.cont_threshold


AGENT_MAX_SAMPLES = 5000
AGENT_MAX_WAIT = 120.0


def _agent_reading(s: dict | None) -> dict | None:
    """把内部采样转换为 agent 易读的结构。"""
    if not s:
        return None
    return {
        "seq": s["seq"],
        "time": s["t"],
        "age_s": round(max(0.0, time.time() - s["t"]), 3),
        "value": None if s.get("ol") else s.get("d"),
        "unit": s.get("u"),
        "overload": bool(s.get("ol")),
        "raw_value": s.get("v"),
        "raw_unit": s.get("fu"),
        "current_a": s.get("i"),
        "function": s.get("fn"),
        "range": s.get("rng"),
        "rate": s.get("rate"),
        "alarm": s.get("alarm"),
        "context_id": s.get("ctx"),
        **({"continuity": (not s.get("ol") and s.get("v") is not None
                           and hub().cont_threshold is not None
                           and s["v"] < hub().cont_threshold),
            "cont_threshold": hub().cont_threshold}
           if s.get("fn") == "CONT" else {}),
    }


def _agent_summary(values: list, n_ol: int, unit) -> dict:
    n = len(values)
    if not n:
        return {"n": 0, "n_ol": n_ol, "unit": unit, "mean": None, "std": None,
                "min": None, "max": None, "pp": None, "rms": None}
    mean = sum(values) / n
    var = sum((x - mean) ** 2 for x in values) / (n - 1) if n > 1 else 0.0
    return {
        "n": n,
        "n_ol": n_ol,
        "unit": unit,
        "mean": mean,
        "std": math.sqrt(var),
        "min": min(values),
        "max": max(values),
        "pp": max(values) - min(values),
        "rms": math.sqrt(sum(x * x for x in values) / n),
    }


def _agent_float(data: dict, key: str, default, lo: float, hi: float):
    value = data.get(key, default)
    if value is None:
        return None
    if not _valid_number(value):
        raise ValueError(f"{key} 必须是数字")
    value = float(value)
    if not (lo <= value <= hi):
        raise ValueError(f"{key} 必须在 {lo:g} ~ {hi:g} 之间")
    return value


def _latest_seq() -> int:
    h = hub()
    with h.lock:
        return h.seq


def _wait_samples(after_seq: int, count: int | None, seconds: float | None,
                  timeout: float) -> tuple[list, bool]:
    """收集 seq > after_seq 的新样本，直到凑够 count 个或经过 seconds 秒。

    返回 (样本列表, 是否超时)。只保留最后一个上下文（功能/单位）内的样本，
    避免把切换前后的读数混在一起。
    """
    h = hub()
    start = time.monotonic()
    deadline = start + timeout
    end_at = start + seconds if seconds else None
    collected: list = []
    last = after_seq
    timed_out = False
    while True:
        with h.lock:
            fresh = [s for s in h.samples if s["seq"] > last] if h.seq > last else []
        if fresh:
            last = fresh[-1]["seq"]
            collected.extend(fresh)
            ctx = collected[-1]["ctx"]
            if collected[0]["ctx"] != ctx:
                collected = [s for s in collected if s["ctx"] == ctx]
        now = time.monotonic()
        if count and len(collected) >= count:
            collected = collected[:count]
            break
        if end_at is not None and now >= end_at:
            break
        if now >= deadline:
            timed_out = True
            break
        time.sleep(0.02)
    return collected[:AGENT_MAX_SAMPLES], timed_out


def _agent_status() -> dict:
    h = hub()
    if (h.dmm and h.dmm.status_snapshot().get("function") == "CONT"
            and h.cont_threshold is None):
        refresh_cont_threshold()
    st = h.state()
    with hub().lock:
        latest = hub().samples[-1] if hub().samples else None
    stats = st["stats"]
    return {
        "device": {
            "state": st["device"].get("state"),
            "message": st["device"].get("message"),
            "simulator": st["sim"],
            "released": st["released"],
            "idn": st["instrument"].get("idn"),
        },
        "instrument": st["instrument"],
        "null": st["null"],
        "config": st["config"],
        "limits": st["limits"],
        "alarm": st["alarm"],
        "cont_threshold": st["cont_threshold"],
        "available_ranges": function_ranges(st["instrument"].get("function")),
        "features": FUNCTION_PROFILES.get(st["instrument"].get("function") or "", {}).get("features", []),
        "logging": st["logging"],
        "latest": _agent_reading(latest),
        "stats": stats,
        "context_id": st["context_id"],
    }


@app.get("/api/agent")
def api_agent_manifest():
    """自描述清单：agent 先读这个就知道能做什么、参数取值范围是什么。"""
    return jsonify({
        "name": "ADCMT 7461A 数字万用表",
        "description": "通过本上位机控制 7461A 并读取测量数据。所有设置会同步到网页界面。",
        "docs": "docs/AGENT.md",
        "endpoints": [
            {"method": "GET", "path": "/api/agent", "desc": "本清单"},
            {"method": "GET", "path": "/api/agent/status", "desc": "设备、设置、报警、记录、最新读数、统计"},
            {"method": "GET", "path": "/api/agent/reading",
             "desc": "最新读数；fresh=1 时等待一个新样本（timeout 秒，默认 5）"},
            {"method": "POST", "path": "/api/agent/measure",
             "desc": "采集新样本并返回统计：{count|seconds, settle, timeout, include_samples}"},
            {"method": "POST", "path": "/api/agent/setup",
             "desc": "一次设置 {function, range, rate, trigger, display_mode, shunt_ohm, cont_threshold}，可带 settle 后读一次"},
            {"method": "GET", "path": "/api/history", "desc": "历史样本 ?seconds=60"},
            {"method": "POST", "path": "/api/limits", "desc": "上下限报警 {mode, enabled, low, high}"},
            {"method": "POST", "path": "/api/stats/reset", "desc": "重置统计"},
            {"method": "POST", "path": "/api/log/start", "desc": "开始 CSV 记录"},
            {"method": "POST", "path": "/api/log/stop", "desc": "停止 CSV 记录"},
            {"method": "GET", "path": "/api/logs", "desc": "记录文件列表"},
            {"method": "GET", "path": "/api/logs/<name>/download", "desc": "下载 CSV（可加 ?start=&end= 导出时间段）"},
            {"method": "GET", "path": "/api/logs/<name>/analysis",
             "desc": "记录分析：?unit=&start=&end=&max_points= 返回降采样曲线(min/max/mean)、统计与分段信息"},
            {"method": "GET", "path": "/api/advanced", "desc": "高级设置项清单"},
            {"method": "GET", "path": "/api/advanced/read", "desc": "读高级设置 ?keys=a,b"},
            {"method": "POST", "path": "/api/advanced/set", "desc": "写高级设置 {key, value}"},
            {"method": "POST", "path": "/api/advanced/action", "desc": "执行动作 {key}"},
            {"method": "POST", "path": "/api/device/reconnect", "desc": "强制重连"},
            {"method": "POST", "path": "/api/device/release",
             "desc": "完全释放控制给仪器面板（之后设置类接口返回 local_mode，数据仍可读取/记录）"},
            {"method": "POST", "path": "/api/device/resume", "desc": "恢复上位机控制"},
        ],
        "options": META,
        "notes": [
            "value 为显示值（分流电流模式下是电流 A），overload=true 时 value 为 null。",
            "分流电流模式（display_mode=current）只能用 DCV 功能。",
            "量程随功能不同：options.functions[].ranges 列出每个功能可选量程，fixed_range 表示固定量程。",
            "通断阈值（cont_threshold，1~1000 Ω）只在 CONT 功能下可设，低于阈值判定为导通。",
            "切换功能/量程后读数需要时间稳定，measure/setup 的 settle 参数用于等待。",
            "错误统一返回 {ok:false, error, code}，code 例如 offline/busy/timeout/local_mode/bad_value/mode_conflict。",
        ],
    })


@app.get("/api/agent/status")
def api_agent_status():
    return jsonify({"ok": True, **_agent_status()})


@app.get("/api/agent/reading")
def api_agent_reading():
    fresh = request.args.get("fresh", "0").lower() in ("1", "true", "yes")
    try:
        raw = request.args.get("timeout")
        timeout = _agent_float({"timeout": float(raw) if raw else 5}, "timeout",
                               5, 0.1, AGENT_MAX_WAIT)
    except ValueError as exc:
        if "timeout" not in str(exc):
            exc = ValueError("timeout 必须是数字")
        return _api_error(str(exc), "bad_value")
    h = hub()
    if fresh:
        samples, timed_out = _wait_samples(_latest_seq(), 1, None, timeout)
        if timed_out or not samples:
            return _api_error(f"{timeout:g} 秒内没有收到新读数", "no_data", 504)
        sample = samples[-1]
    else:
        with h.lock:
            sample = h.samples[-1] if h.samples else None
        if sample is None:
            return _api_error("还没有读数", "no_data", 409)
    return jsonify({"ok": True, "reading": _agent_reading(sample),
                    "device_state": h.dmm.status_snapshot().get("state") if h.dmm else None})


@app.post("/api/agent/measure")
def api_agent_measure():
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return _api_error("请求必须是 JSON 对象", "bad_request")
    try:
        count = data.get("count")
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, (int, float)) or int(count) != count:
                raise ValueError("count 必须是整数")
            count = int(count)
            if not (1 <= count <= AGENT_MAX_SAMPLES):
                raise ValueError(f"count 必须在 1 ~ {AGENT_MAX_SAMPLES} 之间")
        seconds = _agent_float(data, "seconds", None, 0.05, AGENT_MAX_WAIT)
        if count is None and seconds is None:
            count = 10
        settle = _agent_float(data, "settle", 0, 0, 60)
        default_timeout = (seconds or 0) + max(10.0, (count or 0) * 2.0)
        timeout = _agent_float(data, "timeout", min(default_timeout, AGENT_MAX_WAIT),
                               0.1, AGENT_MAX_WAIT + 60)
    except ValueError as exc:
        return _api_error(str(exc), "bad_value")

    h = hub()
    if h.dmm and h.dmm.status_snapshot().get("state") not in ("online", "local"):
        return _api_error("设备离线，无法测量", "offline", 503)
    if settle:
        time.sleep(settle)
    started = time.time()
    samples, timed_out = _wait_samples(_latest_seq(), count, seconds, timeout)
    if not samples:
        return _api_error("等待期间没有收到读数", "no_data", 504)

    values = [s["d"] for s in samples if not s.get("ol") and s.get("d") is not None]
    n_ol = len(samples) - len(values)
    last = samples[-1]
    duration = samples[-1]["t"] - samples[0]["t"] if len(samples) > 1 else 0.0
    result = {
        "ok": True,
        "complete": not timed_out,
        "requested": {"count": count, "seconds": seconds, "settle": settle},
        "summary": _agent_summary(values, n_ol, last.get("u")),
        "function": last.get("fn"),
        "range": last.get("rng"),
        "rate": last.get("rate"),
        "display_mode": h.config["display_mode"],
        "context_id": last.get("ctx"),
        "started": started,
        "duration_s": duration,
        "sample_rate_hz": (len(samples) - 1) / duration if duration > 0 else None,
        "last": _agent_reading(last),
    }
    if timed_out:
        result["warning"] = f"超时：只收到 {len(samples)} 个样本"
    if data.get("include_samples"):
        result["samples"] = [
            {"t": s["t"], "value": None if s.get("ol") else s.get("d"),
             "overload": bool(s.get("ol")), "alarm": s.get("alarm")}
            for s in samples
        ]
    return jsonify(result)


@app.post("/api/agent/setup")
def api_agent_setup():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _api_error("请求必须是 JSON 对象", "bad_request")
    known = {"function", "range", "rate", "trigger", "display_mode", "shunt_ohm",
             "cont_threshold", "settle"}
    unknown = sorted(set(data) - known)
    if unknown:
        return _api_error(f"未知参数：{', '.join(unknown)}", "bad_request")
    try:
        settle = _agent_float(data, "settle", 0, 0, 60)
    except ValueError as exc:
        return _api_error(str(exc), "bad_value")

    h = hub()
    dmm = h.dmm
    applied = []
    fn = str(data["function"]).upper() if data.get("function") is not None else None
    mode = data.get("display_mode")
    shunt = data.get("shunt_ohm")
    target_mode = mode if mode is not None else h.config["display_mode"]
    if mode is not None and mode not in ("voltage", "current"):
        return _api_error("display_mode 只能是 voltage 或 current", "bad_value")
    if shunt is not None and (not _valid_number(shunt)
                              or not (SHUNT_MIN <= float(shunt) <= SHUNT_MAX)):
        return _api_error(f"分流电阻必须在 {SHUNT_MIN:g} ~ {SHUNT_MAX:g} Ω 之间", "bad_value")
    if target_mode == "current" and fn not in (None, "DCV"):
        return _api_error("分流电流模式必须使用直流电压档（DCV）", "mode_conflict", 409)

    try:
        # 先切显示模式（进入分流模式会强制 DCV），再设置功能/量程/速率/触发
        if mode is not None or shunt is not None:
            if target_mode == "current":
                if dmm.status_snapshot().get("state") != "online":
                    return _api_error("设备离线，无法进入分流电流模式", "offline", 503)
                dmm.set_function("DCV")
            h.set_config(display_mode=target_mode,
                         shunt_ohm=float(shunt) if shunt is not None else h.config["shunt_ohm"])
            applied += [k for k in ("display_mode", "shunt_ohm") if data.get(k) is not None]
        if fn is not None:
            dmm.set_function(fn)
            applied.append("function")
        if data.get("range") is not None:
            rng = str(data["range"]).upper()
            rng = "AUTO" if rng in ("AUTO", "R0") else rng
            check_range_for_function(dmm.status_snapshot().get("function"), rng)
            dmm.set_range(rng)
            applied.append("range")
        if data.get("cont_threshold") is not None:
            th = data["cont_threshold"]
            if not _valid_number(th) or not (1 <= float(th) <= 1000):
                raise ValueError("cont_threshold 必须在 1 ~ 1000 Ω 之间")
            if dmm.status_snapshot().get("function") != "CONT":
                raise ValueError("通断阈值只在通断功能（CONT）下设置")
            dmm.send_command(f"KOM{int(round(float(th)))}")
            h.cont_threshold = int(round(float(th)))
            refresh_cont_threshold()
            applied.append("cont_threshold")
        if data.get("rate") is not None:
            dmm.set_rate(str(data["rate"]))
            applied.append("rate")
        if data.get("trigger") is not None:
            dmm.set_trigger(str(data["trigger"]))
            applied.append("trigger")
    except (dmm7461a.DMMError, ValueError) as exc:
        resp, status = _dmm_http_error(exc)
        body = resp.get_json()
        body["applied"] = applied
        return jsonify(body), status

    result = {"ok": True, "applied": applied}
    if settle:
        time.sleep(settle)
        samples, _ = _wait_samples(_latest_seq(), 1, None, 10)
        result["reading"] = _agent_reading(samples[-1]) if samples else None
    result.update({k: v for k, v in _agent_status().items()
                   if k in ("instrument", "config", "null", "device")})
    return jsonify(result)


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
