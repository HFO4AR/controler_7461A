import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as appmod


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 测试使用临时数据目录，避免把测试设置/日志写进真实 data/
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        appmod.DATA_DIR = base
        appmod.LOG_DIR = base / "logs"
        appmod.SETTINGS_FILE = base / "settings.json"
        appmod.LOG_DIR.mkdir(parents=True, exist_ok=True)

        cls.flask_app = appmod.build(sim=True)
        cls.client = cls.flask_app.test_client()
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls):
        appmod.shutdown()
        cls._tmp.cleanup()

    def json(self, resp):
        self.assertIsNotNone(resp)
        return resp.get_json()

    def test_meta(self):
        data = self.json(self.client.get("/api/meta"))
        codes = {f["code"] for f in data["functions"]}
        self.assertIn("DCV", codes)
        self.assertIn("ACV", codes)
        self.assertIn("FREQ", codes)

    def test_state_online(self):
        data = self.json(self.client.get("/api/state"))
        self.assertEqual(data["device"]["state"], "online")
        self.assertIn("config", data)
        self.assertIn("stats", data)

    def test_control_function_and_rate(self):
        r = self.client.post("/api/control", json={"action": "function", "value": "RES2W"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/control", json={"action": "rate", "value": "PR5"})
        self.assertEqual(r.status_code, 200)
        inst = self.json(self.client.get("/api/state"))["instrument"]
        self.assertEqual(inst["function"], "RES2W")
        self.assertEqual(inst["rate"], "PR5")

    def test_config_and_shunt_validation(self):
        r = self.client.post("/api/config", json={"display_mode": "current", "shunt_ohm": 0.1})
        self.assertEqual(r.status_code, 200)
        st = self.json(self.client.get("/api/state"))
        self.assertEqual(st["config"]["display_mode"], "current")
        self.assertEqual(st["instrument"]["function"], "DCV")

        r = self.client.post("/api/config", json={"display_mode": "current", "shunt_ohm": -1})
        self.assertEqual(r.status_code, 400)

        r = self.client.post("/api/control", json={"action": "function", "value": "ACV"})
        self.assertEqual(r.status_code, 409)

        r = self.client.post("/api/config", json={"display_mode": "voltage", "shunt_ohm": 0.1})
        self.assertEqual(r.status_code, 200)

    def test_limits_validation(self):
        r = self.client.post("/api/limits", json={
            "mode": "voltage", "enabled": True, "low": 1.0, "high": 0.5})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/limits", json={
            "mode": "voltage", "enabled": True, "low": 0.0, "high": 1.0})
        self.assertEqual(r.status_code, 200)
        lim = self.json(r)["limits"]["voltage"]
        self.assertTrue(lim["enabled"])

    def test_arbitrary_integration_time(self):
        r = self.client.post("/api/advanced/set", json={"key": "itp", "value": 2.5})
        self.assertEqual(r.status_code, 200)
        self.assertIn("ITP", self.json(r)["value"])
        r = self.client.post("/api/advanced/set", json={"key": "its", "value": 0.01})
        self.assertEqual(r.status_code, 200)
        d = self.json(self.client.get("/api/advanced/read?keys=itp,its"))
        self.assertTrue(d["values"]["itp"]["ok"])
        self.assertTrue(d["values"]["its"]["ok"])

    def test_extended_stats_fields(self):
        time.sleep(0.4)
        st = self.json(self.client.get("/api/state"))["stats"]
        for key in ("rms", "median", "mad", "mean_abs_dev", "std_pop",
                    "var_sample", "slope", "crest", "sum", "first", "last",
                    "min_t", "max_t", "ol_ratio", "duration"):
            self.assertIn(key, st)
        self.assertGreaterEqual(st["n"], 1)
        self.assertIsNotNone(st["median"])
        self.assertIsNotNone(st["rms"])
        self.assertIsNotNone(st["first"])
        self.assertIsNotNone(st["last"])

    def test_history(self):
        time.sleep(0.3)
        d = self.json(self.client.get("/api/history?seconds=60"))
        self.assertGreater(len(d["samples"]), 0)
        self.assertTrue(all(s["ctx"] == d["context_id"] for s in d["samples"]))

    def test_logging_and_download(self):
        self.assertEqual(self.client.post("/api/log/start", json={}).status_code, 200)
        time.sleep(0.3)
        stop = self.json(self.client.post("/api/log/stop", json={}))
        name = stop["logging"]["file"]
        self.assertRegex(name, r"^7461A_\d{8}_\d{6}\.csv$")

        files = self.json(self.client.get("/api/logs"))["files"]
        self.assertTrue(any(f["name"] == name for f in files))

        r = self.client.get(f"/api/logs/{name}/download")
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertIn("iso_time,epoch_s,elapsed_s", text)
        self.assertIn("function_unit", text)
        self.assertIn("reading_value", text)
        self.assertIn("current_a", text)

        r = self.client.get("/api/logs/evil.csv/download")
        self.assertEqual(r.status_code, 404)

    def test_stats_reset(self):
        r = self.client.post("/api/stats/reset", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["stats"]["n_total"], 0)

    def test_sim_fault_injection(self):
        r = self.client.post("/api/dev/sim", json={"ol": True})
        self.assertEqual(r.status_code, 200)
        time.sleep(0.15)
        r = self.client.post("/api/dev/sim", json={"ol": False})
        self.assertEqual(r.status_code, 200)

    def test_advanced_schema(self):
        d = self.json(self.client.get("/api/advanced"))
        keys = {i["key"] for g in d["groups"] for i in g["items"]}
        self.assertIn("itp", keys)
        self.assertIn("digits", keys)
        self.assertIn("null_const", keys)
        self.assertIn("hi", keys)
        self.assertIn("trs", keys)

    def test_advanced_set_and_read(self):
        r = self.client.post("/api/advanced/set", json={"key": "digits", "value": "RE5"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.json(r)["value"], "RE5")
        r = self.client.post("/api/advanced/set", json={"key": "smooth_count", "value": 20})
        self.assertEqual(r.status_code, 200)

        d = self.json(self.client.get("/api/advanced/read?keys=digits,smooth_count,azero"))
        self.assertTrue(d["values"]["digits"]["ok"])
        self.assertEqual(d["values"]["digits"]["value"], "RE5")
        self.assertEqual(d["values"]["smooth_count"]["value"], "TI20")
        self.assertTrue(d["values"]["azero"]["ok"])

        r = self.client.post("/api/advanced/set", json={"key": "digits", "value": "BAD"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/advanced/set", json={"key": "smooth_count", "value": 1})
        self.assertEqual(r.status_code, 400)

    def test_advanced_action_null_from_latest(self):
        time.sleep(0.2)
        r = self.client.post("/api/advanced/action", json={"key": "null_const"})
        self.assertEqual(r.status_code, 200)
        body = self.json(r)
        self.assertTrue(body["sent"].startswith("KNL"))
        self.assertTrue(body["value"].startswith("KNL"))

    def test_stats_rate_hz(self):
        time.sleep(0.3)
        st = self.json(self.client.get("/api/state"))["stats"]
        self.assertGreater(st.get("rate_hz", 0), 0)

    def test_units_follow_function_and_shunt(self):
        # 先恢复到中速，避免前面测试留下的慢速档让样本更新过慢
        self.client.post("/api/control", json={"action": "rate", "value": "PR3"})
        time.sleep(0.2)
        # 电阻：单位 Ω，且不计算分流电流
        self.assertEqual(self.client.post(
            "/api/control", json={"action": "function", "value": "RES2W"}).status_code, 200)
        time.sleep(0.25)
        st = self.json(self.client.get("/api/state"))
        self.assertEqual(st["stats"]["unit"], "Ω")
        samples = self.json(self.client.get("/api/history?seconds=5"))["samples"]
        self.assertTrue(samples)
        self.assertEqual(samples[-1]["u"], "Ω")
        self.assertIsNone(samples[-1]["i"])

        # 直接电流：单位 A
        self.assertEqual(self.client.post(
            "/api/control", json={"action": "function", "value": "DCI"}).status_code, 200)
        time.sleep(0.25)
        st = self.json(self.client.get("/api/state"))
        self.assertEqual(st["stats"]["unit"], "A")

        # 分流电流：DCV + R，显示 A，I = V / R
        self.assertEqual(self.client.post(
            "/api/config", json={"display_mode": "current", "shunt_ohm": 0.1}).status_code, 200)
        time.sleep(0.25)
        samples = self.json(self.client.get("/api/history?seconds=5"))["samples"]
        self.assertTrue(samples)
        s = samples[-1]
        self.assertEqual(s["u"], "A")
        self.assertAlmostEqual(s["d"], s["v"] / 0.1, places=9)

        self.client.post("/api/control", json={"action": "function", "value": "DCV"})
        self.client.post("/api/config", json={"display_mode": "voltage", "shunt_ohm": 0.1})


if __name__ == "__main__":
    unittest.main()
