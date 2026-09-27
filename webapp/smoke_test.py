# -*- coding: utf-8 -*-
"""Web API 端到端冒烟测试：demo -> run -> export -> verify -> settings。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8620"


def call(path, payload=None, method=None, raw=False):
    data = json.dumps(payload or {}).encode() if payload is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=120)
    if raw:
        return r
    return json.load(r)


files = call("/api/demo", {})["files"]
ids = {f["detected_role"]: f["id"] for f in files}
print("roles:", list(ids))

d = call("/api/run", {"instruction": "做假期余量结算：先扣年假额度，超出部分自动转成事假",
                      "file_ids": [ids["leave"], ids["balance"]], "skill_key": "", "role_override": {}})
print("run:", d["run_id"], d["skill_name"], "rows:", d["total_rows"], "anom:", len(d["anomalies"]))
print("mapping.leave:", {f["label"]: f["source"] for f in d["mapping"]["leave"]["fields"]})
print("row0:", d["rows"][0])

r = call("/api/export", {"run_id": d["run_id"], "date_mode": "date"}, raw=True)
open("outputs/test_export.xlsx", "wb").read() if False else open("outputs/test_export.xlsx", "wb").write(r.read())
print("export ok -> outputs/test_export.xlsx")

v = call("/api/verify", {})
print("verify:", [(c["结果"], c["验收项"]) for c in v["checks"]])

s = call("/api/settings")
print("settings:", s)
t = call("/api/settings/test", {"base_url": "https://example.invalid/v1", "api_key": "sk-x", "model": "x"})
print("settings test(expect fail):", t["ok"], t["message"][:60])

# overtime + 筛选
d2 = call("/api/run", {"instruction": "把生产部 2026年1月到3月 加班时长大于等于2小时的记录整理成HR加班导入模板",
                       "file_ids": [ids["overtime"]], "skill_key": "", "role_override": {}})
print("overtime rows:", d2["total_rows"], "depts:", sorted({r["dept"] for r in d2["rows"]}))

# generic map
d3 = call("/api/run", {"instruction": "", "file_ids": [ids["leave"]], "skill_key": "generic_map",
                       "params": {"target_spec": [
                           {"label": "员工工号", "source": "工号", "dtype": "text", "expr": ""},
                           {"label": "休假天数", "source": "请假天数", "dtype": "number", "expr": ""},
                       ]}})
print("generic rows:", d3["total_rows"], "cols:", d3["columns"])
print("ALL_API_OK")
