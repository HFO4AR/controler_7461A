import ctypes
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dmm7461a as dmm


class ParseTests(unittest.TestCase):
    def test_h1_sample(self):
        kind, payload = dmm.parse_message("DCV_ +018.7214E-03")
        self.assertEqual(kind, "sample")
        self.assertEqual(payload["tag"], "DCV_")
        self.assertAlmostEqual(payload["value"], 0.0187214)
        self.assertFalse(payload["ol"])

    def test_bare_sample_negative(self):
        kind, payload = dmm.parse_message("-001.2345E+00")
        self.assertEqual(kind, "sample")
        self.assertAlmostEqual(payload["value"], -1.2345)
        self.assertIsNone(payload["tag"])

    def test_ol(self):
        for raw in ("+9.999999E+37", "-9.999999E+37"):
            kind, payload = dmm.parse_message(raw)
            self.assertEqual(kind, "sample")
            self.assertTrue(payload["ol"])

    def test_reply_not_sample(self):
        for raw in ("F01", "R0", "PR3", "TRS0", "ADC Corp.,7461A,1,D00"):
            kind, _ = dmm.parse_message(raw)
            self.assertEqual(kind, "reply", raw)

    def test_reply_maps(self):
        self.assertEqual(dmm._function_from_reply("F01"), "DCV")
        self.assertEqual(dmm._function_from_reply("F50"), "FREQ")
        self.assertEqual(dmm._range_from_reply("R0"), "AUTO")
        self.assertEqual(dmm._range_from_reply("R5"), "R5")
        self.assertEqual(dmm._rate_from_reply("PR3"), "PR3")
        self.assertEqual(dmm._trigger_from_reply("TRS0"), "TRS0")


class PollTests(unittest.TestCase):
    def _dmm(self, replies):
        events = []
        d = dmm.DMM7461A(on_status=events.append)
        d._fd = 1
        d._apply_instrument(function="DCV", range="AUTO", rate="PR3", trigger="TRS0")
        d._query_locked = lambda cmd, pattern, deadline=0.8: replies[cmd]
        return d, events

    def test_external_function_change(self):
        d, events = self._dmm({"F?": "F02"})
        d._poll_field("F?", "F")
        self.assertEqual(d.snapshot()["function"], "ACV")
        self.assertTrue(events)

    def test_external_range_rate_trigger_change(self):
        d, _ = self._dmm({"R?": "R5", "PR?": "PR1", "TRS?": "TRS1"})
        d._poll_field("R?", "R")
        d._poll_field("PR?", "PR")
        d._poll_field("TRS?", "TRS")
        snap = d.snapshot()
        self.assertEqual(snap["range"], "R5")
        self.assertEqual(snap["rate"], "PR1")
        self.assertEqual(snap["trigger"], "TRS1")


class IoctlTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(dmm.USBTMC_IOCTL_CLEAR, 0x5B02)
        self.assertEqual(dmm.USBTMC_IOCTL_SET_TIMEOUT, 0x40045B0A)
        self.assertEqual(dmm.USBTMC_IOCTL_CTRL_REQUEST, 0xC0105B08)

    def test_control_request_struct_size(self):
        req = struct.pack("<BBHHH", 0xA1, 0xA0, 1, 0, 1) + struct.pack("<Q", 0)
        self.assertEqual(len(req), 16)


if __name__ == "__main__":
    unittest.main()
