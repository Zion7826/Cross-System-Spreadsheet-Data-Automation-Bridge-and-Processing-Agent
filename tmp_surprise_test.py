# -*- coding: utf-8 -*-
"""突袭测试：全新业务域（零售 POS 销售 + 仓储库存），验证对从未见过的数据的泛化能力。
数据现场生成，真值用独立 pandas 复算（等价人工公式），不经过被测引擎的逻辑。
"""
import sys, time, json
from pathlib import Path
import pandas as pd

ROOT = Path(r"D:/WorkBuddy/希望早睡百景/agent_app")
sys.path.insert(0, str(ROOT))
from core.parser import load_table
from core.llm import build_llm
from core.agent import TableBridgeAgent

TMP = Path(r"D:/WorkBuddy/希望早睡百景/agent_app/tmp_surprise")
TMP.mkdir(exist_ok=True)

# ---------- 1. 现场生成全新结构数据 ----------
sales_rows = [
    # 小票号, 门店编号, 分店名称, 交易日期, 货号, 品类, 件数, 吊牌价, 实收金额
    ["P10001", "SH001", "上海旗舰店", "2026/8/1", "G-A01", "女装", 2, 399.0, 798.0],
    ["P10002", "SH001", "上海旗舰店", "2026/8/2", "G-B02", "男装", 1, 599.0, 599.0],
    ["P10003", "SH001", "上海旗舰店", "2026/8/3", "G-C03", "童装", 3, 199.0, 597.0],
    ["P10004", "SH001", "上海旗舰店", "2026/8/5", "G-A01", "女装", 1, 399.0, 399.0],
    ["P10005", "SH002", "杭州西湖店", "2026/8/1", "G-A01", "女装", 4, 399.0, 1596.0],
    ["P10006", "SH002", "杭州西湖店", "2026/8/2", "G-B02", "男装", 2, 599.0, 1198.0],
    ["P10007", "SH002", "杭州西湖店", "2026/8/4", "G-C03", "童装", 1, 199.0, 199.0],
    ["P10008", "GZ001", "广州天河店", "2026/8/2", "G-A01", "女装", 5, 399.0, 1995.0],
    ["P10009", "GZ001", "广州天河店", "2026/8/3", "G-D04", "鞋靴", 2, 899.0, 1798.0],
    ["P10010", "SZ001", "深圳南山店", "2026/8/1", "G-B02", "男装", 3, 599.0, 1797.0],
    ["P10011", "SZ001", "深圳南山店", "2026/8/6", "G-D04", "鞋靴", 1, 899.0, 899.0],
    ["P10012", "SH001", "上海旗舰店", "2026/8/8", "G-B02", "男装", 1, 599.0, 549.0],   # 打折
    ["P10013", "SH002", "杭州西湖店", "2026/8/9", "G-A01", "女装", 2, 399.0, 718.0],
]
dirty_rows = [
    ["P10014", "SH001", "上海旗舰店", "2026/8/10", "G-A01", "女装", 1, 399.0, "退款"],   # 金额文本
    [None, None, None, None, None, None, None, None, None],                              # 全空行
    ["P10015", "SH002", "杭州西湖店", "2026/8/11", "G-XXX", "###", 2, 99.0, 198.0],      # 品类乱码
]
sales = pd.DataFrame(sales_rows + dirty_rows, columns=[
    "小票号", "门店编号", "分店名称", "交易日期", "货号", "品类", "件数", "吊牌价", "实收金额"])
sales.to_excel(TMP / "POS销售明细.xlsx", index=False)

inv = pd.DataFrame([
    ["SH001", "女装", 15000.0], ["SH001", "男装", 22000.0], ["SH001", "童装", 8000.0],
    ["SH002", "女装", 30000.0], ["SH002", "男装", 12000.0], ["SH002", "童装", 5000.0],
    ["SH002", "###", 100.0],
    ["GZ001", "女装", 40000.0], ["GZ001", "鞋靴", 25000.0],
    ["SZ001", "男装", 18000.0], ["SZ001", "鞋靴", 9000.0],
], columns=["门店代码", "大类", "月末库存金额"])
inv.to_excel(TMP / "仓储库存月报.xlsx", index=False)

# ---------- 2. 独立复算真值（人工公式等价） ----------
s = sales_rows  # 只取正常行
df = pd.DataFrame(s, columns=sales.columns)
df["实收金额"] = df["实收金额"].astype(float)
east = df[df["分店名称"].isin(["上海旗舰店", "杭州西湖店"])]
agg = east.groupby(["分店名称", "品类"], as_index=False)["实收金额"].sum()
iv = inv[inv["门店代码"].isin(["SH001", "SH002"])]
m = {"SH001": "上海旗舰店", "SH002": "杭州西湖店"}
iv = iv.assign(门店=iv["门店代码"].map(m))
gt = agg.merge(iv[["门店", "大类", "月末库存金额"]], left_on=["分店名称", "品类"],
               right_on=["门店", "大类"], how="left")
gt["周转天数"] = (gt["月末库存金额"] / gt["实收金额"] * 30).round(4)
gt = gt.sort_values(["分店名称", "品类"]).reset_index(drop=True)
print("=== 独立复算真值（华东 2 店） ===")
print(gt.to_string())

# ---------- 3. 引擎实跑 ----------
cfg = json.load(open(ROOT / "webapp" / "llm_config.json", encoding="utf-8"))
llm = build_llm(base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"], timeout=120)
pos = load_table(str(TMP / "POS销售明细.xlsx"), "POS销售明细")
stock = load_table(str(TMP / "仓储库存月报.xlsx"), "仓储库存月报")

# 先独立看一眼模型规划（与 rules.py 相同的 meta 组装）
import math
from core.llm import json_llm, llm_plan_freeform
tm = []
for t in (pos, stock):
    ct = [{"name": str(c), "dtype": ("number" if ("int" in str(t.df[c].dtype) or "float" in str(t.df[c].dtype))
            else ("date" if "datetime" in str(t.df[c].dtype) else "text"))} for c in t.columns]
    sr = []
    for _, r in t.df.head(6).iterrows():
        rec = {str(k): (None if v is None or (isinstance(v, float) and math.isnan(v))
                        else (str(v) if not isinstance(v, (int, float)) else v)) for k, v in r.items()}
        if any(v not in (None, "") for v in rec.values()):
            sr.append(rec)
        if len(sr) >= 3:
            break
    tm.append({"name": t.name, "columns": ct, "samples": sr})
ins = ("已挂载两张表：POS销售明细 和 仓储库存月报。请生成一份区域运营导入表，目标列为："
       "门店、品类、销售额（把实收金额按门店和品类分组求和）、月末库存（从仓储库存月报按门店和品类匹配过来）、"
       "周转天数（等于月末库存金额除以销售额再乘30，销售额为0则留空）。"
       "只要华东区的门店（上海旗舰店、杭州西湖店），其他门店不要。无法处理的行请单独标记到异常。")
_p = llm_plan_freeform(json_llm(llm), ins, tm)
print("=== 模型规划（独立观察） ===")
print(json.dumps(_p, ensure_ascii=False, indent=1)[:1800])

ins = ("已挂载两张表：POS销售明细 和 仓储库存月报。请生成一份区域运营导入表，目标列为："
       "门店、品类、销售额（把实收金额按门店和品类分组求和）、月末库存（从仓储库存月报按门店和品类匹配过来）、"
       "周转天数（等于月末库存金额除以销售额再乘30，销售额为0则留空）。"
       "只要华东区的门店（上海旗舰店、杭州西湖店），其他门店不要。无法处理的行请单独标记到异常。")
agent = TableBridgeAgent(llm=llm)
t0 = time.time()
out = agent.run(ins, [pos, stock])
dt = time.time() - t0
r = out["result"] if "result" in out else out
o = r.output
print(f"\n=== 引擎输出（技能={type(out['skill']).__name__} 耗时={dt:.1f}s） {len(o)} 行 ===")
print(o.to_string())
print(f"\n异常 {len(r.anomalies)} 条：")
for a in r.anomalies[:8]:
    print("  -", a.get("异常类型"), str(a.get("说明"))[:80])

# ---------- 4. 自动比对 ----------
ok_rows = 0; bad = []
keymap = {"门店": "分店名称"}
for _, g in gt.iterrows():
    hit = o[(o["门店"] == g["分店名称"]) & (o["品类"] == g["品类"])] if "门店" in o.columns else pd.DataFrame()
    if len(hit) != 1:
        bad.append(("缺行", g["分店名称"], g["品类"])); continue
    row = hit.iloc[0]
    sv = float(row.get("销售额", row.get("实收金额", -1)))
    if abs(sv - float(g["实收金额"])) > 1e-6: bad.append(("销售额", g["分店名称"], sv, g["实收金额"]))
    ivv = float(row.get("月末库存", row.get("月末库存金额", -1)))
    if abs(ivv - float(g["月末库存金额"])) > 1e-6: bad.append(("月末库存", g["分店名称"], ivv, g["月末库存金额"]))
    tv = row.get("周转天数")
    try: tv = float(tv)
    except Exception: bad.append(("周转天数非数值", g["分店名称"], tv)); continue
    if abs(tv - float(g["周转天数"])) > 1e-3: bad.append(("周转天数", g["分店名称"], tv, g["周转天数"]))
    ok_rows += 1
n_anom_ok = len(r.anomalies) >= 1  # 脏数据兜底：退款文本行/全空行至少隔离其一
print(f"\n=== 比对：真值 {len(gt)} 行，逐行命中且数值一致 {ok_rows} 行；不一致 {len(bad)} 处 ===")
for b in bad[:8]: print("   ", b)
print(f"脏数据兜底隔离 >= 1 条：{'PASS' if n_anom_ok else 'FAIL'}（实际 {len(r.anomalies)} 条）")
print("总体：" + ("PASS ✅" if not bad and n_anom_ok and ok_rows == len(gt) else "FAIL ❌"))
