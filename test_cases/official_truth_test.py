# -*- coding: utf-8 -*-
"""官方数据集真值端到端测试：源表 → 引擎输出 vs 官方导入模板（ground truth）逐行比对。"""
import sys, time
from pathlib import Path
sys.path.insert(0, ".")
sys.path.insert(0, "agent_app")
import pandas as pd
from core.parser import load_table
from core.llm import build_llm
from core.agent import TableBridgeAgent

DS = str(Path(__file__).resolve().parent.parent / "data" / "official") + "/"


def norm_dt(v):
    return pd.Timestamp(v) if v is not None else None


def compare(gt_df, out_df, cols_text, cols_num, cols_dt, label):
    """gt: 官方真值；out: 引擎输出。逐行逐列比对。"""
    assert len(gt_df) == len(out_df), f"{label}: 行数不一致 gt={len(gt_df)} out={len(out_df)}"
    bad = []
    for i in range(len(gt_df)):
        for c in cols_text:
            g, o = str(gt_df[c].iloc[i]).strip(), str(out_df[c].iloc[i]).strip()
            if g != o:
                bad.append((i, c, g, o))
        for c in cols_num:
            g, o = float(gt_df[c].iloc[i]), float(out_df[c].iloc[i] or 0)
            if abs(g - o) > 1e-6:
                bad.append((i, c, g, o))
        for c in cols_dt:
            g, o = norm_dt(gt_df[c].iloc[i]), norm_dt(out_df[c].iloc[i])
            if g != o:
                bad.append((i, c, g, o))
    n_total = len(gt_df) * (len(cols_text) + len(cols_num) + len(cols_dt))
    print(f"[{label}] 比对 {len(gt_df)} 行 × {len(cols_text)+len(cols_num)+len(cols_dt)} 列 = {n_total} 单元格，"
          f"不一致 {len(bad)} 处 -> {'PASS' if not bad else 'FAIL'}")
    for b in bad[:8]:
        print("   不一致:", b)
    return not bad


def _llm():
    import json
    cfg_path = Path(__file__).resolve().parent.parent / "webapp" / "llm_config.json"
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    return build_llm(base_url=cfg["base_url"], api_key=cfg["api_key"],
                     model=cfg["model"], timeout=280)

def test_leave():
    """测试B：OA 请假单 → HR 最低版本导入（13 行真值）。"""
    print("=" * 72)
    oa = load_table(DS + "OA导出请假单.xlsx", "OA导出请假单")
    ins = ("基于已挂载的 OA导出请假单，生成 HR 系统最低版本导入文件，目标列为："
           "工号（取工号）、姓名（取申请人）、部门（取部门）、缺勤类别（取请假类型）、"
           "起始时间（起始日期拼接起始时间时刻，格式如 2026-04-27 20:00）、"
           "终止时间（结束日期拼接结束时间时刻）、合计小时（天大于0时等于天乘8，否则取小时）、"
           "免打卡（固定填 是）。这是跨系统格式转换任务，不涉及假期抵扣计算，请用通用字段映射完成。")
    llm = _llm()
    agent = TableBridgeAgent(llm=llm)
    t0 = time.time()
    out = agent.run(ins, [oa])
    dt = time.time() - t0
    r = out['result'] if 'result' in out else out
    print(f"技能={type(out['skill']).__name__} 耗时={dt:.1f}s 输出 {len(r.output)} 行, 列={list(r.output.columns)}")
    if len(r.output) == 0:
        for a in r.anomalies[:5]:
            print("  异常:", a.get("异常类型"), str(a.get("说明"))[:150])
        for t_ in r.trace:
            print("  轨迹:", t_.get("步骤"), "=>", str(t_.get("结果"))[:120])

    # 兼容 LLM 规划的列名，统一对齐到官方 8 列
    df = r.output
    ren = {}
    for c in df.columns:
        cl = str(c)
        if cl in ("姓名", "申请人"):
            ren[c] = "姓名"
        elif "缺勤" in cl or cl == "请假类型":
            ren[c] = "缺勤类别"
        elif "起始" in cl and "日期" not in cl:
            ren[c] = "起始时间"
        elif "终止" in cl or ("结束" in cl and "日期" not in cl):
            ren[c] = "终止时间"
        elif "小时" in cl:
            ren[c] = "合计小时"
        elif "打卡" in cl:
            ren[c] = "免打卡"
    df = df.rename(columns=ren)

    gt = load_table(DS + "HR 系统需要最低版本导入.xls", "GT")
    ok = compare(gt.df, df,
                 cols_text=["工号", "姓名", "部门", "缺勤类别", "免打卡"],
                 cols_num=["合计小时"],
                 cols_dt=["起始时间", "终止时间"], label="请假导入")
    return ok


def test_overtime():
    """测试A：ERP 加班 3.6 万行 → HR 加班导入（3.6 万行真值）。"""
    print("=" * 72)
    erp = load_table(DS + "ERP导出加班.XLSX", "ERP导出加班")
    ins = ("基于已挂载的 ERP导出加班，生成 HR 系统的导入文件，目标列依次为："
           "工号（取工号）、姓名（取姓名）、部门（取部门）、职位（取职位）、"
           "起始日期（取起始日期）、申请加班（取申请加班，保持数值）。"
           "这是跨系统格式转换任务，不涉及加班类型判定或时长换算，请现场规划目标列。")
    llm = _llm()
    agent = TableBridgeAgent(llm=llm)
    t0 = time.time()
    out = agent.run(ins, [erp])
    dt = time.time() - t0
    r = out['result'] if 'result' in out else out
    print(f"技能={type(out['skill']).__name__} 耗时={dt:.1f}s 输出 {len(r.output)} 行, 列={list(r.output.columns)}")
    if len(r.output) == 0:
        for a in r.anomalies[:5]:
            print("  异常:", a.get("异常类型"), str(a.get("说明"))[:150])
        for t_ in r.trace:
            print("  轨迹:", t_.get("步骤"), "=>", str(t_.get("结果"))[:120])

    gt = load_table(DS + "导入HR 加班.xls", "GT")
    df = r.output
    # 列名对齐（LLM 规划的列名可能略有出入）
    common = [c for c in gt.df.columns if c in df.columns]
    missing = [c for c in gt.df.columns if c not in df.columns]
    if missing:
        print("缺失列:", missing)
        return False
    ok = compare(gt.df[common], df[common],
                 cols_text=["工号", "姓名", "部门", "职位"],
                 cols_num=["申请加班"],
                 cols_dt=["起始日期"], label="加班导入")
    return ok


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    results = {}
    if which in ("all", "overtime"):
        results["加班导入 3.6万行 vs 官方真值"] = test_overtime()
    if which in ("all", "leave"):
        results["请假导入 13行 vs 官方真值"] = test_leave()
    print("=" * 72)
    for k, v in results.items():
        print(("PASS " if v else "FAIL "), k)
