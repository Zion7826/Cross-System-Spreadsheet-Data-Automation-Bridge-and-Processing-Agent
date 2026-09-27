# -*- coding: utf-8 -*-
"""
跨系统表格数据自动化桥接与处理智能体 · 交互工作台

启动：streamlit run app.py
"""
from __future__ import annotations

import io
import os
import sys
from datetime import datetime

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.agent import TableBridgeAgent, classify_intent, guess_role   # noqa: E402
from core.fields import field_label                                    # noqa: E402
from core.llm import build_llm                                         # noqa: E402
from core.parser import RawTable, load_table                           # noqa: E402
from core.rules import SKILL_LIST, SKILLS                              # noqa: E402
from verify import run_all as run_checks                               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(HERE, "data", "demo")
TPL_DIR = os.path.join(HERE, "data", "templates")

st.set_page_config(page_title="跨系统表格桥接智能体", page_icon="🔗", layout="wide")
st.markdown("""
<style>
  .block-container {padding-top: 1.6rem;}
  .agent-title {font-size: 1.5rem; font-weight: 700; margin-bottom: .2rem;}
  .agent-sub {color: #5b6472; font-size: .9rem; margin-bottom: 1rem;}
  .step-card {border-left: 3px solid #4b6bfb; padding: .4rem .8rem; margin: .3rem 0; background: #f6f8ff;}
</style>
""", unsafe_allow_html=True)


def init_state():
    st.session_state.setdefault("tables", [])
    st.session_state.setdefault("run_out", None)
    st.session_state.setdefault("export_bytes", None)
    st.session_state.setdefault("export_name", "目标系统导入文件.xlsx")


init_state()


# ------------------------------------------------------------------ 侧边栏

with st.sidebar:
    st.markdown("### 🔗 表格桥接智能体")
    page = st.radio("导航", ["智能体工作台", "通用映射（未知结构）", "验收自检", "使用说明"], index=0)
    st.divider()
    st.markdown("**大模型增强（可选）**")
    st.caption("不填也可运行：表头映射、条件解析走规则引擎，数值计算永远由确定性引擎完成。")
    base_url = st.text_input("API Base URL", value=os.getenv("LLM_BASE_URL", ""),
                             placeholder="https://xxx/v1")
    api_key = st.text_input("API Key", value=os.getenv("LLM_API_KEY", ""), type="password")
    model = st.text_input("模型", value=os.getenv("LLM_MODEL", "gpt-4o-mini"))
    llm = build_llm(base_url, api_key, model)
    st.caption("大模型状态：" + ("已连接（用于意图识别与未知表头映射兜底）" if llm else "未配置 → 纯规则模式"))
    st.divider()
    date_mode = st.radio("日期输出格式", ["date", "text"], index=0, horizontal=True,
                         format_func=lambda x: "Excel 日期型" if x == "date" else "yyyy-mm-dd 文本")


def agent():
    return TableBridgeAgent(llm=llm)


def load_demo():
    names = ["ERP_加班记录导出.xlsx", "OA_请假单导出.xlsx", "HR_年假余额导出.xlsx"]
    tables = []
    for n in names:
        p = os.path.join(DEMO_DIR, n)
        if os.path.exists(p):
            tables.append(load_table(p, n))
    return tables


# ================================================================== 工作台

if page == "智能体工作台":
    st.markdown('<div class="agent-title">跨系统表格数据自动化桥接与处理智能体</div>', unsafe_allow_html=True)
    st.markdown('<div class="agent-sub">自然语言指令 → 异构表头解析 → 跨表关联计算 → 目标系统导入模板（异常自动隔离）</div>',
                unsafe_allow_html=True)

    c1, c2 = st.columns([1.15, 1])
    with c1:
        uploads = st.file_uploader("① 上传源系统导出表（可多选，xlsx / csv）",
                                   type=["xlsx", "xls", "csv"], accept_multiple_files=True)
        if st.button("载入内置演示数据", use_container_width=False):
            st.session_state.tables = load_demo()
            st.session_state.run_out = None
            st.rerun()
        if uploads:
            sig = tuple(f"{f.name}:{getattr(f, 'size', 0)}" for f in uploads)
            if sig != st.session_state.get("upload_sig"):
                st.session_state.tables = [load_table(f, f.name) for f in uploads]
                st.session_state.run_out = None
                st.session_state.upload_sig = sig

    tables: list[RawTable] = st.session_state.tables
    if tables:
        st.info("  ｜  ".join(t.summary() for t in tables))
    else:
        st.warning("还没有数据。上传导出表，或点「载入内置演示数据」。")

    with c2:
        st.markdown("② 用自然语言下发指令")
        examples = [
            "做假期余量结算：先扣年假额度，超出部分自动转成事假，年假和事假分行记录",
            "把生产部 2026年1月到3月 加班时长大于等于2小时的记录，整理成 HR 加班导入模板",
            "整理成目标系统能导入的加班表",
        ]
        ex = st.selectbox("示例指令（点击填入）", ["（自定义）"] + examples, index=0)
        instruction = st.text_area("指令", value="" if ex == "（自定义）" else ex, height=90,
                                   label_visibility="collapsed",
                                   placeholder="例如：结算年假，超出部分转事假，只处理生产部 1 月的记录")

    # 技能与表角色
    auto_skill = classify_intent(instruction, llm) if (instruction or tables) else "leave_settlement"
    skill_key = st.selectbox("③ 执行技能（自动识别，可手动改）",
                             [s.key for s in SKILL_LIST],
                             index=[s.key for s in SKILL_LIST].index(auto_skill),
                             format_func=lambda k: SKILLS[k].name)
    st.caption(SKILLS[skill_key].desc)

    skill = SKILLS[skill_key]
    role_override = {}
    if tables and skill.sources:
        _, notes = agent().assign_roles(tables)
        cols = st.columns(len(skill.sources))
        for (role, (label, _)), col in zip(skill.sources.items(), cols):
            with col:
                default = None
                for t in tables:
                    r, _ = guess_role(t.columns)
                    if r == role:
                        default = t.name
                        break
                names = [t.name for t in tables]
                idx = names.index(default) if default in names else 0
                role_override[role] = st.selectbox(f"{label}  ←  用哪张表", names, index=idx,
                                                   key=f"role_{skill_key}_{role}")

    run_btn = st.button("▶ 运行智能体", type="primary", use_container_width=True, disabled=not tables)

    if run_btn and tables:
        # 收集人工映射覆盖
        mapping_override = {}
        for key in list(st.session_state.keys()):
            if key.startswith("ov_"):
                _, role, field = key.split("_", 2)
                mapping_override.setdefault(role, {})[field] = st.session_state[key]
        try:
            with st.spinner("智能体执行中：解析表头 → 关联字段 → 跨表计算 → 生成导入模板"):
                st.session_state.run_out = agent().run(
                    instruction, tables, skill_key=skill_key,
                    role_override=role_override, mapping_override=mapping_override)
                out_path = os.path.join(HERE, "outputs", "目标系统导入文件.xlsx")
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                agent().export(st.session_state.run_out, out_path, date_mode=date_mode)
                with open(out_path, "rb") as f:
                    st.session_state.export_bytes = f.read()
                st.session_state.export_name = f"{skill.name}_{datetime.now():%m%d_%H%M}.xlsx"
        except Exception as ex:
            st.error(f"执行失败：{ex}")

    out = st.session_state.run_out
    if out:
        res = out["result"]
        st.divider()
        t1, t2, t3, t4, t5 = st.tabs(["执行轨迹", "字段映射", "结果数据", "计算明细", "异常日志"])

        with t1:
            for s in res.trace:
                st.markdown(f'<div class="step-card"><b>{s["步骤"]}</b>：{s["内容"]}<br><span style="color:#2e7d32">→ {s["结果"]}</span></div>',
                            unsafe_allow_html=True)

        with t2:
            st.caption("自动映射结果可直接改写，改完点下方按钮重算。")
            for role, tbl in out["assigned"].items():
                m = (res.detail.get("mapping") or {}).get(role)
                if not m:
                    continue
                st.markdown(f"**{role} ← {tbl.name}**")
                none_opt = "（不使用）"
                for key, info in m.items():
                    opts = [none_opt] + list(tbl.columns)
                    cur = info.get("source") or none_opt
                    st.selectbox(f"{field_label(key)}  ←  源列（{info.get('method')}，{info.get('score')}分）",
                                 opts, index=opts.index(cur) if cur in opts else 0,
                                 key=f"ov_{role}_{key}")
            if st.button("↻ 按修正后的映射重算"):
                mapping_override = {}
                for key in list(st.session_state.keys()):
                    if key.startswith("ov_"):
                        _, role, field = key.split("_", 2)
                        v = st.session_state[key]
                        mapping_override.setdefault(role, {})[field] = None if v == "（不使用）" else v
                st.session_state.run_out = agent().run(
                    instruction, tables, skill_key=skill_key,
                    role_override=role_override, mapping_override=mapping_override)
                out_path = os.path.join(HERE, "outputs", "目标系统导入文件.xlsx")
                agent().export(st.session_state.run_out, out_path, date_mode=date_mode)
                with open(out_path, "rb") as f:
                    st.session_state.export_bytes = f.read()
                st.rerun()

        with t3:
            st.dataframe(res.output, use_container_width=True, height=380)
            st.caption(f"共 {len(res.output)} 行，列顺序完全对齐目标系统导入模板。")

        with t4:
            if res.detail.get("settlement"):
                st.dataframe(pd.DataFrame(res.detail["settlement"]), use_container_width=True, height=340)
            else:
                st.info("该技能无逐条计算明细。")

        with t5:
            adf = res.anomaly_df()
            st.dataframe(adf if not adf.empty else pd.DataFrame([{"说明": "无异常"}]),
                         use_container_width=True, height=300)
            st.caption(f"异常 {len(res.anomalies)} 条：已隔离，不参与计算，随导出文件一并交付供人工复核。")

        if st.session_state.export_bytes:
            st.download_button("⬇ 下载目标系统导入文件（含异常日志）",
                               data=st.session_state.export_bytes,
                               file_name=st.session_state.export_name,
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               type="primary")


# ================================================================== 通用映射

elif page == "通用映射（未知结构）":
    st.markdown("## 通用字段映射导出 · 未知结构自适应")
    st.caption("面对不在初期测试用例中的新报表：上传 → 逐列指定映射或写计算表达式 → 直接产出导入模板。")

    f = st.file_uploader("上传任意结构报表", type=["xlsx", "xls", "csv"])
    if f:
        tbl = load_table(f, f.name)
        st.success(tbl.summary())
        st.dataframe(tbl.df.head(20), use_container_width=True, height=240)

        st.markdown("### 目标列配置")
        n = st.number_input("目标列数量", min_value=1, max_value=30, value=4, step=1)
        none_opt = "（空）"
        expr_opt = "（写表达式）"
        opts = [none_opt, expr_opt] + list(tbl.columns)
        spec = []
        for i in range(int(n)):
            a, b, c, d = st.columns([1.1, 1.4, 1.4, 0.8])
            with a:
                label = st.text_input(f"目标列名 {i+1}", value=f"目标列{i+1}", key=f"gl_{i}")
            with b:
                src = st.selectbox(f"来源 {i+1}", opts, key=f"gs_{i}")
            with c:
                dtype = st.selectbox(f"类型 {i+1}", ["text", "number", "date"], key=f"gd_{i}")
            with d:
                expr = st.text_input(f"表达式 {i+1}", value="", key=f"ge_{i}",
                                     placeholder="如 round(时长*8)")
            spec.append({"label": label,
                         "source": None if src in (none_opt, expr_opt) else src,
                         "expr": expr if src == expr_opt else "",
                         "dtype": dtype})

        cond_text = st.text_input("筛选条件（可选，自然语言）", value="")
        if st.button("▶ 生成导入文件", type="primary"):
            out = agent().run(cond_text, [tbl], skill_key="generic_map",
                              params={"target_spec": spec})
            op = os.path.join(HERE, "outputs", "通用映射导出.xlsx")
            os.makedirs(os.path.dirname(op), exist_ok=True)
            agent().export(out, op, date_mode=date_mode)
            st.dataframe(out["result"].output, use_container_width=True)
            st.dataframe(out["result"].anomaly_df(), use_container_width=True)
            with open(op, "rb") as fh:
                st.download_button("⬇ 下载导入文件", data=fh.read(),
                                   file_name="通用映射导出.xlsx", type="primary")


# ================================================================== 验收自检

elif page == "验收自检":
    st.markdown("## 验收自检")
    st.caption("对照任务书三条验收项自动执行，输出 PASS / FAIL 与证据。")
    if st.button("▶ 运行全部验收项", type="primary"):
        with st.spinner("正在跑三条验收..."):
            rows = run_checks()
        st.session_state["checks"] = rows
    if "checks" in st.session_state:
        df = pd.DataFrame(st.session_state["checks"])
        for _, r in df.iterrows():
            icon = "✅ PASS" if r["结果"] == "PASS" else "❌ FAIL"
            with st.expander(f"{icon}　{r['验收项']}", expanded=(r["结果"] == "FAIL")):
                st.markdown(f"**验收标准**：{r['验收标准']}")
                st.markdown(f"**实测详情**：{r['详情']}")


# ================================================================== 说明

else:
    st.markdown("## 使用说明与交付说明")
    st.markdown("""
**它能干什么**

面向企业内部 ERP / OA / HR 等老旧系统的数据流转场景：业务人员用一句自然语言，
把多张结构各异的导出表，自动解析、关联、计算，产出**目标系统能直接导入**的标准文件。

**四层能力**

| 层 | 作用 | 实现 |
|---|---|---|
| 意图与表头解析 | 自然语言指令 → 执行技能；异构表头 → 标准字段 | 语义词典 + 归一化 + 模糊匹配，大模型可选兜底 |
| 复杂逻辑与筛选计算 | 跨表关联、年假抵扣与事假转化、时长换算、动态筛选 | 确定性规则引擎（pandas），不交给模型算数 |
| 字段重组与格式转换 | 按目标模板列序/类型重组，写出 xlsx | 目标模板驱动，无合并单元格、无公式 |
| 异常兜底 | 空行/脏字符/缺关联键/数值异常 | 行级隔离 + 异常日志随文件交付 |

**怎么跑**

```bash
pip install pandas openpyxl streamlit
python make_demo.py     # 生成演示数据与空白导入模板
streamlit run app.py
python verify.py        # 命令行跑三条验收
```

**准确性保障**：所有数值与逻辑判定由确定性代码完成，大模型只参与"理解"（意图、未知表头），
不参与算数——因此不存在数值捏造或漂移，结果可复现、可逐格比对。
""")
    st.markdown("### 目标导入模板下载")
    for name in sorted(os.listdir(TPL_DIR)) if os.path.isdir(TPL_DIR) else []:
        p = os.path.join(TPL_DIR, name)
        with open(p, "rb") as fh:
            st.download_button(f"⬇ {name}", data=fh.read(), file_name=name)
