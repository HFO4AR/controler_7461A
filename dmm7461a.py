#!/usr/bin/env python3
"""ADCMT 7461A 数字万用表 USB (USBTMC) 驱动。

已验证的协议要点：
- 设备节点 /dev/usbtmc0，内核 usbtmc 驱动。
- 仪器默认处于 LOCAL 状态，必须先发送 USB488 REN_CONTROL（远程使能），
  否则它只会以约 20 次/秒连续输出测量数据，完全忽略命令。
- 命令为 ASCII，以 '\\n' 结尾；查询命令发出后至少等待 20ms 再读取应答。
- USB 模式只支持 ADCMT 的 ADC 命令语言（SCPI 仅 GPIB 可用）。
- 测量数据与查询应答共用读取流，需要区分。
- H1 开启表头后样本形如 "DCV_ +018.7214E-03"。
- 超量程值为 +9.999999E+37，面板显示 OL。

单独自检：
    python dmm7461a.py --selftest
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import logging
import os
import queue
import re
import struct
import sys
import threading
import time

LOG = logging.getLogger("dmm7461a")

DEVICE_PATH = "/dev/usbtmc0"

# ---------------------------------------------------------------------------
# ioctl 常量（<linux/usb/tmc.h>）
# ---------------------------------------------------------------------------
def _ioc(direction: int, nr_type: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (nr_type << 8) | nr


USBTMC_IOCTL_CLEAR = _ioc(0, 91, 2, 0)            # 0x5B02
USBTMC_IOCTL_SET_TIMEOUT = _ioc(1, 91, 10, 4)     # 0x40045B0A
USBTMC_IOCTL_CTRL_REQUEST = _ioc(3, 91, 8, 16)    # 0xC0105B08

# USB488 类请求
USB488_REN_CONTROL = 0xA0
USB488_GOTO_LOCAL = 0xA1
USBTMC_STATUS_SUCCESS = 0x01

READ_TIMEOUT_MS = 250
QUERY_DELAY_S = 0.03
QUERY_TIMEOUT_S = 1.5
OL_THRESHOLD = 9.9e37


class DMMError(Exception):
    """驱动错误基类。"""

    code = "device_error"


class DeviceOffline(DMMError):
    code = "offline"


class DeviceNotReady(DMMError):
    code = "not_ready"


class QueryTimeout(DMMError):
    code = "timeout"


class BusyError(DMMError):
    code = "busy"


class LocalMode(DMMError):
    code = "local_mode"


class _Disconnected(Exception):
    """内部：USB 通讯中断，需要重连。"""


# ---------------------------------------------------------------------------
# 命令表
# ---------------------------------------------------------------------------
# name: (命令, 功能代码, 基本单位, 中文名)
FUNCTIONS = {
    "DCV": ("F1", 1, "V", "直流电压"),
    "ACV": ("F2", 2, "V", "交流电压"),
    "RES2W": ("F3", 3, "OHM", "二线电阻"),
    "RES4W": ("F4", 4, "OHM", "四线电阻"),
    "DCI": ("F5", 5, "A", "直流电流"),
    "ACI": ("F6", 6, "A", "交流电流"),
    "ACV_DC": ("F7", 7, "V", "交流+直流电压"),
    "ACI_DC": ("F8", 8, "A", "交流+直流电流"),
    "DCV_BCH": ("F12", 12, "V", "直流电压 Bch"),
    "DIODE": ("F13", 13, "V", "二极管"),
    "RES2W_LP": ("F20", 20, "OHM", "低功耗二线电阻"),
    "RES4W_LP": ("F21", 21, "OHM", "低功耗四线电阻"),
    "CONT": ("F22", 22, "OHM", "通断"),
    "FREQ": ("F50", 50, "Hz", "频率"),
}
FUNCTION_BY_CODE = {v[1]: k for k, v in FUNCTIONS.items()}
FUNCTION_BY_TAG = {
    "DCV_": "DCV",
    "ACV_": "ACV",
    "DCI_": "DCI",
    "ACI_": "ACI",
    "R2W_": "RES2W",
    "OHM_": "RES2W",
    "OHM2_": "RES2W",
    "R4W_": "RES4W",
    "OHM4_": "RES4W",
    "R2L_": "RES2W_LP",
    "R4L_": "RES4W_LP",
    "ADV_": "ACV_DC",
    "BDV_": "DCV_BCH",
    "DOD_": "DIODE",
    "DIOD_": "DIODE",
    "DIODE_": "DIODE",
    "RCT_": "CONT",
    "CONT_": "CONT",
    "FREQ_": "FREQ",
}

# code: (命令, 中文名)
RANGES = {
    "AUTO": ("R0", "自动"),
    "R3": ("R3", "100 mV"),
    "R4": ("R4", "1000 mV"),
    "R5": ("R5", "10 V"),
    "R6": ("R6", "100 V"),
    "R7": ("R7", "1000 V / 700 V"),
    "R8": ("R8", "10 MΩ / 3 A"),
    "R9": ("R9", "100 MΩ"),
}

RATES = {
    "PR0": ("PR0", "自由"),
    "PR1": ("PR1", "快速1"),
    "PR2": ("PR2", "快速2"),
    "PR3": ("PR3", "中速"),
    "PR4": ("PR4", "慢速1"),
    "PR5": ("PR5", "慢速2"),
}

TRIGGERS = {
    "TRS0": ("TRS0", "立即"),
    "TRS1": ("TRS1", "手动"),
    "TRS2": ("TRS2", "外部"),
    "TRS3": ("TRS3", "总线"),
    "TRS4": ("TRS4", "电平"),
    "TRS5": ("TRS5", "差值"),
}

_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"
_H1_RE = re.compile(rf"^(?P<tag>[A-Za-z0-9_+\-]+)\s+(?P<num>{_NUM})$")
_BARE_RE = re.compile(rf"^(?P<num>{_NUM})$")
_FUNC_RE = re.compile(r"^F0*(\d+)$")
_RANGE_RE = re.compile(r"^R(\d+)$")
_RATE_RE = re.compile(r"^PR(\d+)$")
_TRIG_RE = re.compile(r"^TRS(\d+)$")


def parse_message(line: str):
    """返回 (kind, payload)。

    kind == "sample" 时 payload 为 dict（value/tag/ol/raw）；
    kind == "reply" 时 payload 为原始字符串。
    """
    m = _H1_RE.match(line)
    if m:
        value = float(m.group("num"))
        return "sample", {
            "tag": m.group("tag"),
            "value": value,
            "ol": abs(value) >= OL_THRESHOLD,
            "raw": line,
        }
    m = _BARE_RE.match(line)
    if m:
        value = float(m.group("num"))
        return "sample", {
            "tag": None,
            "value": value,
            "ol": abs(value) >= OL_THRESHOLD,
            "raw": line,
        }
    return "reply", line


def _ren_control(fd: int, enable: bool = True) -> int:
    """发送 USB488 REN_CONTROL（远程使能/撤销），返回状态字节。"""
    data = ctypes.create_string_buffer(1)
    req = struct.pack("<BBHHH", 0xA1, USB488_REN_CONTROL, 1 if enable else 0, 0, 1)
    req += struct.pack("<Q", ctypes.addressof(data))
    fcntl.ioctl(fd, USBTMC_IOCTL_CTRL_REQUEST, bytearray(req), True)
    return data.raw[0]


def _goto_local_control(fd: int):
    """发送 USB488 GOTO_LOCAL（部分固件可能不支持，失败可忽略）。"""
    data = ctypes.create_string_buffer(1)
    req = struct.pack("<BBHHH", 0x21, USB488_GOTO_LOCAL, 0, 0, 0)
    req += struct.pack("<Q", ctypes.addressof(data))
    fcntl.ioctl(fd, USBTMC_IOCTL_CTRL_REQUEST, bytearray(req), True)


def _ren_with_retry(fd: int, attempts: int = 3) -> int:
    status = 0
    for i in range(attempts):
        try:
            fcntl.ioctl(fd, USBTMC_IOCTL_CLEAR)
        except OSError:
            pass
        status = _ren_control(fd)
        if status == USBTMC_STATUS_SUCCESS:
            return status
        time.sleep(0.05)
    return status


def _function_from_reply(reply: str):
    m = _FUNC_RE.match(reply.strip())
    if not m:
        return None
    return FUNCTION_BY_CODE.get(int(m.group(1)))


def _range_from_reply(reply: str):
    m = _RANGE_RE.match(reply.strip())
    if not m:
        return None
    code = f"R{int(m.group(1))}"
    return "AUTO" if code == "R0" else code


def _rate_from_reply(reply: str):
    m = _RATE_RE.match(reply.strip())
    if not m:
        return None
    return f"PR{int(m.group(1))}"


def _trigger_from_reply(reply: str):
    m = _TRIG_RE.match(reply.strip())
    if not m:
        return None
    return f"TRS{int(m.group(1))}"


def _parse_knl(reply: str | None):
    if not reply:
        return None
    m = re.search(r"KNL\s*([+-]?\d+(?:\.\d*)?(?:[Ee][+-]?\d+)?)", reply, re.I)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 状态基类
# ---------------------------------------------------------------------------
class _StateMixin:
    def _init_state(self, device: str):
        self._lock = threading.Lock()
        self._state = {
            "device": device,
            "state": "offline",
            "message": "未连接",
            "idn": None,
            "serial": None,
            "function": None,
            "function_label": None,
            "range": None,
            "range_label": None,
            "rate": None,
            "rate_label": None,
            "trigger": None,
            "trigger_label": None,
            "null": None,
            "null_const": None,
        }
        self._on_sample = None
        self._on_status = None
        self._last_status_json = None

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def status_snapshot(self) -> dict:
        return self.snapshot()

    def _apply(self, **fields):
        with self._lock:
            self._state.update(fields)

    def _apply_instrument(self, **fields):
        with self._lock:
            if "function" in fields:
                name = fields["function"]
                self._state["function"] = name
                self._state["function_label"] = FUNCTIONS.get(name, (None, None, None, name))[3] if name else None
            if "range" in fields:
                code = fields["range"]
                self._state["range"] = code
                self._state["range_label"] = RANGES.get(code, (None, code))[1] if code else None
            if "rate" in fields:
                code = fields["rate"]
                self._state["rate"] = code
                self._state["rate_label"] = RATES.get(code, (None, code))[1] if code else None
            if "trigger" in fields:
                code = fields["trigger"]
                self._state["trigger"] = code
                self._state["trigger_label"] = TRIGGERS.get(code, (None, code))[1] if code else None

    def _set_state(self, state: str, message: str, **extra):
        with self._lock:
            changed = self._state["state"] != state or self._state["message"] != message
            self._state.update(state=state, message=message, **extra)
            snap = dict(self._state)
        if changed:
            self._publish_status(snap)

    def _publish_status(self, snap=None):
        if self._on_status is None:
            return
        snap = snap or self.snapshot()
        try:
            self._on_status(snap)
        except Exception:  # 回调不能影响采集
            LOG.exception("on_status 回调异常")

    def _emit_sample(self, sample: dict):
        if self._on_sample is not None:
            try:
                self._on_sample(sample)
            except Exception:
                LOG.exception("on_sample 回调异常")


# ---------------------------------------------------------------------------
# 真实设备
# ---------------------------------------------------------------------------
class DMM7461A(_StateMixin):
    def __init__(self, device: str = DEVICE_PATH, on_sample=None, on_status=None):
        self._init_state(device)
        self._on_sample = on_sample
        self._on_status = on_status
        self._fd = None
        self._thread = None
        self._stop = threading.Event()
        self._jobs: queue.Queue = queue.Queue(maxsize=32)
        self._consecutive_timeouts = 0
        self._poll_last = {"F": 0.0, "R": 0.0, "PR": 0.0, "TRS": 0.0}
        # 面板优先模式：保持 REN 撤销，让面板按键可用；命令发出后再自动退回本地
        self._local_mode = False
        self._last_local_sync = 0.0
        self._host_state: dict = {}
        # 手动完全释放：不发任何命令、不使能 REN、不轮询，只被动接收仪器输出的数据
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def preset_released(self, released: bool = True):
        """启动前设置（恢复上次的释放状态），避免启动时抢占面板。"""
        self._released = bool(released)

    # -- 生命周期 ----------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dmm-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        try:
            self._jobs.put_nowait(lambda: None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout)
        self._close_fd()

    def force_reconnect(self):
        """请求 worker 关闭并重新打开设备。"""
        if not self._thread or not self._thread.is_alive():
            raise DeviceOffline("设备线程未启动")
        self._submit(self._force_reconnect_locked, timeout=6.0, allow_local=True)

    def _auto_local_locked(self):
        """撤销 REN 并发送 GOTO_LOCAL，把仪器交回面板（不影响后续数据读取）。"""
        if self._fd is None:
            return
        try:
            _ren_control(self._fd, enable=False)
        except (OSError, ValueError):
            LOG.debug("撤销 REN 失败（可忽略）", exc_info=True)
        try:
            _goto_local_control(self._fd)
        except (OSError, ValueError):
            LOG.debug("GOTO_LOCAL 失败（可忽略）", exc_info=True)

    def _local_maybe_sync(self):
        """面板优先模式下低频率同步面板上的量程/速率/触发改动（10 秒一次）。"""
        now = time.monotonic()
        if now - self._last_local_sync < 10.0:
            return
        self._last_local_sync = now
        before = self.snapshot()
        fn = rg = rt = tr = nl = knl = None

        def q(cmd, pattern, parser):
            try:
                return parser(self._query_locked(cmd, pattern, deadline=0.8))
            except QueryTimeout:
                return None

        try:
            _ren_control(self._fd, enable=True)
            fn = q("F?", r"^F\d+", _function_from_reply)
            rg = q("R?", r"^R\d+", _range_from_reply)
            rt = q("PR?", r"^PR\d+", _rate_from_reply)
            tr = q("TRS?", r"^TRS\d+", _trigger_from_reply)
            nl = q("NL?", r"^NL\d", lambda r: r.strip().upper())
            knl = q("KNL?", r"^KNL", _parse_knl)
        finally:
            self._auto_local_locked()

        fields = {}
        if fn and fn != before["function"]:
            fields["function"] = fn
        if rg and rg != before["range"]:
            fields["range"] = rg
        if rt and rt != before["rate"]:
            fields["rate"] = rt
        if tr and tr != before["trigger"]:
            fields["trigger"] = tr
        if nl and nl != before["null"]:
            fields["null"] = nl
        if knl is not None and knl != before["null_const"]:
            fields["null_const"] = knl
        inst = {k: v for k, v in fields.items()
                if k in ("function", "range", "rate", "trigger")}
        extra = {k: v for k, v in fields.items() if k in ("null", "null_const")}
        if inst:
            self._apply_instrument(**inst)
        if extra:
            self._apply(**extra)
        if fields:
            self._publish_status()
            LOG.info("面板设置同步：%s", fields)

    def _enter_local_locked(self, message: str):
        """进入本地面板模式：撤销 REN、GOTO_LOCAL，停止发送命令（worker 继续读数据）。"""
        if self._fd is not None:
            try:
                _ren_control(self._fd, enable=False)
            except (OSError, ValueError):
                LOG.debug("撤销 REN 失败（可忽略）", exc_info=True)
            try:
                _goto_local_control(self._fd)
            except (OSError, ValueError):
                LOG.debug("GOTO_LOCAL 失败（可忽略）", exc_info=True)
        self._local_mode = True
        self._set_state("local", message)

    RELEASED_MESSAGE = "已释放给仪器面板（只接收数据，不发送命令）"

    def release_to_local(self):
        """完全释放控制：撤销 REN、发送 GOTO_LOCAL，此后不再发送任何命令或查询。

        面板优先模式仍会定期临时使能 REN 来同步状态、执行网页命令；完全释放后
        这些都停止，面板完全不受上位机影响。测量数据仍被动接收。
        """
        if not self._thread or not self._thread.is_alive():
            raise DeviceOffline("设备线程未启动")
        if self._released:
            return {"ok": True}
        self._released = True
        # 丢弃尚未执行的命令，避免释放后又被拉回远程
        while True:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break
        if self._fd is None:
            return {"ok": True}

        def job():
            self._enter_local_locked(self.RELEASED_MESSAGE)
            return {"ok": True}

        return self._submit(job, timeout=5.0, allow_local=True)

    def resume_remote(self):
        """恢复上位机控制：重新使能 REN、同步状态，然后回到面板优先模式。"""
        if not self._thread or not self._thread.is_alive():
            raise DeviceOffline("设备线程未启动")
        if not self._released:
            return self.snapshot()
        if self._fd is None:
            # 设备未连接：取消释放，worker 重新连接时按正常流程初始化
            self._released = False
            return self.snapshot()

        def job():
            status = _ren_with_retry(self._fd)
            if status != USBTMC_STATUS_SUCCESS:
                raise DeviceNotReady(f"REN_CONTROL 失败（状态 0x{status:02x}）")
            try:
                snap = self._sync_locked()
            except Exception:
                self._auto_local_locked()
                raise
            self._released = False
            self._remember_host_state(snap)
            now = time.monotonic()
            self._poll_last = {k: now for k in self._poll_last}
            self._local_mode = True
            self._auto_local_locked()
            self._last_local_sync = now
            self._set_state("online", "已恢复上位机控制（面板可用，自动释放远程）")
            return snap

        return self._submit(job, timeout=8.0, allow_local=True)

    # -- 公共命令接口 ------------------------------------------------------
    def sync_state(self):
        result = self._submit(self._sync_locked)
        self._remember_host_state(result)
        return result

    def _remember_host_state(self, snap: dict | None = None):
        snap = snap or self.snapshot()
        self._host_state = {k: snap.get(k) for k in ("function", "range", "rate", "trigger")}

    def send_command(self, text: str, host: bool = True):
        """发送一条设置/动作命令（无应答）。host=False 用于模拟面板外部改动。"""
        text = text.strip()
        if not text:
            raise ValueError("空命令")
        result = self._submit(lambda: self._write(text + "\n"))
        if host:
            self._update_host_state_from_command(text)
        return result

    def _update_host_state_from_command(self, text: str):
        t = text.strip().upper()
        if re.fullmatch(r"F\d+", t):
            fn = _function_from_reply(t)
            if fn:
                self._host_state["function"] = fn
        elif re.fullmatch(r"R\d+", t):
            rg = _range_from_reply(t)
            if rg:
                self._host_state["range"] = rg
        elif re.fullmatch(r"PR\d+", t):
            rt = _rate_from_reply(t)
            if rt:
                self._host_state["rate"] = rt
        elif re.fullmatch(r"TRS\d+", t):
            tr = _trigger_from_reply(t)
            if tr:
                self._host_state["trigger"] = tr

    def query_command(self, text: str, first_message: bool = False) -> str:
        """发送查询命令并返回原始应答。

        普通查询应答（ITP+/RE/FL 等）用正则跳过测量数据流；只有
        MAX?/MIN?/AVE?（应答形如 "DCVM +2.3E-02"）和 *STB?/*ESR?（纯数字）
        与测量数据格式无法区分，必须直接取查询后的第一条消息。
        """
        text = text.strip()
        if not text:
            raise ValueError("空查询")
        if first_message:
            return self._submit(lambda: self._query_first_locked(text))
        return self._submit(lambda: self._query_locked(text, r"."))

    def _query_first_locked(self, cmd: str, deadline: float = 1.2) -> str:
        self._write(cmd + "\n")
        time.sleep(QUERY_DELAY_S)
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            lines = self._read_messages()
            if not lines:
                continue
            return lines[0]
        raise QueryTimeout(f"查询超时: {cmd}")

    def set_function(self, name: str):
        name = name.upper()
        if name not in FUNCTIONS:
            raise ValueError(f"不支持的功能: {name}")
        result = self._submit(lambda: self._set_function_locked(name))
        self._host_state["function"] = result["function"]
        return result

    def set_range(self, code: str):
        code = code.upper()
        if code not in RANGES:
            raise ValueError(f"不支持的量程: {code}")
        result = self._submit(lambda: self._set_range_locked(code))
        self._host_state["range"] = result["range"]
        return result

    def set_rate(self, code: str):
        code = code.upper()
        if code not in RATES:
            raise ValueError(f"不支持的采样率: {code}")
        result = self._submit(lambda: self._set_rate_locked(code))
        self._host_state["rate"] = result["rate"]
        return result

    def set_trigger(self, code: str):
        code = code.upper()
        if code not in TRIGGERS:
            raise ValueError(f"不支持的触发源: {code}")
        result = self._submit(lambda: self._set_trigger_locked(code))
        self._host_state["trigger"] = result["trigger"]
        return result

    # -- 内部：fd / 通讯 ---------------------------------------------------
    def _submit(self, fn, timeout: float = 5.0, allow_local: bool = False):
        if not self._thread or not self._thread.is_alive():
            raise DeviceOffline("设备线程未启动")
        if self._released and not allow_local:
            raise LocalMode("已释放给仪器面板，请先点击「恢复控制」")
        if self._fd is None:
            raise DeviceOffline("设备未连接")
        evt = threading.Event()
        box: dict = {}

        def job():
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                evt.set()

        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            raise BusyError("命令队列已满，请稍后重试")
        if not evt.wait(timeout):
            raise QueryTimeout("设备响应超时")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _open(self):
        self._set_state("connecting", "正在连接设备…")
        fd = os.open(self._dev_path(), os.O_RDWR)
        try:
            fcntl.ioctl(fd, USBTMC_IOCTL_SET_TIMEOUT, struct.pack("I", READ_TIMEOUT_MS))
            fcntl.ioctl(fd, USBTMC_IOCTL_CLEAR)
        except OSError:
            LOG.debug("CLEAR/SET_TIMEOUT 失败（可忽略）", exc_info=True)
        status = _ren_with_retry(fd)
        if status != USBTMC_STATUS_SUCCESS:
            os.close(fd)
            raise DeviceNotReady(f"REN_CONTROL 失败（状态 0x{status:02x}）")
        self._fd = fd
        self._consecutive_timeouts = 0
        try:
            snap = self._sync_locked(sync_identity=True)
        except Exception:
            self._close_fd()
            raise
        self._remember_host_state(snap)
        now = time.monotonic()
        self._poll_last = {k: now for k in self._poll_last}
        # 面板优先：连接后立即撤销 REN，让面板按键可用；数据读取仍在继续
        self._local_mode = True
        self._auto_local_locked()
        self._last_local_sync = now
        self._set_state("online", "已连接（面板可用，自动释放远程）")

    def _open_passive(self):
        """释放状态下打开设备：只确保仪器处于本地，不同步状态、不发命令。"""
        self._set_state("connecting", "正在连接设备（保持面板控制）…")
        fd = os.open(self._dev_path(), os.O_RDWR)
        try:
            fcntl.ioctl(fd, USBTMC_IOCTL_SET_TIMEOUT, struct.pack("I", READ_TIMEOUT_MS))
        except OSError:
            LOG.debug("SET_TIMEOUT 失败（可忽略）", exc_info=True)
        self._fd = fd
        self._consecutive_timeouts = 0
        self._enter_local_locked(self.RELEASED_MESSAGE)

    def _dev_path(self) -> str:
        return self.snapshot()["device"]

    def _close_fd(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _force_reconnect_locked(self):
        self._close_fd()
        self._set_state("connecting", "正在重新连接设备…")
        return {"ok": True}

    def _write(self, text: str):
        if self._fd is None:
            raise DeviceOffline("设备未连接")
        payload = text.encode("ascii", "replace")
        try:
            total = 0
            while total < len(payload):
                total += os.write(self._fd, payload[total:])
        except OSError as exc:
            raise _Disconnected(str(exc)) from exc

    def _read_messages(self):
        """读取一条 USBTMC 消息并拆分为行；超时返回 None。"""
        if self._fd is None:
            raise _Disconnected("设备未连接")
        try:
            data = os.read(self._fd, 4096)
        except OSError as exc:
            if exc.errno in (errno.ETIMEDOUT, errno.EAGAIN):
                return None
            raise _Disconnected(str(exc)) from exc
        text = data.decode("ascii", "replace")
        return [ln.strip("\r\n\x00 \t") for ln in re.split(r"[\r\n]+", text) if ln.strip()]

    def _read_once(self):
        lines = self._read_messages()
        if not lines:
            return
        for line in lines:
            kind, payload = parse_message(line)
            if kind == "sample":
                self._publish_sample(payload)
            else:
                LOG.debug("未请求的应答: %r", line)

    # -- 面板按键外部改动同步 ----------------------------------------------
    def _maybe_poll(self):
        """在两次读取之间轮询仪器状态，同步面板按键造成的改动。"""
        if self._fd is None:
            return
        now = time.monotonic()
        # 功能较快：约 1 秒一次（用户最关心）
        if now - self._poll_last["F"] >= 1.0:
            self._poll_last["F"] = now
            self._poll_field("F?", "F")
            return
        # 触发/量程/速率：约 1.5 秒各一次（触发优先，手动触发时无数据流也能同步）
        for key, cmd in (("TRS", "TRS?"), ("R", "R?"), ("PR", "PR?")):
            if now - self._poll_last[key] >= 1.5:
                self._poll_last[key] = now
                self._poll_field(cmd, key)
                return

    _POLL_FIELDS = {
        "F": ("function", r"^F\d+", _function_from_reply),
        "R": ("range", r"^R\d+", _range_from_reply),
        "PR": ("rate", r"^PR\d+", _rate_from_reply),
        "TRS": ("trigger", r"^TRS\d+", _trigger_from_reply),
    }

    def _poll_field(self, cmd: str, key: str):
        try:
            name, pattern, parser = self._POLL_FIELDS[key]
            before = self.snapshot()
            new_value = parser(self._query_locked(cmd, pattern, deadline=0.8))
            if not new_value or new_value == before[name]:
                return
            # 面板/外部改动：直接同步到上位机，不释放控制
            self._apply_instrument(**{name: new_value})
            self._publish_status()
            LOG.info("状态同步 %s: %s -> %s（面板/外部改动）", name, before[name], new_value)
        except QueryTimeout:
            LOG.debug("状态轮询超时: %s", cmd)

    def _publish_sample(self, payload: dict):
        tag = payload.get("tag")
        sample = {
            "t": time.time(),
            "mono": time.monotonic(),
            "v": None if payload["ol"] else payload["value"],
            "ol": payload["ol"],
            "tag": tag,
            "raw": payload["raw"],
        }
        # 本地面板模式下没有命令/轮询，只能靠数据表头同步功能与单位
        if self._local_mode and tag and tag in FUNCTION_BY_TAG:
            fn = FUNCTION_BY_TAG[tag]
            with self._lock:
                changed = self._state["function"] != fn
                if changed:
                    self._state["function"] = fn
                    self._state["function_label"] = FUNCTIONS[fn][3]
            if changed:
                self._publish_status()
        self._emit_sample(sample)

    def _query_locked(self, cmd: str, pattern: str, deadline: float = QUERY_TIMEOUT_S,
                      publish: bool = True) -> str:
        self._write(cmd + "\n")
        time.sleep(QUERY_DELAY_S)
        end = time.monotonic() + deadline
        rx = re.compile(pattern)
        while time.monotonic() < end:
            lines = self._read_messages()
            if lines is None:
                continue
            for line in lines:
                kind, payload = parse_message(line)
                if kind == "sample":
                    if publish:
                        self._publish_sample(payload)
                    continue
                if rx.search(line):
                    return line
                LOG.warning("等待 %s 时收到意外应答: %r", cmd, line)
        raise QueryTimeout(f"查询超时: {cmd}")

    # -- 内部：worker ------------------------------------------------------
    def _run(self):
        backoff = [0.5, 1.0, 2.0, 5.0, 10.0]
        idx = 0
        while not self._stop.is_set():
            if self._released:
                # 完全释放：只读数据；仅执行允许的作业（恢复控制/重连）
                if self._fd is None:
                    try:
                        self._open_passive()
                        idx = 0
                    except Exception as exc:  # noqa: BLE001
                        self._set_state("offline", f"设备未连接：{exc}")
                        if self._stop.wait(backoff[min(idx, len(backoff) - 1)]):
                            break
                        idx += 1
                    continue
                try:
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        job = None
                    if job is not None:
                        job()
                    else:
                        self._read_once()
                except _Disconnected as exc:
                    LOG.warning("释放状态 USB 断开: %s", exc)
                    self._close_fd()
                    self._set_state("offline", f"设备断开：{exc}")
                except Exception:  # noqa: BLE001
                    LOG.exception("释放状态循环异常")
                    time.sleep(0.05)
                continue
            if self._local_mode:
                # 面板优先模式：读数据；有命令时执行，执行完自动退回本地
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    job = None
                try:
                    if job is not None:
                        try:
                            # 面板模式下撤销了 REN，执行上位机命令前需临时重新使能
                            _ren_control(self._fd, enable=True)
                            job()
                        finally:
                            self._auto_local_locked()
                    else:
                        self._local_maybe_sync()
                        self._read_once()
                except _Disconnected as exc:
                    LOG.warning("面板模式 USB 断开: %s", exc)
                    self._close_fd()
                    self._local_mode = False
                    self._set_state("offline", f"设备断开：{exc}")
                except Exception:  # noqa: BLE001
                    LOG.exception("面板模式循环异常")
                continue
            if self._fd is None:
                try:
                    self._open()
                    idx = 0
                except Exception as exc:  # noqa: BLE001
                    self._set_state("offline", f"设备未连接：{exc}")
                    if self._stop.wait(backoff[min(idx, len(backoff) - 1)]):
                        break
                    idx += 1
                    continue
            try:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    job = None
                if job is not None:
                    job()
                else:
                    self._maybe_poll()
                    self._read_once()
            except _Disconnected as exc:
                LOG.warning("USB 通讯中断: %s", exc)
                self._close_fd()
                self._set_state("offline", f"设备断开：{exc}")
            except Exception:  # noqa: BLE001
                LOG.exception("worker 循环异常")
                time.sleep(0.05)
        self._close_fd()

    # -- 内部：同步与设置 --------------------------------------------------
    def _sync_locked(self, sync_identity: bool = False):
        if self._fd is None:
            raise DeviceOffline("设备未连接")
        if sync_identity:
            self._write("H1\n")
            time.sleep(0.05)
            idn = self._query_locked("*IDN?", r"ADC|7461", deadline=2.0, publish=False)
            serial = None
            parts = [p.strip() for p in idn.split(",")]
            if len(parts) >= 3:
                serial = parts[2]
            self._apply(idn=idn, serial=serial)
        fn_reply = self._query_locked("F?", r"^F\d+")
        rg_reply = self._query_locked("R?", r"^R\d+")
        rt_reply = self._query_locked("PR?", r"^PR\d+")
        tr_reply = self._query_locked("TRS?", r"^TRS\d+")
        try:
            nl_reply = self._query_locked("NL?", r"^NL\d")
        except QueryTimeout:
            nl_reply = None
        try:
            knl_reply = self._query_locked("KNL?", r"^KNL")
        except QueryTimeout:
            knl_reply = None
        if nl_reply:
            self._apply(null=nl_reply.strip().upper())
        if knl_reply:
            self._apply(null_const=_parse_knl(knl_reply))
        self._apply_instrument(
            function=_function_from_reply(fn_reply) or self.snapshot()["function"],
            range=_range_from_reply(rg_reply),
            rate=_rate_from_reply(rt_reply),
            trigger=_trigger_from_reply(tr_reply),
        )
        self._publish_status()
        return self.snapshot()

    def _set_function_locked(self, name: str):
        cmd = FUNCTIONS[name][0]
        for _ in range(2):
            self._write(cmd + "\n")
            time.sleep(0.05)
            actual = _function_from_reply(self._query_locked("F?", r"^F\d+"))
            if actual == name:
                self._apply_instrument(function=actual)
                self._publish_status()
                return self.snapshot()
        self._apply_instrument(function=actual)
        self._publish_status()
        return self.snapshot()

    def _set_range_locked(self, code: str):
        cmd = RANGES[code][0]
        for _ in range(2):
            self._write(cmd + "\n")
            time.sleep(0.05)
            actual = _range_from_reply(self._query_locked("R?", r"^R\d+"))
            if actual == code:
                self._apply_instrument(range=actual)
                self._publish_status()
                return self.snapshot()
        self._apply_instrument(range=actual)
        self._publish_status()
        return self.snapshot()

    def _set_rate_locked(self, code: str):
        cmd = RATES[code][0]
        for _ in range(2):
            self._write(cmd + "\n")
            time.sleep(0.05)
            actual = _rate_from_reply(self._query_locked("PR?", r"^PR\d+"))
            if actual == code:
                self._apply_instrument(rate=actual)
                self._publish_status()
                return self.snapshot()
        self._apply_instrument(rate=actual)
        self._publish_status()
        return self.snapshot()

    def _set_trigger_locked(self, code: str):
        cmd = TRIGGERS[code][0]
        self._write(cmd + "\n")
        time.sleep(0.05)
        try:
            actual = _trigger_from_reply(self._query_locked("TRS?", r"^TRS\d+"))
        except QueryTimeout:
            if code != "TRS0":
                # 手动/外部/总线/电平/差值触发等待时，仪器固件不再应答查询；
                # 但命令本身已执行，按设定值更新状态，避免误报失败。
                self._apply_instrument(trigger=code)
                self._publish_status()
                return self.snapshot()
            raise
        if actual != code:
            self._write(cmd + "\n")
            time.sleep(0.05)
            actual = _trigger_from_reply(self._query_locked("TRS?", r"^TRS\d+"))
        self._apply_instrument(trigger=actual)
        self._publish_status()
        return self.snapshot()


# ---------------------------------------------------------------------------
# 模拟器（无硬件开发/演示）
# ---------------------------------------------------------------------------
class SimulatedDMM(_StateMixin):
    def __init__(self, on_sample=None, on_status=None):
        self._init_state("simulator")
        self._on_sample = on_sample
        self._on_status = on_status
        self._thread = None
        self._stop = threading.Event()
        self._disconnected = False
        self._local_mode = False
        self._ol = False
        self._adv: dict = {}
        self._apply_instrument(function="DCV", range="AUTO", rate="PR3", trigger="TRS0")
        self._apply(null="NL0", null_const=0.0,
                    idn="ADC Corp.,7461A,SIM0000000000,D00", serial="SIM0000000000")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if self._local_mode:
            self._set_state("local", "已释放给仪器面板（模拟器，只接收数据）")
        else:
            self._set_state("online", "已连接（模拟器）")
        self._thread = threading.Thread(target=self._run, name="dmm-sim", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        t0 = time.monotonic()
        while True:
            rate = self.snapshot().get("rate") or "PR3"
            interval = {"PR1": 0.005, "PR2": 0.01, "PR3": 0.05,
                        "PR4": 0.2, "PR5": 0.5}.get(rate, 0.05)
            if self._stop.wait(interval):
                break
            if self._disconnected:
                continue
            t = time.monotonic() - t0
            fn = self.snapshot()["function"] or "DCV"
            # 通断档：模拟表笔接触/断开交替（约 3 s 切换），便于演示通断判定
            cont_open = fn == "CONT" and int(t / 3) % 2 == 1
            if fn == "CONT" and not self._ol and not cont_open:
                value = 2.35 + (__import__("random").random() - 0.5) * 0.02
                self._emit_sample({"t": time.time(), "mono": time.monotonic(), "v": value,
                                   "ol": False, "tag": "RCT_", "raw": f"{value:+.7E}"})
                continue
            if self._ol or cont_open:
                value = 9.999999e37
                sample = {"t": time.time(), "mono": time.monotonic(), "v": None, "ol": True,
                          "tag": {"DCV": "DCV_"}.get(fn), "raw": "+9.999999E+37"}
            else:
                base = 0.0187214 + 0.006 * __import__("math").sin(2 * 3.141592653589793 * 0.12 * t)
                noise = (__import__("random").random() - 0.5) * 0.0004
                value = base + noise
                sample = {"t": time.time(), "mono": time.monotonic(), "v": value, "ol": False,
                          "tag": {"DCV": "DCV_"}.get(fn), "raw": f"{value:+.7E}"}
            self._emit_sample(sample)

    def _ensure_remote(self):
        if self._local_mode:
            raise LocalMode("本地面板控制中，请先点击「恢复控制」")

    @property
    def released(self) -> bool:
        return self._local_mode

    def preset_released(self, released: bool = True):
        self._local_mode = bool(released)

    def release_to_local(self):
        if self._local_mode:
            return {"ok": True}
        self._local_mode = True
        self._set_state("local", "已释放给仪器面板（模拟器，只接收数据）")
        return {"ok": True}

    def resume_remote(self):
        if not self._local_mode:
            return self.snapshot()
        self._local_mode = False
        self._set_state("online", "已连接（模拟器）")
        return self.snapshot()

    def sync_state(self):
        return self.snapshot()

    _ADV_DEFAULTS = {
        "RE?": "RE6", "FL?": "FL2", "AZ?": "AZ1", "IMP?": "IMP0", "KOM?": "KOM10",
        "ITP?": "ITP+1.000000E+00", "ITS?": "ITS+1.000000E-02",
        "NL?": "NL0", "KNL?": "KNL+0.000000E+00", "SM?": "SM0", "TI?": "TI10",
        "SC?": "SC0", "KA?": "KA+1.000000E+00", "KB?": "KB+0.000000E+00",
        "KC?": "KC+1.000000E+00", "DB?": "DB0", "KD?": "KD+1.000000E+00",
        "MN?": "MN0", "MAX?": "+0.000000E+00", "MIN?": "+0.000000E+00",
        "AVE?": "+0.000000E+00", "AVN?": "AVN0",
        "HI?": "HI+0.000000E+00", "LO?": "LO+0.000000E+00",
        "LOP?": "LOP0", "MIP?": "MIP0", "HIP?": "HIP0",
        "TRS?": "TRS0", "TRT?": "TRT+1.000000E-03", "TRD?": "TRD+0.0000E+00",
        "TRN?": "TRN1", "SPN?": "SPN1", "PTL?": "PTL0", "TRCM?": "TRCM0",
        "CW?": "CW0", "TLV?": "TLV0", "TLS?": "TLS0", "TDL?": "TDL1",
        "TDS?": "TDS0", "TDC?": "TDC0", "ST?": "ST0", "IRPO?": "IRPO0001",
        "IRNO?": "IRNO0000, 0000", "SCNT?": "SCNT+0.000000E+00",
        "SMAX?": "+0.000000E+00", "SMIN?": "+0.000000E+00",
        "*STB?": "0", "*ESR?": "0",
    }

    def send_command(self, text: str, host: bool = True):
        self._ensure_remote()
        text = text.strip()
        if not text:
            raise ValueError("空命令")
        m = re.match(r"^([A-Z]+)(.*)$", text)
        if m:
            prefix, value = m.groups()
            if value:
                self._adv[prefix + "?"] = text
            elif text == "ICL":
                self._adv["IRPO?"] = "IRPO0000"
        if text.startswith("TRS"):
            self._apply_instrument(trigger=text)
            self._publish_status()
        elif text.startswith("NL"):
            self._apply(null=text.upper())
            self._publish_status()
        elif text.startswith("KNL"):
            value = _parse_knl(text)
            if value is not None:
                self._apply(null_const=value)
                self._publish_status()
        return {"ok": True}

    def query_command(self, text: str, first_message: bool = False) -> str:
        self._ensure_remote()
        q = text.strip().upper()
        return self._adv.get(q, self._ADV_DEFAULTS.get(q, "OK"))

    def set_function(self, name: str):
        self._ensure_remote()
        name = name.upper()
        if name not in FUNCTIONS:
            raise ValueError(f"不支持的功能: {name}")
        self._apply_instrument(function=name)
        self._publish_status()
        return self.snapshot()

    def set_range(self, code: str):
        self._ensure_remote()
        code = code.upper()
        if code not in RANGES:
            raise ValueError(f"不支持的量程: {code}")
        self._apply_instrument(range=code)
        self._publish_status()
        return self.snapshot()

    def set_rate(self, code: str):
        self._ensure_remote()
        code = code.upper()
        if code not in RATES:
            raise ValueError(f"不支持的采样率: {code}")
        self._apply_instrument(rate=code)
        self._publish_status()
        return self.snapshot()

    def set_trigger(self, code: str):
        self._ensure_remote()
        code = code.upper()
        if code not in TRIGGERS:
            raise ValueError(f"不支持的触发源: {code}")
        self._apply_instrument(trigger=code)
        self._publish_status()
        return self.snapshot()

    def force_reconnect(self):
        self._disconnected = False
        if self._local_mode:
            self._set_state("local", "已释放给仪器面板（模拟器，只接收数据）")
        else:
            self._set_state("online", "已连接（模拟器）")
        return {"ok": True}

    # 模拟故障注入
    def inject_ol(self, enabled: bool):
        self._ol = bool(enabled)
        return {"ol": self._ol}

    def inject_disconnect(self, enabled: bool):
        self._disconnected = bool(enabled)
        if self._disconnected:
            self._set_state("offline", "模拟断开")
        else:
            self._set_state("online", "已连接（模拟器）")
        return {"disconnected": self._disconnected}


def create_dmm(sim: bool = False, on_sample=None, on_status=None):
    if sim:
        return SimulatedDMM(on_sample=on_sample, on_status=on_status)
    return DMM7461A(DEVICE_PATH, on_sample=on_sample, on_status=on_status)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _selftest():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    samples = []

    def on_sample(s):
        samples.append(s)

    def on_status(st):
        print(f"[状态] {st['state']}: {st['message']}"
              + (f" | {st['idn']}" if st.get("idn") else ""))

    dmm = DMM7461A(DEVICE_PATH, on_sample=on_sample, on_status=on_status)
    dmm.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and dmm.snapshot()["state"] != "online":
            time.sleep(0.1)
        st = dmm.snapshot()
        if st["state"] != "online":
            print("设备未连接：", st["message"])
            return 1
        print("IDN   :", st["idn"])
        print("功能  :", st["function"], st["function_label"])
        print("量程  :", st["range"], st["range_label"])
        print("速率  :", st["rate"], st["rate_label"])
        print("触发  :", st["trigger"], st["trigger_label"])
        t0 = time.time()
        while time.time() - t0 < 1.5 and len(samples) < 10:
            time.sleep(0.05)
        print(f"1.5 秒内收到 {len(samples)} 个样本：")
        for s in samples[:5]:
            print(" ", s["raw"])
        print("同步查询:", dmm.sync_state())
        return 0
    finally:
        dmm.stop()


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else _selftest())
