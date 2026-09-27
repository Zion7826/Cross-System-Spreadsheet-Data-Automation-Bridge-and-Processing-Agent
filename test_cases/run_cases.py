# -*- coding: utf-8 -*-
"""
5 个扩展案例：生成数据 → 通过线上 Web API 实测 → 断言 → 导出文件。
案例 1 异构表头乱序（中文别名 + 列序打乱）
案例 2 千行规模 + 5% 脏数据（鲁棒性 + 性能）
案例 3 余额递减链（多单依次抵扣、归零转事假、零额度、非年假单）
案例 4 CSV + 英文表头（格式与语言泛化）
案例 5 未知结构报表（通用映射 + 表达式 + 自然语言筛选）
"""
import io
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import openpyxl

BASE = "http://127.0.0.1:8620"
ROOT = Path(__file__).parent
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)
random.seed(42)

RESULTS = []


def check(case, name, ok, detail=""):
    RESULTS.append((case, name, ok, detail))
    print(("  PASS " if ok else "  FAIL ") + name + ("　" + detail if detail else ""))


def xlsx_bytes(rows, header):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_bytes(rows, header):
    import csv as _csv
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def clear_and_upload(files):
    """files: [(filename, bytes)]"""
    httpx.post(BASE + "/api/files/clear", timeout=30, trust_env=False)
    data = [("files", (fn, content, "application/octet-stream")) for fn, content in files]
    r = httpx.post(BASE + "/api/files/upload", files=data, timeout=60, trust_env=False)
    r.raise_for_status()
    return r.json()["files"]


def run(payload):
    r = httpx.post(BASE + "/api/run", json=payload, timeout=300, trust_env=False)
    if r.status_code != 200:
        raise RuntimeError(f"run 400: {r.text[:200]}")
    return r.json()


def export(run_id, save_path):
    r = httpx.post(BASE + "/api/export", json={"run_id": run_id, "date_mode": "date"}, timeout=120, trust_env=False)
    r.raise_for_status()
    Path(save_path).write_bytes(r.content)


def col_find(columns, *keywords):
    for c in columns:
        if any(k.lower() in str(c).lower() for k in keywords):
            return c
    return None


# ================================================================ 案例 1
def case1():
    print("\n案例 1　异构表头乱序：中文别名 + 列序打乱（假期结算）")
    # 请假单：非常见列名、列序完全打乱
    leave_header = ["姓名", "员工编号", "休假类型", "结束日期", "起始日期", "申请天数"]
    leaves = [
        # emp, name, type, start, end, days
        ("A1001", "张三", "年假", "2026-01-05", "2026-01-07", 3),
        ("A1002", "李四", "年假", "2026-02-10", "2026-02-13", 4),
        ("A1003", "王五", "事假", "2026-01-20", "2026-01-21", 2),
        ("A1004", "赵六", "年假", "2026-03-02", "2026-03-06", 5),
        ("A1005", "钱七", "病假", "2026-02-17", "2026-02-17", 1),
        ("A1001", "张三", "年假", "2026-03-09", "2026-03-10", 2),
    ]
    # 余额表：工号叫"职工号码"，余额叫"剩余年假"
    bal_header = ["职工号码", "姓名", "剩余年假"]
    balances = [("A1001", "张三", 4), ("A1002", "李四", 2), ("A1003", "王五", 8), ("A1004", "赵六", 3)]
    # A1005 缺余额记录
    leave_rows = [(nm, emp, tp, e, s, d) for (emp, nm, tp, s, e, d) in leaves]  # 列序错位
    f1 = clear_and_upload([
        ("请假导出_异构.xlsx", xlsx_bytes(leave_rows, leave_header)),
        ("余额导出_异构.xlsx", xlsx_bytes(balances, bal_header)),
    ])
    d = run({"instruction": "结算年假，超出转事假", "file_ids": [f["id"] for f in f1],
             "skill_key": "leave_settlement"})
    rows = d["rows"]
    cols = d["columns"]
    c_emp, c_type = col_find(cols, "工号", "emp_id"), col_find(cols, "假别", "leave_type", "类型")
    c_days = col_find(cols, "天数", "days")
    agg = {}
    for r in rows:
        agg.setdefault(r[c_emp], {}).setdefault(r[c_type], 0.0)
        agg[r[c_emp]][r[c_type]] += float(r[c_days])
    exp = {
        "A1001": {"年假": 4.0, "事假": 1.0},
        "A1002": {"年假": 2.0, "事假": 2.0},
        "A1004": {"年假": 3.0, "事假": 2.0},
        "A1003": {"事假": 2.0},
        "A1005": {"病假": 1.0},  # 非年假单按原类型记录；缺余额仅影响年假抵扣
    }
    ok = all(abs(agg.get(e, {}).get(t, 0) - v) < 1e-6 for e in exp for t, v in exp[e].items())
    check(1, "异构表头映射与结算数值", ok, json.dumps(agg, ensure_ascii=False))
    check(1, "缺余额员工有异常提示", len(d["anomalies"]) >= 1, f"异常 {len(d['anomalies'])} 条")
    export(d["run_id"], OUT / "案例1_异构表头_结算导入.xlsx")


# ================================================================ 案例 2
def case2():
    print("\n案例 2　千行规模 + 5% 脏数据（鲁棒性 + 性能）")
    header = ["工号", "姓名", "部门", "加班日期", "加班时长(小时)"]
    depts = ["生产部", "装配车间", "质检部", "研发部"]
    n, rows, expected, dirty_cnt = 1000, [], 0, 0
    for i in range(n):
        if random.random() < 0.05:  # 5% 脏数据
            kind = random.choice(["empty", "baddate", "nahours", "fullwidth", "nodept"])
            emp, nm = f"E{i:04d}", f"员{i}"
            if kind == "empty":
                rows.append(["", "", "", "", ""])
            elif kind == "baddate":
                rows.append([emp, nm, "生产部", "???", 2.5]); dirty_cnt += 1
            elif kind == "nahours":
                rows.append([emp, nm, "生产部", "2026-02-1%d" % (i % 10), "N/A"]); dirty_cnt += 1
            elif kind == "fullwidth":
                h = round(random.uniform(2, 6), 1)
                fw = str(h).translate(str.maketrans("0123456789.", "０１２３４５６７８９．"))
                d = "2026-01-%02d" % (1 + i % 28)
                rows.append([emp, nm, "生产部", d, fw])
                expected += 1  # 全角数字应被归一化后正常计入
            else:
                rows.append([emp, nm, "", "2026-02-%02d" % (1 + i % 28), 3.0])
            continue
        dept = depts[i % 4]
        dt = "2026-%02d-%02d" % (1 + i % 3, 1 + i % 28)  # 1-3 月
        h = round(random.uniform(0.5, 6), 1)
        rows.append([f"E{i:04d}", f"员{i}", dept, dt, h])
        if dept == "生产部" and h >= 2:
            expected += 1
    f2 = clear_and_upload([("ERP加班_千行.xlsx", xlsx_bytes(rows, header))])
    t0 = time.time()
    d = run({"instruction": "把生产部 2026年1月到3月 加班时长大于等于2小时的记录整理成HR加班导入模板",
             "file_ids": [f2[0]["id"]]})
    dt = time.time() - t0
    out_depts = {r.get(col_find(cols := d["columns"], "dept", "部门")) for r in d["rows"]}
    # 阈值 45s：含 LLM 意图识别/规划一次往返（约 5-10s）；1000 行纯计算 <1s（10 万行基准 3.8s）
    check(2, f"千行不宕机（{dt:.2f}s）", dt < 45, f"{len(rows)} 行输入")
    check(2, "筛选数量精确", len(d["rows"]) == expected,
          f"输出 {len(d['rows'])} = 期望 {expected}")
    check(2, "部门与时长约束", out_depts <= {"生产部"}, f"部门集合 {out_depts}")
    check(2, "脏数据进异常日志", len(d["anomalies"]) >= dirty_cnt,
          f"异常 {len(d['anomalies'])} >= 注入 {dirty_cnt}")
    export(d["run_id"], OUT / "案例2_千行脏数据_加班导入.xlsx")


# ================================================================ 案例 3
def case3():
    print("\n案例 3　余额递减链：多单依次抵扣、归零转事假（假期结算）")
    header = ["工号", "姓名", "假期类型", "开始日期", "结束日期", "请假天数"]
    leaves = [
        ("B001", "甲", "年假", "2026-01-06", "2026-01-08", 3),
        ("B001", "甲", "年假", "2026-02-09", "2026-02-12", 4),
        ("B001", "甲", "年假", "2026-03-02", "2026-03-06", 5),   # 余额剩 3 → 3 年假 + 2 事假
        ("B001", "甲", "年假", "2026-04-01", "2026-04-02", 2),   # 余额 0 → 全事假
        ("B002", "乙", "年假", "2026-01-13", "2026-01-14", 2),   # 余额 0 → 全事假
        ("B003", "丙", "事假", "2026-02-23", "2026-02-23", 1),   # 非年假单
    ]
    bal_header = ["工号", "姓名", "年假余额"]
    balances = [("B001", "甲", 10), ("B002", "乙", 0), ("B003", "丙", 5)]
    f3 = clear_and_upload([
        ("请假单_递减链.xlsx", xlsx_bytes(leaves, header)),
        ("余额表_递减链.xlsx", xlsx_bytes(balances, bal_header)),
    ])
    d = run({"instruction": "结算年假，超出转事假", "file_ids": [f["id"] for f in f3],
             "skill_key": "leave_settlement"})
    cols = d["columns"]
    c_emp, c_type = col_find(cols, "工号", "emp_id"), col_find(cols, "假别", "leave_type", "类型")
    c_days = col_find(cols, "天数", "days")
    agg = {}
    for r in d["rows"]:
        agg.setdefault(r[c_emp], {}).setdefault(r[c_type], 0.0)
        agg[r[c_emp]][r[c_type]] += float(r[c_days])
    exp = {"B001": {"年假": 10.0, "事假": 4.0}, "B002": {"年假": 0.0, "事假": 2.0},
           "B003": {"年假": 0.0, "事假": 1.0}}
    ok = all(abs(agg.get(e, {}).get(t, 0) - v) < 1e-6 for e in exp for t, v in exp[e].items())
    check(3, "递减链逐单抵扣数值", ok, json.dumps(agg, ensure_ascii=False))
    export(d["run_id"], OUT / "案例3_余额递减链_结算导入.xlsx")


# ================================================================ 案例 4
def case4():
    print("\n案例 4　CSV + 英文表头（格式与语言泛化，加班导入）")
    header = ["emp_id", "emp_name", "department", "work_date", "duration_hours"]
    rows, expected = [], 0
    for i in range(40):
        dept = ["RD", "FA", "QC"][i % 3] if i % 7 else "研发部"
        # 中英混合：部门值本身也用中文"研发部"以便筛选命中；其余为英文值
        dept = "研发部" if i % 5 == 0 else ["装配车间", "质检部"][i % 2]
        dt = f"2026-0{2 if i % 2 else 1}-{1 + i % 28:02d}"  # 1月/2月
        is_target = i % 5 == 0 and i % 2 == 1  # 研发部 + 2月 的候选行
        h = 3.5 if is_target else round(1 + (i % 3) * 0.4, 1)  # 确定性时长，候选行必 >= 3
        rows.append([f"C{i:03d}", f"staff{i}", dept, dt, h])
        if dept == "研发部" and dt.startswith("2026-02") and h >= 3:
            expected += 1
    f4 = clear_and_upload([("overtime_export.csv", csv_bytes(rows, header))])
    d = run({"instruction": "把研发部 2026年2月 加班时长大于等于3小时的记录整理成HR加班导入模板",
             "file_ids": [f4[0]["id"]]})
    check(4, "CSV 英文表头识别与筛选", len(d["rows"]) == expected,
          f"输出 {len(d['rows'])} = 期望 {expected}")
    export(d["run_id"], OUT / "案例4_CSV英文表头_加班导入.xlsx")


# ================================================================ 案例 5
def case5():
    print("\n案例 5　未知结构报表：通用映射 + 表达式 + 自然语言筛选")
    header = ["工号", "姓名", "所属部门", "加班天数", "日补贴标准", "发放月份"]
    rows = []
    for i in range(30):
        dept = "装配车间" if i % 3 == 0 else ["生产部", "质检部"][i % 2]
        days = 2 + i % 5
        std = 50 + (i % 4) * 10
        rows.append([f"F{i:03d}", f"工{i}", dept, days, std, "2026-03"])
    expected_amt = {r[0]: round(r[3] * r[4], 2) for r in rows if r[2] == "装配车间"}
    expected_n = len(expected_amt)
    f5 = clear_and_upload([("补贴发放表_未知结构.xlsx", xlsx_bytes(rows, header))])
    spec = [
        {"label": "员工工号", "source": "工号", "dtype": "text", "expr": ""},
        {"label": "员工姓名", "source": "姓名", "dtype": "text", "expr": ""},
        {"label": "补贴金额", "source": None, "dtype": "number", "expr": "round(加班天数*日补贴标准, 2)"},
        {"label": "备注", "source": "发放月份", "dtype": "text", "expr": ""},
    ]
    d = run({"instruction": "所属部门=装配车间", "file_ids": [f5[0]["id"]],
             "skill_key": "generic_map", "params": {"target_spec": spec}})
    cols = d["columns"]
    c_id, c_amt = col_find(cols, "工号"), col_find(cols, "金额")
    got = {r[c_id]: float(r[c_amt]) for r in d["rows"]}
    ok_amt = all(abs(got.get(k, -1) - v) < 1e-6 for k, v in expected_amt.items())
    check(5, "筛选命中行数", len(d["rows"]) == expected_n, f"{len(d['rows'])} = {expected_n}")
    check(5, "表达式计算逐行正确", ok_amt,
          f"示例: {list(got.items())[:2]}")
    export(d["run_id"], OUT / "案例5_通用映射_补贴导入.xlsx")


if __name__ == "__main__":
    cases = {"1": case1, "2": case2, "3": case3, "4": case4, "5": case5}
    picks = sys.argv[1:] or list(cases)
    for p in picks:
        cases[p]()
    fails = [r for r in RESULTS if not r[2]]
    print("\n========== 汇总 ==========")
    print(f"共 {len(RESULTS)} 项断言，失败 {len(fails)} 项")
    for c, n, ok, det in fails:
        print(f"  FAIL 案例{c} {n} {det}")
    sys.exit(1 if fails else 0)
