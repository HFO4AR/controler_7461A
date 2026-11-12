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

    def test_null_control_state(self):
        st = self.json(self.client.get("/api/state"))
        self.assertIn("null", st)
        r = self.client.post("/api/advanced/action", json={"key": "null_const"})
        self.assertEqual(r.status_code, 200)
        time.sleep(0.3)
        st = self.json(self.client.get("/api/state"))
        self.assertTrue(st["null"]["enabled"])
        self.assertIsNotNone(st["null"]["const"])

        r = self.client.post("/api/advanced/set",
                             json={"key": "null", "value": "NL0"})
        self.assertEqual(r.status_code, 200)
        time.sleep(0.2)
        st = self.json(self.client.get("/api/state"))
        self.assertFalse(st["null"]["enabled"])

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
        # 前面的测试可能刚重置统计、且模拟器处于慢速档，轮询等待最多 2 s
        deadline = time.time() + 2.0
        st = {}
        while time.time() < deadline:
            st = self.json(self.client.get("/api/state"))["stats"]
            if st.get("rate_hz", 0) > 0:
                break
            time.sleep(0.1)
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

    # -- Agent 接口 ----------------------------------------------------------
    def test_agent_manifest_and_status(self):
        data = self.json(self.client.get("/api/agent"))
        self.assertIn("endpoints", data)
        self.assertIn("DCV", {f["code"] for f in data["options"]["functions"]})
        st = self.json(self.client.get("/api/agent/status"))
        self.assertTrue(st["ok"])
        self.assertEqual(st["device"]["state"], "online")
        self.assertIn("latest", st)

    def test_agent_reading(self):
        r = self.json(self.client.get("/api/agent/reading?fresh=1&timeout=3"))
        self.assertTrue(r["ok"])
        self.assertIn("value", r["reading"])
        self.assertLess(r["reading"]["age_s"], 3)
        bad = self.client.get("/api/agent/reading?timeout=abc")
        self.assertEqual(bad.status_code, 400)

    def test_agent_measure(self):
        r = self.client.post("/api/agent/measure", json={"count": 5, "include_samples": True})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["complete"])
        self.assertEqual(len(d["samples"]), 5)
        self.assertEqual(d["summary"]["n"] + d["summary"]["n_ol"], 5)
        if d["summary"]["n"]:
            self.assertLessEqual(d["summary"]["min"], d["summary"]["mean"])
        r = self.client.post("/api/agent/measure", json={"seconds": 0.3})
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.get_json()["summary"]["n"], 1)
        for body in ({"count": 0}, {"count": 1.5}, {"seconds": -1}, {"settle": "x"}):
            self.assertEqual(self.client.post("/api/agent/measure", json=body).status_code, 400)

    def test_agent_setup(self):
        r = self.client.post("/api/agent/setup",
                             json={"function": "RES2W", "rate": "PR3", "settle": 0.2})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["instrument"]["function"], "RES2W")
        self.assertEqual(d["instrument"]["rate"], "PR3")
        self.assertEqual(set(d["applied"]), {"function", "rate"})
        self.assertIsNotNone(d["reading"])
        # 分流模式与非 DCV 冲突
        r = self.client.post("/api/agent/setup", json={"display_mode": "current", "function": "ACV"})
        self.assertEqual(r.status_code, 409)
        r = self.client.post("/api/agent/setup", json={"display_mode": "current", "shunt_ohm": 0.5})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["instrument"]["function"], "DCV")
        self.assertEqual(d["config"], {"display_mode": "current", "shunt_ohm": 0.5})
        # 参数校验
        self.assertEqual(self.client.post("/api/agent/setup", json={"foo": 1}).status_code, 400)
        r = self.client.post("/api/agent/setup",
                             json={"display_mode": "voltage", "shunt_ohm": 0.1, "function": "DCV"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post("/api/agent/setup", json={"function": "XYZ"}).status_code, 400)

    def test_mcp_server_tools(self):
        import json as _json
        import mcp_server

        def fake_http(method, path, body=None, query=None, timeout=30.0, raw=False):
            if query:
                from urllib.parse import urlencode
                path += "?" + urlencode({k: v for k, v in query.items() if v is not None})
            resp = (self.client.get(path) if method == "GET"
                    else self.client.post(path, json=body))
            if raw:
                return resp.get_data(as_text=True)
            data = resp.get_json()
            if resp.status_code >= 400:
                raise mcp_server.ApiError(data.get("error"))
            return data

        orig = mcp_server.http
        mcp_server.http = fake_http
        try:
            init = mcp_server.handle({"method": "initialize",
                                      "params": {"protocolVersion": "2025-06-18"}})
            self.assertEqual(init["protocolVersion"], "2025-06-18")
            names = {t["name"] for t in mcp_server.handle({"method": "tools/list"})["tools"]}
            self.assertTrue({"dmm_status", "dmm_measure", "dmm_setup"} <= names)

            def call(name, **args):
                return mcp_server.handle({"method": "tools/call",
                                          "params": {"name": name, "arguments": args}})

            res = call("dmm_setup", function="DCV", range="AUTO")
            self.assertFalse(res.get("isError"), res)
            res = call("dmm_measure", count=3)
            self.assertFalse(res.get("isError"), res)
            self.assertEqual(_json.loads(res["content"][0]["text"])["summary"]["n"]
                             + res["structuredContent"]["summary"]["n_ol"], 3)
            res = call("dmm_history", seconds=5, max_points=10)
            self.assertLessEqual(res["structuredContent"]["returned"], 10)
            res = call("dmm_setup", function="NOPE")
            self.assertTrue(res["isError"])
            res = call("dmm_options", include_advanced=True)
            self.assertIn("advanced", res["structuredContent"])
            with self.assertRaises(LookupError):
                call("no_such_tool")
        finally:
            mcp_server.http = orig

    # -- 按功能区分的量程与设置 ------------------------------------------------
    def test_function_profiles_and_ranges(self):
        meta = self.json(self.client.get("/api/meta"))
        fns = {f["code"]: f for f in meta["functions"]}
        self.assertIn("shunt", fns["DCV"]["features"])
        self.assertNotIn("shunt", fns["CONT"]["features"])
        self.assertEqual(fns["CONT"]["fixed_range"], "1000 Ω")
        self.assertIn("R9", [r["code"] for r in fns["RES2W"]["ranges"]])
        self.assertNotIn("R3", [r["code"] for r in fns["DCI"]["ranges"]])

        self.client.post("/api/control", json={"action": "function", "value": "RES2W"})
        r = self.client.post("/api/control", json={"action": "range", "value": "R8"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["instrument"]["range_label"], "10 MΩ")
        st = self.json(self.client.get("/api/state"))
        self.assertEqual(st["instrument"]["range_label"], "10 MΩ")

        self.client.post("/api/control", json={"action": "function", "value": "DCI"})
        r = self.client.post("/api/control", json={"action": "range", "value": "R3"})
        self.assertEqual(r.status_code, 400)

        self.client.post("/api/control", json={"action": "function", "value": "CONT"})
        r = self.client.post("/api/control", json={"action": "range", "value": "R4"})
        self.assertEqual(r.status_code, 400)
        self.client.post("/api/control", json={"action": "range", "value": "AUTO"})
        self.client.post("/api/control", json={"action": "function", "value": "DCV"})

    def test_cont_threshold(self):
        r = self.client.post("/api/agent/setup", json={"function": "CONT", "cont_threshold": 25})
        self.assertEqual(r.status_code, 200, r.get_json())
        st = self.json(self.client.get("/api/agent/status"))
        self.assertEqual(st["cont_threshold"], 25)
        self.assertEqual(self.json(self.client.get("/api/state"))["cont_threshold"], 25)
        rd = self.json(self.client.get("/api/agent/reading?fresh=1"))["reading"]
        self.assertIn("continuity", rd)
        # 高级设置写入同样更新缓存
        r = self.client.post("/api/advanced/set", json={"key": "cont_limit", "value": 40})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.json(self.client.get("/api/state"))["cont_threshold"], 40)
        for bad in (0, 1001, "x"):
            self.assertEqual(self.client.post(
                "/api/agent/setup", json={"cont_threshold": bad}).status_code, 400)
        r = self.client.post("/api/agent/setup", json={"function": "DCV", "cont_threshold": 10})
        self.assertEqual(r.status_code, 400)

    # -- 完全释放控制 ------------------------------------------------------------
    def test_release_and_resume(self):
        self.client.post("/api/agent/setup", json={"function": "DCV"})
        r = self.client.post("/api/device/release")
        self.assertEqual(r.status_code, 200)
        st = self.json(self.client.get("/api/state"))
        self.assertTrue(st["released"])
        self.assertEqual(st["device"]["state"], "local")
        import json as _json
        self.assertTrue(_json.loads(appmod.SETTINGS_FILE.read_text())["released"])
        # 仪器命令被拒绝
        r = self.client.post("/api/control", json={"action": "rate", "value": "PR3"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "local_mode")
        # 数据仍在：可以测量、记录、改上位机侧限值
        r = self.client.post("/api/agent/measure", json={"count": 3})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.client.post("/api/log/start").status_code, 200)
        self.client.post("/api/log/stop")
        r = self.client.post("/api/limits", json={"mode": "voltage", "enabled": False})
        self.assertEqual(r.status_code, 200)
        # DCV 下可进入分流模式（不需要发命令）
        r = self.client.post("/api/config", json={"display_mode": "current", "shunt_ohm": 0.1})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.client.post("/api/config", json={"display_mode": "voltage", "shunt_ohm": 0.1})
        # 恢复
        r = self.client.post("/api/device/resume")
        self.assertEqual(r.status_code, 200)
        st = self.json(self.client.get("/api/state"))
        self.assertFalse(st["released"])
        self.assertEqual(st["device"]["state"], "online")
        self.assertEqual(self.client.post(
            "/api/control", json={"action": "rate", "value": "PR3"}).status_code, 200)
        self.assertFalse(_json.loads(appmod.SETTINGS_FILE.read_text())["released"])

    # -- 记录分析 / 回放 ---------------------------------------------------------
    def test_log_analysis(self):
        name = "7461A_20260101_000000.csv"
        path = appmod.LOG_DIR / name
        rows = [",".join(appmod.CSV_FIELDS)]
        t0 = 1_700_000_000.0
        for i in range(3000):
            ol = 1 if i % 500 == 7 else 0
            v = "" if ol else f"{0.01 + 0.001 * (i % 10):.12g}"
            unit, fn = ("V", "DCV") if i < 2500 else ("Ω", "RES2W")
            alarm = "high" if i == 100 else "off"
            rows.append(f"x,{t0 + i * 0.1:.6f},{i * 0.1:.3f},{i},1,{fn},{unit},AUTO,PR3,"
                        f"{v},,{v},{unit},{ol},0.1,voltage,{alarm},raw")
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        d = self.json(self.client.get(f"/api/logs/{name}/analysis?max_points=200"))
        self.assertTrue(d["ok"])
        self.assertEqual(d["unit"], "V")                     # 行数最多的单位
        self.assertEqual(d["meta"]["rows"], 3000)
        self.assertEqual([u["unit"] for u in d["meta"]["units"]], ["V", "Ω"])
        self.assertEqual(len(d["meta"]["segments"]), 2)
        self.assertFalse(d["raw"])
        self.assertLessEqual(len(d["points"]), 200)
        st = d["stats"]
        self.assertEqual(st["n"] + st["n_ol"], 2500)
        self.assertEqual(st["n_ol"], 5)
        self.assertEqual(st["n_alarm"], 1)
        self.assertAlmostEqual(st["min"], 0.01)
        self.assertAlmostEqual(st["max"], 0.019)
        # 桶保留极值
        self.assertAlmostEqual(min(p[1] for p in d["points"] if p[1] is not None), 0.01)
        # 时间范围 + 原始点
        d = self.json(self.client.get(
            f"/api/logs/{name}/analysis?start={t0 + 10}&end={t0 + 20}&max_points=500"))
        self.assertTrue(d["raw"])
        self.assertEqual(d["range"]["rows"], 101)
        self.assertEqual(len(d["points"]), 101)
        # 另一个单位
        d = self.json(self.client.get(f"/api/logs/{name}/analysis?unit=%CE%A9"))
        self.assertEqual(d["stats"]["n"] + d["stats"]["n_ol"], 500)
        self.assertEqual(d["functions"], ["RES2W"])
        # 错误参数
        self.assertEqual(self.client.get(f"/api/logs/{name}/analysis?unit=Hz").status_code, 400)
        self.assertEqual(self.client.get(f"/api/logs/{name}/analysis?start=abc").status_code, 400)
        self.assertEqual(self.client.get("/api/logs/../x.csv/analysis").status_code, 404)
        # 导出时间段
        r = self.client.get(f"/api/logs/{name}/download?start={t0 + 10}&end={t0 + 20}")
        self.assertEqual(r.status_code, 200)
        lines = r.get_data(as_text=True).strip().split("\n")
        self.assertEqual(lines[0], ",".join(appmod.CSV_FIELDS))
        self.assertEqual(len(lines), 102)
        self.assertIn("selection", r.headers["Content-Disposition"])
        path.unlink()


if __name__ == "__main__":
    unittest.main()
