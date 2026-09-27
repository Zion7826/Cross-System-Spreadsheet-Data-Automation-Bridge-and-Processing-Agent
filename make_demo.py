# -*- coding: utf-8 -*-
"""
生成演示数据与空白目标模板。

包含刻意注入的脏数据（空行、干扰字符、格式不一致、缺失余额），
用于验证「抗错鲁棒性」与「异常隔离」。
运行：python make_demo.py
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta

import pandas as pd

from core.exporter import write_target_template
from core.rules import SKILLS

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "demo")
TPL = os.path.join(HERE, "data", "templates")
os.makedirs(OUT, exist_ok=True)
os.makedirs(TPL, exist_ok=True)

random.seed(20260101)

DEPTS = ["生产部", "装配车间", "品质部", "仓储部", "研发部"]
NAMES = ["张伟", "李娜", "王强", "刘洋", "陈静", "赵磊", "孙芳", "周涛", "吴敏", "郑凯",
         "黄丽", "徐斌", "何蕾", "罗刚", "高颖"]


def _emp(i: int):
    return f"E{1000 + i}", NAMES[i % len(NAMES)], DEPTS[i % len(DEPTS)]


def build_overtime(n=60):
    rows = []
    for i in range(n):
        emp, nm, dept = _emp(i % 15)
        d = datetime(2026, 1, 1) + timedelta(days=random.randint(0, 89))
        while d.weekday() == 6 and random.random() < 0.7:
            d += timedelta(days=1)
        start = datetime(d.year, d.month, d.day, 18, 0)
        hours = random.choice([1.5, 2, 2.5, 3, 3.5, 4, 5, 6, 8])
        end = start + timedelta(hours=hours)
        rows.append({
            "员工编号": emp, "员工姓名": nm, "所属部门": dept,
            "加班日期": d.strftime("%Y/%m/%d"),
            "开始时间": start.strftime("%Y-%m-%d %H:%M"),
            "结束时间": end.strftime("%Y-%m-%d %H:%M"),
            "加班时长(h)": f"{hours}小时",
            "加班事由": random.choice(["订单赶工", "设备抢修", "月末盘点", "项目上线"]),
        })
    # 注入脏数据
    rows.insert(7, {k: "" for k in rows[0]})                                   # 空行
    rows[12]["加班时长(h)"] = "待补录"                                           # 不可解析数值
    rows[20]["加班日期"] = "2026年2月30日"                                      # 非法日期
    rows[31]["员工编号"] = ""                                                   # 缺关联键
    rows[40]["加班时长(h)"] = "３．５小时"                                      # 全角数字
    return pd.DataFrame(rows)


def build_leave(n=26):
    rows = []
    for i in range(n):
        emp, nm, dept = _emp(i % 15)
        s = datetime(2026, 1, 5) + timedelta(days=random.randint(0, 80))
        days = random.choice([0.5, 1, 1, 2, 2.5, 3, 5, 6])
        e = s + timedelta(days=int(days) - 1) if days >= 1 else s
        rows.append({
            "工号": emp, "姓名": nm, "部门": dept,
            "假期类型": random.choice(["年假", "年假", "年假", "事假", "病假"]),
            "开始日期": s.strftime("%Y-%m-%d"),
            "结束日期": e.strftime("%Y-%m-%d"),
            "请假天数": days,
            "请假事由": random.choice(["返乡探亲", "个人事务", "身体不适", "外出办事"]),
        })
    rows.insert(5, {k: "" for k in rows[0]})
    rows[9]["请假天数"] = "N/A"
    rows[14]["工号"] = ""
    rows[18]["假期类型"] = "年假"
    rows[18]["请假天数"] = 9      # 故意超出余额，触发事假转化
    return pd.DataFrame(rows)


def build_balance():
    rows = []
    for i in range(15):
        emp, nm, dept = _emp(i)
        rows.append({
            "工号": emp, "姓名": nm,
            "年假额度": 10,
            "已休年假": random.choice([0, 1, 2, 3]),
            "年假余额": random.choice([0, 1, 2, 3, 5, 5.5, 7, 8, 10]),
        })
    rows[3]["年假余额"] = ""      # 空值 -> 异常
    rows.append({"工号": "", "姓名": "幽灵员工", "年假额度": 5, "已休年假": 0, "年假余额": 5})
    return pd.DataFrame(rows)


def main():
    # 源表（异构表头、异构格式，模拟 ERP / OA / HR 三套老系统导出）
    build_overtime().to_excel(os.path.join(OUT, "ERP_加班记录导出.xlsx"), index=False)
    build_leave().to_excel(os.path.join(OUT, "OA_请假单导出.xlsx"), index=False)
    build_balance().to_excel(os.path.join(OUT, "HR_年假余额导出.xlsx"), index=False)

    # 目标系统空白导入模板
    write_target_template(os.path.join(TPL, "目标系统_假期导入模板.xlsx"),
                          SKILLS["leave_settlement"].target_columns)
    write_target_template(os.path.join(TPL, "目标系统_加班导入模板.xlsx"),
                          SKILLS["overtime_import"].target_columns)

    print("演示数据已生成：", OUT)
    for f in sorted(os.listdir(OUT)):
        print("  -", f)
    print("目标模板已生成：", TPL)
    for f in sorted(os.listdir(TPL)):
        print("  -", f)


if __name__ == "__main__":
    main()
