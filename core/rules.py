# -*- coding: utf-8 -*-
"""
业务规则技能库（确定性计算，绝不捏造数值）。

每个 Skill = 一段可复用、可验证的业务流水线：
  源表角色 -> 字段映射 -> 条件筛选 -> 跨表计算 -> 目标模板输出 + 异常隔离

新增业务只需加一个 Skill，不动主流程。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from .filters import _resolve_column, build_mask, describe_conditions, parse_conditions
from .fields import field_label
from .mapper import apply_mapping, auto_map, merge_mapping
from .parser import RawTable, norm_date, norm_number, norm_text


# ------------------------------------------------------------------ 数据结构

@dataclass
class TargetColumn:
    key: str
    label: str
    dtype: str = "text"        # text / number / date
    width: int = 14


@dataclass
class RunResult:
    skill: str
    skill_name: str
    output: pd.DataFrame
    anomalies: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def anomaly_df(self) -> pd.DataFrame:
        if not self.anomalies:
            return pd.DataFrame(columns=["源表", "行号", "异常类型", "说明", "原始数据"])
        return pd.DataFrame(self.anomalies)


class Skill:
    """业务技能基类。"""

    key: str = ""
    name: str = ""
    desc: str = ""
    # 源角色 -> (显示名, 需要的标准字段)
    sources: dict[str, tuple[str, list[str]]] = {}
    target_columns: list[TargetColumn] = []

    def run(self, ctx: dict) -> RunResult:
        raise NotImplementedError


def _new_anomaly(table: str, row_no, kind: str, desc: str, raw: Any) -> dict:
    if isinstance(raw, dict):
        raw = {k: ("" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v))
               for k, v in raw.items()}
    return {"源表": table, "行号": row_no, "异常类型": kind, "说明": desc, "原始数据": raw}


def _as_date(v) -> datetime | None:
    """统一取日期：None / NaT / 非日期一律返回 None（NaT 是 datetime 子类，必须显式排除）。"""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v if isinstance(v, datetime) else None


def _seed_parse_anomalies(ctx: dict, anomalies: list[dict]):
    """把解析阶段（空行、表头前的杂行等）的异常并入结果日志，随导出文件交付。"""
    for _role, t in (ctx.get("tables") or {}).items():
        for a in getattr(t, "anomalies", []):
            anomalies.append(_new_anomaly(t.name, a.row_no, "解析阶段", a.reason, a.raw))


def _prepare(table: RawTable, role: str, wanted: list[str], ctx: dict):
    """对该源表做字段映射，返回 (标准字段 DataFrame, 映射信息)。"""
    skill_key = ctx.get("skill_key", "")
    override = (ctx.get("mapping_override") or {}).get(role, {})
    mapping = auto_map(table.columns, wanted, llm=ctx.get("llm"))
    mapping = merge_mapping(mapping, override)
    std = apply_mapping(table.df, mapping)
    return std, mapping


def _apply_conditions(std: pd.DataFrame, table: RawTable, ctx: dict, trace: list):
    conds = ctx.get("conditions") or parse_conditions(ctx.get("condition_text", ""))
    if conds:
        # 条件优先在原始表上求值：标准化帧只保留技能所需字段，
        # 业务列（如「部门」）可能已被丢弃或改名为规范字段，直接在 std 上
        # 模糊匹配会错配到无关列（曾把 dept 匹配到 name 导致整表被过滤）。
        raw = table.df.reset_index(drop=True)
        std = std.reset_index(drop=True)
        raw_conds, std_conds = [], []
        for c in conds:
            (raw_conds if _resolve_column(raw, c.get("field")) is not None else std_conds).append(c)
        mask = build_mask(raw, raw_conds) & build_mask(std, std_conds)
        dropped = int((~mask).sum())
        std = std[mask].reset_index(drop=True)
        trace.append({"步骤": "动态筛选", "内容": describe_conditions(conds),
                      "结果": f"保留 {len(std)} 行，过滤 {dropped} 行"})
    return std


# ------------------------------------------------------------------ 技能一：假期余量结算

class LeaveSettlementSkill(Skill):
    """年假抵扣 + 超出部分自动转事假，并在输出中分离记录。"""

    key = "leave_settlement"
    name = "假期余量结算（年假抵扣 / 事假转化）"
    desc = "读取 OA 请假单与 HR 年假余额表，按员工逐单抵扣年假，超出部分自动转为事假并分行记录。"

    sources = {
        "leave": ("OA 请假单", ["emp_id", "name", "leave_type", "start_date", "end_date", "days", "reason"]),
        "balance": ("HR 年假余额表", ["emp_id", "name", "balance"]),
    }
    target_columns = [
        TargetColumn("emp_id", "工号", "text", 12),
        TargetColumn("name", "姓名", "text", 12),
        TargetColumn("leave_type", "假别", "text", 12),
        TargetColumn("start_date", "开始日期", "date", 14),
        TargetColumn("end_date", "结束日期", "date", 14),
        TargetColumn("days", "天数", "number", 10),
        TargetColumn("balance_left", "剩余年假余额", "number", 16),
    ]

    def run(self, ctx: dict) -> RunResult:
        trace: list[dict] = []
        anomalies: list[dict] = []
        _seed_parse_anomalies(ctx, anomalies)
        leave_t: RawTable = ctx["tables"]["leave"]
        bal_t: RawTable = ctx["tables"]["balance"]

        trace.append({"步骤": "载入源表", "内容": f"{leave_t.summary()}；{bal_t.summary()}",
                      "结果": "OK"})

        leave_std, leave_map = _prepare(leave_t, "leave", self.sources["leave"][1], ctx)
        bal_std, bal_map = _prepare(bal_t, "balance", self.sources["balance"][1], ctx)
        trace.append({"步骤": "表头语义映射",
                      "内容": f"请假单：{ {field_label(k): v['source'] for k, v in leave_map.items()} }；"
                              f"余额表：{ {field_label(k): v['source'] for k, v in bal_map.items()} }",
                      "结果": "OK"})

        leave_std = _apply_conditions(leave_std, leave_t, ctx, trace)

        # 余额索引（按工号；工号缺失则回退姓名）
        balance_map: dict[str, float] = {}
        name_map: dict[str, str] = {}
        for _, r in bal_std.iterrows():
            key = norm_text(r.get("emp_id")) or norm_text(r.get("name"))
            if not key:
                anomalies.append(_new_anomaly(bal_t.name, "—", "缺关联键",
                                              "余额表该行既无工号也无姓名，无法建立关联", dict(r)))
                continue
            val = norm_number(r.get("balance"))
            if val is None:
                anomalies.append(_new_anomaly(bal_t.name, "—", "数值不可解析",
                                              f"年假余额无法解析为数字：{r.get('balance')!r}", dict(r)))
                val = 0.0
            balance_map[key] = val
            if norm_text(r.get("name")):
                name_map[key] = norm_text(r.get("name"))
        remaining = dict(balance_map)
        trace.append({"步骤": "跨表关联", "内容": f"以工号/姓名为关联键，载入 {len(balance_map)} 名员工的年假余额",
                      "结果": "OK"})

        rows: list[dict] = []
        settlement: list[dict] = []
        apply_types = ctx.get("params", {}).get("deduct_types") or ["年假", "年休假", "annual"]

        for i, r in leave_std.iterrows():
            emp = norm_text(r.get("emp_id"))
            nm = norm_text(r.get("name")) or name_map.get(emp, "")
            key = emp or nm
            ltype = norm_text(r.get("leave_type")) or "年假"
            s = _as_date(r.get("start_date"))
            e = _as_date(r.get("end_date"))
            raw_days = norm_number(r.get("days"))

            if not key:
                anomalies.append(_new_anomaly(leave_t.name, i + 2, "缺关联键",
                                              "请假单缺少工号与姓名，无法与余额表关联，已隔离", dict(r)))
                continue

            # 天数：优先取字段；缺失则用起止日期推算（含首尾）
            if raw_days is None:
                if s is not None and e is not None:
                    raw_days = float((e.date() - s.date()).days + 1)
                    src = "起止日期推算"
                else:
                    anomalies.append(_new_anomaly(leave_t.name, i + 2, "天数缺失",
                                                  "既无天数字段也无法由起止日期推算，已隔离", dict(r)))
                    continue
            else:
                src = "天数字段"
            if raw_days is None or (isinstance(raw_days, float) and math.isnan(raw_days)) or raw_days <= 0:
                anomalies.append(_new_anomaly(leave_t.name, i + 2, "数值异常",
                                              f"请假天数 {raw_days} 非正数，已隔离", dict(r)))
                continue

            bal = float(remaining.get(key, 0.0))
            is_annual = any(t in ltype for t in apply_types)
            if key not in remaining:
                anomalies.append(_new_anomaly(leave_t.name, i + 2, "未匹配余额",
                                              f"余额表中无 {key} 的记录，年假额度按 0 处理", dict(r)))

            if not is_annual:
                rows.append({"emp_id": emp, "name": nm, "leave_type": ltype,
                             "start_date": s, "end_date": e, "days": round(raw_days, 4),
                             "balance_left": round(bal, 4)})
                settlement.append({"工号": emp, "姓名": nm, "原假别": ltype,
                                   "申请天数": raw_days, "年假余额": bal,
                                   "抵扣年假": 0.0, "转事假": 0.0, "剩余年假": bal,
                                   "天数来源": src})
                continue

            annual_used = min(raw_days, bal)          # 优先扣年假
            personal = round(raw_days - annual_used, 4)  # 超出转事假
            left = round(bal - annual_used, 4)
            remaining[key] = left

            if annual_used > 0:
                rows.append({"emp_id": emp, "name": nm, "leave_type": "年假",
                             "start_date": s, "end_date": e,
                             "days": round(annual_used, 4), "balance_left": left})
            if personal > 0:
                rows.append({"emp_id": emp, "name": nm, "leave_type": "事假",
                             "start_date": s, "end_date": e,
                             "days": personal, "balance_left": left})
            settlement.append({"工号": emp, "姓名": nm, "原假别": ltype,
                               "申请天数": raw_days, "年假余额": bal,
                               "抵扣年假": round(annual_used, 4), "转事假": personal,
                               "剩余年假": left, "天数来源": src})

        out = pd.DataFrame(rows, columns=[c.key for c in self.target_columns])
        trace.append({"步骤": "复杂逻辑计算",
                      "内容": "逐单判定：优先扣减年假额度，超出部分转为事假并在输出中分离记录",
                      "结果": f"输出 {len(out)} 行（含分离后的年假/事假记录）"})
        trace.append({"步骤": "异常隔离", "内容": "不可用行单独记录，不参与计算",
                      "结果": f"隔离 {len(anomalies)} 条"})
        return RunResult(self.key, self.name, out, anomalies, trace,
                         {"settlement": settlement,
                          "mapping": {"leave": leave_map, "balance": bal_map},
                          "balance_left": remaining})


# ------------------------------------------------------------------ 技能二：加班记录导入转换

class OvertimeImportSkill(Skill):
    """加班记录 -> 目标 HR 系统加班导入模板（含部门/时间段筛选、时长换算、加班类型判定）。"""

    key = "overtime_import"
    name = "加班记录转 HR 导入模板"
    desc = "解析 ERP 加班导出，按部门/时间段/时长筛选，换算加班小时数与加班类型，输出目标系统导入模板。"

    sources = {
        "overtime": ("ERP 加班记录", ["emp_id", "name", "dept", "date", "start_date", "end_date", "hours", "reason"]),
    }
    target_columns = [
        TargetColumn("emp_id", "工号", "text", 12),
        TargetColumn("name", "姓名", "text", 12),
        TargetColumn("dept", "部门", "text", 14),
        TargetColumn("date", "加班日期", "date", 14),
        TargetColumn("start_time", "加班开始时间", "text", 16),
        TargetColumn("end_time", "加班结束时间", "text", 16),
        TargetColumn("hours", "加班小时数", "number", 12),
        TargetColumn("ot_type", "加班类型", "text", 14),
        TargetColumn("reason", "加班事由", "text", 24),
    ]

    def run(self, ctx: dict) -> RunResult:
        trace, anomalies = [], []
        _seed_parse_anomalies(ctx, anomalies)
        t: RawTable = ctx["tables"]["overtime"]
        trace.append({"步骤": "载入源表", "内容": t.summary(), "结果": "OK"})

        std, mapping = _prepare(t, "overtime", self.sources["overtime"][1], ctx)
        trace.append({"步骤": "表头语义映射",
                      "内容": str({field_label(k): v["source"] for k, v in mapping.items()}),
                      "结果": "OK"})
        std = _apply_conditions(std, t, ctx, trace)

        holidays = set(ctx.get("params", {}).get("holidays") or [])
        rows = []
        for i, r in std.iterrows():
            emp = norm_text(r.get("emp_id"))
            nm = norm_text(r.get("name"))
            if not emp:
                anomalies.append(_new_anomaly(t.name, i + 2, "缺关联键",
                                              f"缺少工号（目标系统导入必需键），已隔离供人工复核；姓名：{nm or '空'}",
                                              dict(r)))
                continue

            d = _as_date(r.get("date")) or _as_date(r.get("start_date"))
            if d is None:
                anomalies.append(_new_anomaly(t.name, i + 2, "日期不可解析",
                                              f"加班日期无法解析：{r.get('date')!r}", dict(r)))
                continue

            hours = norm_number(r.get("hours"))
            s, e = _as_date(r.get("start_date")), _as_date(r.get("end_date"))
            if hours is None and s is not None and e is not None:
                hours = round((e - s).total_seconds() / 3600.0, 4)
            if hours is None or hours <= 0:
                anomalies.append(_new_anomaly(t.name, i + 2, "数值异常",
                                              f"加班小时数缺失或非正数：{r.get('hours')!r}", dict(r)))
                continue

            key = d.strftime("%Y-%m-%d")
            if key in holidays:
                ot_type = "法定节假日加班"
            elif d.weekday() >= 5:
                ot_type = "休息日加班"
            else:
                ot_type = "工作日加班"

            rows.append({
                "emp_id": emp, "name": nm, "dept": norm_text(r.get("dept")),
                "date": d,
                "start_time": s.strftime("%H:%M") if s is not None else "",
                "end_time": e.strftime("%H:%M") if e is not None else "",
                "hours": round(float(hours), 4),
                "ot_type": ot_type,
                "reason": norm_text(r.get("reason")),
            })

        out = pd.DataFrame(rows, columns=[c.key for c in self.target_columns])
        trace.append({"步骤": "字段换算与规则判定",
                      "内容": "小时数 = 时长字段（缺失则用起止时间差）；加班类型按日期星期/法定节假日判定",
                      "结果": f"输出 {len(out)} 行"})
        trace.append({"步骤": "异常隔离", "内容": "缺工号/日期不可解析/小时数异常的行隔离",
                      "结果": f"隔离 {len(anomalies)} 条"})
        return RunResult(self.key, self.name, out, anomalies, trace,
                         {"mapping": {"overtime": mapping}})


# ------------------------------------------------------------------ 技能三：通用映射导出（泛化自适应）

class GenericMapSkill(Skill):
    """任意结构报表 -> 任意目标模板：人工/自动指定映射，支持简单计算列。"""

    key = "generic_map"
    name = "通用字段映射导出（未知结构自适应）"
    desc = "面对未知结构报表，允许用户逐列指定映射或写入计算表达式，直接产出目标导入模板。"

    sources = {}
    target_columns = []

    def run(self, ctx: dict) -> RunResult:
        trace, anomalies = [], []
        _seed_parse_anomalies(ctx, anomalies)
        t: RawTable = ctx["tables"]["source"]
        spec: list[dict] = ctx.get("params", {}).get("target_spec") or []
        if not spec:
            raise ValueError("尚未定义目标模板列，请先在「通用映射」页配置目标列。")

        trace.append({"步骤": "载入源表", "内容": t.summary(), "结果": "OK"})
        df = t.df.copy()
        conds = ctx.get("conditions") or parse_conditions(ctx.get("condition_text", ""))
        if conds:
            mask = build_mask(df, conds)
            keep = int(mask.sum())
            df = df[mask].reset_index(drop=True)
            trace.append({"步骤": "动态筛选", "内容": describe_conditions(conds),
                          "结果": f"保留 {keep} 行"})

        cols, rows = [], []
        safe_cols = {str(c): i for i, c in enumerate(df.columns)}
        for i, r in df.iterrows():
            rec = {}
            ok = True
            for item in spec:
                label = item["label"]
                expr = item.get("expr", "")
                src = item.get("source")
                dtype = item.get("dtype", "text")
                try:
                    if expr:
                        val = _safe_eval(expr, r, safe_cols, extra=rec)
                    elif src and src in df.columns:
                        val = r[src]
                    else:
                        val = None
                except Exception as ex:
                    val = None
                    anomalies.append(_new_anomaly(t.name, i + 2, "计算表达式异常",
                                                  f"列「{label}」表达式求值失败：{ex}", dict(r)))
                    ok = False
                if dtype == "number":
                    val = norm_number(val)
                    if val is None:
                        anomalies.append(_new_anomaly(t.name, i + 2, "数值不可解析",
                                                      f"列「{label}」无法转为数值：{r.get(src)!r}", dict(r)))
                        ok = False
                elif dtype == "date":
                    val = norm_date(val)
                    if val is None:
                        anomalies.append(_new_anomaly(t.name, i + 2, "日期不可解析",
                                                      f"列「{label}」无法转为日期", dict(r)))
                        ok = False
                else:
                    val = norm_text(val)
                rec[label] = val
            if ok:
                rows.append(rec)
            cols = list(rec.keys())

        out = pd.DataFrame(rows, columns=cols)
        trace.append({"步骤": "字段重组与格式转换", "内容": f"按目标模板 {cols} 重组并做类型清洗",
                      "结果": f"输出 {len(out)} 行"})
        trace.append({"步骤": "异常隔离", "内容": "转换失败的行隔离并记录", "结果": f"隔离 {len(anomalies)} 条"})
        return RunResult(self.key, self.name, out, anomalies, trace, {"columns": cols})


def _safe_eval(expr: str, row, safe_cols: dict, extra: dict | None = None):
    """在受限命名空间中求值，变量名可以是源表列名，也可注入已算出的目标列值。"""
    env = {"abs": abs, "round": round, "min": min, "max": max, "int": int, "float": float,
           "len": len, "str": str}
    env.update({c: row[c] for c in safe_cols})
    if extra:
        env.update(extra)
    return eval(compile(expr, "<expr>", "eval"), {"__builtins__": {}}, env)  # noqa: S307


# ------------------------------------------------------------------ 技能四：自由模式（任意表格 × 自然语言）

class FreeformSkill(Skill):
    """不依赖预置技能：大模型现场把指令规划成目标列定义，规则引擎确定性执行。"""

    key = "freeform"
    name = "自由模式（任意表格 × 自然语言）"
    desc = ("不依赖预置模板：大模型阅读你的指令与表结构，现场规划目标列、计算表达式与跨表关联，"
            "再由确定性规则引擎逐行执行，需要已配置大模型。支持上传多张表做多源聚合。")

    sources = {}
    target_columns = []

    def run(self, ctx: dict) -> RunResult:
        from .llm import llm_plan_freeform
        from .parser import norm_id

        trace, anomalies = [], []
        _seed_parse_anomalies(ctx, anomalies)
        all_tables: list[RawTable] = [t for t in (ctx.get("tables") or {}).values()]
        if not all_tables:
            raise ValueError("请先上传至少一张源表")
        chat = ctx.get("llm")
        instruction = (ctx.get("condition_text") or "").strip()
        if not instruction:
            raise ValueError("自由模式需要一句自然语言指令，描述想要输出的列与筛选条件。")
        if not chat:
            raise ValueError("自由模式需要大模型支持：请到「大模型设置」页配置 OpenAI 兼容接口"
                             "（当前部署已含反代网关，绑定后即可用），或改用预置技能。")

        # ---- 采样：每张表的列名 + 类型 + 前 3 行非空数据，供模型理解表结构
        tables_meta = []
        for t in all_tables:
            col_types = []
            for c in t.columns:
                dt = str(t.df[c].dtype)
                dt = "number" if ("int" in dt or "float" in dt) else \
                     ("date" if "datetime" in dt else "text")
                col_types.append({"name": str(c), "dtype": dt})
            sample_rows = []
            for _, r in t.df.head(6).iterrows():
                rec = {str(k): (None if v is None or (isinstance(v, float) and math.isnan(v))
                                else (str(v) if not isinstance(v, (int, float)) else v))
                       for k, v in r.items()}
                if any(v not in (None, "") for v in rec.values()):
                    sample_rows.append(rec)
                if len(sample_rows) >= 3:
                    break
            tables_meta.append({"name": t.name, "columns": col_types, "samples": sample_rows})

        multi = len(all_tables) > 1
        trace.append({"步骤": "自由模式规划",
                      "内容": f"指令：{instruction}；上传 {len(all_tables)} 张表："
                              + "、".join(f"「{t.name}」{len(t.columns)}列" for t in all_tables),
                      "结果": "调用大模型解析指令与表结构…"})

        plan = llm_plan_freeform(chat, instruction, tables_meta)
        import os as _os, sys as _sys
        if _os.environ.get("FREEFORM_DEBUG"):
            print("[freeform-plan] " + json.dumps(plan, ensure_ascii=False)[:1500],
                  file=_sys.stderr)  # 诊断开关：FREEFORM_DEBUG=1 时观察模型真实规划
        spec = plan.get("target_columns") or []
        if not spec:
            raise ValueError("大模型未能从指令中规划出目标列。请把指令写得更明确"
                             "（要哪些列、怎么算、筛哪些行），或改用预置技能。")

        # ---- 跨表关联（多表时）：模型给出 join/union 计划，引擎确定性执行
        if multi:
            work_df, merge_desc = self._merge_tables(all_tables, plan.get("joins") or [])
            trace.append({"步骤": "跨表关联（多源聚合）", "内容": merge_desc,
                          "结果": f"合并后 {len(work_df)} 行 / {len(work_df.columns)} 列"})
            t = RawTable(name="跨表合并结果", path=None, df=work_df,
                         columns=list(work_df.columns), header_row=0,
                         raw_rows=len(work_df))
        else:
            t = all_tables[0]

        # 后续管线只看合并后的单表，避免污染外层 ctx
        ctx = dict(ctx)
        ctx["tables"] = {"source": t}

        df_cols = {str(c) for c in t.columns}
        labels: set[str] = set()
        for item in spec:
            lab = str(item.get("label") or "").strip()
            if not lab:
                raise ValueError(f"模型规划的目标列缺少列名：{item}")
            if lab in labels:
                raise ValueError(f"模型规划出了重复列「{lab}」，请换一种指令描述。")
            labels.add(lab)
            src = str(item.get("source") or "").strip()
            expr = str(item.get("expr") or "").strip()
            dtype = item.get("dtype") or "text"
            if dtype not in ("text", "number", "date"):
                dtype = "text"
            item["label"], item["source"], item["expr"], item["dtype"] = lab, src, expr, dtype
            if not expr and (not src or src not in df_cols):
                raise ValueError(f"目标列「{lab}」的来源列「{src}」在源表中不存在，且无计算表达式。")
            # 模型常把固定常量写成裸值（expr="是"），求值时会 NameError。
            # 确定性兜底：不含运算符/括号/函数调用、不是源列名、也不是数字的短表达式 → 按字符串字面量处理
            if expr and not src and not re.search(r"[+\-*/()\[\]\s:\"']", expr) \
                    and expr not in df_cols and not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", expr):
                item["expr"] = f'"{expr}"'

        # ---- 结构化筛选：field ∈ 源表列名 -> 映射前筛；field ∈ 目标列名 -> 映射后筛（支持计算列）
        op_names = {"eq", "ne", "contains", "gt", "ge", "lt", "le"}
        pre_conds, post_conds = [], []
        for flt in plan.get("filters") or []:
            if not isinstance(flt, dict):
                continue
            fld = str(flt.get("field") or "").strip()
            op = str(flt.get("op") or "").strip()
            if op not in op_names:
                raise ValueError(f"筛选条件操作符不合法：{flt}（允许 {sorted(op_names)}）")
            if fld in df_cols:
                pre_conds.append({"field": fld, "op": op, "value": flt.get("value")})
            elif fld in labels:
                post_conds.append({"field": fld, "op": op, "value": flt.get("value")})
            else:
                raise ValueError(f"筛选字段「{fld}」既不在源表列中也不是输出列，无法执行：{flt}")

        ctx["params"] = dict(ctx.get("params") or {})
        ctx["params"]["target_spec"] = spec
        # 前置筛选直接注入；condition_text 置空，防止通用映射回退到对全指令做关键词解析
        ctx["conditions"] = pre_conds
        ctx["condition_text"] = ""

        flt_desc = describe_conditions(pre_conds + post_conds)
        trace.append({"步骤": "执行计划生成",
                      "内容": "模型规划：" + json.dumps(
                          [{"列": s["label"],
                            "来源": s["expr"] or s["source"],
                            "类型": s["dtype"]} for s in spec], ensure_ascii=False),
                      "结果": f"筛选条件：{flt_desc}；已交由规则引擎执行"})

        # ---- 复用通用映射的确定性执行（类型清洗 / 表达式 / 异常隔离全部一致）
        res = GenericMapSkill().run(ctx)

        # ---- 分组汇总：group_by + measures，确定性 pandas groupby（多源聚合的统计半边）
        agg = plan.get("aggregate") or {}
        gb = [str(g).strip() for g in (agg.get("group_by") or []) if str(g).strip()]
        measures = agg.get("measures") or []
        if gb and measures:
            # 宽松列名映射：模型给出的分组/聚合列可能用近似名（如「门店名称」vs 目标列「门店」），
            # 双向包含唯一命中时自动对齐，避免因命名差异导致聚合失败。
            out_cols = list(res.output.columns)

            def _fuzzy(name: str) -> str | None:
                name = str(name).strip()
                if name in out_cols:
                    return name
                cands = [c for c in out_cols
                         if name and str(c) and (name in str(c) or str(c) in name)]
                return cands[0] if len(cands) == 1 else None

            gb = [(_fuzzy(g) or g) for g in gb]
            missing = [g for g in gb if g not in out_cols]
            if missing:
                raise ValueError(f"分组列 {missing} 不在输出列中，无法聚合（请用目标列名）")
            agg_map = {}
            for m in measures:
                if not isinstance(m, dict):
                    continue
                lab = _fuzzy(str(m.get("label") or "").strip()) or str(m.get("label") or "").strip()
                op = str(m.get("op") or "sum").strip().lower()
                if op not in ("sum", "mean", "max", "min", "count"):
                    raise ValueError(f"聚合操作符不合法：{op}（允许 sum/mean/max/min/count）")
                src = _fuzzy(str(m.get("source") or lab).strip()) or str(m.get("source") or lab).strip()
                if lab not in out_cols or src not in out_cols:
                    raise ValueError(f"聚合列「{lab}」/「{src}」不在输出列中（请用目标列名）")
                agg_map[lab] = (src, op)
            if agg_map:
                before = len(res.output)
                res.output = res.output.groupby(gb, as_index=False, dropna=False).agg(**agg_map)
                # 聚合后重算表达式列：如「周转天数=月末库存/销售额*30」依赖聚合后的目标列值，
                # groupby 只保留 group_by+measures 列，其他表达式列需在聚合结果上重新求值。
                for item in spec:
                    if not str(item.get("expr") or "").strip():
                        continue
                    lab = str(item.get("label") or "").strip()
                    if lab in agg_map or lab in gb or lab not in [str(s.get("label") or "").strip() for s in spec]:
                        continue
                    try:
                        res.output[lab] = res.output.apply(
                            lambda row: _safe_eval(item["expr"], row,
                                                   {c: i for i, c in enumerate(res.output.columns)}),
                            axis=1)
                    except Exception:
                        pass  # 表达式与聚合结果不兼容时保留原状（聚合列缺失即视为不适用）
                res.trace.append({"步骤": "分组汇总",
                                  "内容": f"按 {gb} 分组：" +
                                          "、".join(f"{k}={v[1]}({v[0]})" for k, v in agg_map.items()),
                                  "结果": f"{before} 行 -> {len(res.output)} 组"})

        # ---- 后置筛选：对着输出列（含计算列/聚合列）再筛一次，行级确定性比较
        if post_conds:
            before = len(res.output)
            mask = build_mask(res.output, post_conds)
            res.output = res.output[mask].reset_index(drop=True)
            res.trace.append({"步骤": "计算列筛选",
                              "内容": describe_conditions(post_conds),
                              "结果": f"{before} 行 -> 保留 {len(res.output)} 行"})

        res.skill = self.key
        res.skill_name = self.name
        res.trace = trace + res.trace
        res.detail = {"columns": res.detail.get("columns"), "plan": spec}
        return res

    @staticmethod
    def _merge_tables(tables: list[RawTable], joins: list[dict]) -> tuple[pd.DataFrame, str]:
        """按模型规划的 joins 顺序执行确定性合并。

        keyed 关联（merge）模式下所有列名统一加前缀「表名_」（合法 Python 标识符，
        表达式可直接引用）；关联键在合并前做 norm_id 归一（1001.0 -> "1001"），
        解决人员标识格式差异。全部为 union（同构纵向合并）时保留原始列名。
        """
        from .parser import norm_id

        by_name = {t.name: t for t in tables}
        if not joins:
            raise ValueError("检测到多张表，但未获得跨表关联计划。请在指令中说明两表如何关联"
                             "（例如『用工号关联』），或只上传一张表。")

        def norm_key(s: pd.Series) -> pd.Series:
            return s.map(lambda v: norm_id(v))

        # 全 union：同构表纵向拼接，列名保持原样
        if all(str(j.get("how") or "").strip().lower() == "union" for j in joins):
            ordered: list[RawTable] = []
            for j in joins:
                for key in ("left", "right"):
                    nm = str(j.get(key) or "").strip()
                    if nm in by_name and by_name[nm] not in ordered:
                        ordered.append(by_name[nm])
            for t in tables:          # 兜底：模型漏写的表也并进去
                if t not in ordered:
                    ordered.append(t)
            work = pd.concat([t.df for t in ordered], ignore_index=True, sort=False)
            desc = f"{'、'.join(t.name for t in ordered)} 纵向合并（union，{len(work)} 行）"
            return work.reset_index(drop=True), desc

        # keyed 关联模式：加前缀后逐步合并
        prefixed = {t.name: t.df.add_prefix(f"{t.name}_") for t in tables}
        work: pd.DataFrame | None = None
        work_name = ""
        steps: list[str] = []
        for idx, j in enumerate(joins):
            if not isinstance(j, dict):
                raise ValueError(f"关联计划格式不合法：{j}")
            right_name = str(j.get("right") or "").strip()
            if right_name not in by_name:
                raise ValueError(f"关联计划引用了不存在的表「{right_name}」")
            how = str(j.get("how") or "inner").strip().lower()
            if idx == 0:
                left_name = str(j.get("left") or "").strip()
                if left_name not in by_name:
                    raise ValueError(f"关联计划引用了不存在的表「{left_name}」")
                work = prefixed[left_name].copy()
                work_name = left_name
            right = prefixed[right_name].copy()
            if how == "union":
                work = pd.concat([work, right], ignore_index=True, sort=False)
                steps.append(f"{work_name} ∪ {right_name}（纵向合并，{len(work)} 行）")
            else:
                if how not in ("inner", "left", "right", "outer"):
                    how = "inner"
                # 复合关联键支持：LLM 可能给出「表_列1,表_列2」（逗号分隔），逐段校验
                def _split_keys(raw: str) -> list[str]:
                    out: list[str] = []
                    for seg in str(raw).replace("，", ",").replace(";", ",").replace("；", ",").split(","):
                        seg = seg.strip()
                        if seg:
                            out.append(seg)
                    return out
                lks = _split_keys(j.get("left_key") or "")
                rks = _split_keys(j.get("right_key") or "")
                if len(lks) != len(rks):
                    raise ValueError(f"关联键数量不匹配：左 {lks} vs 右 {rks}")
                if not lks:
                    raise ValueError("关联计划缺少关联键（left_key/right_key）")
                for k in lks:
                    if k not in work.columns:
                        raise ValueError(f"关联键「{k}」在当前合并结果中不存在（多表模式列名格式：表名_列名）")
                for k in rks:
                    if k not in right.columns:
                        raise ValueError(f"关联键「{k}」在表「{right_name}」中不存在（应为 表名_列名 格式）")
                before = len(work)
                for k in lks:
                    work[k] = norm_key(work[k])
                for k in rks:
                    right[k] = norm_key(right[k])
                work = work.merge(right, left_on=lks, right_on=rks, how=how)
                steps.append(f"{work_name} 与 {right_name} 按 {'+'.join(lks)}={'+'.join(rks)} 做 {how} 关联"
                             f"（{before} 行 → {len(work)} 行）")
            work_name = f"{work_name}+{right_name}"
        return work.reset_index(drop=True), "；".join(steps)


SKILL_LIST: list[Skill] = [LeaveSettlementSkill(), OvertimeImportSkill(),
                           GenericMapSkill(), FreeformSkill()]
SKILLS: dict[str, Skill] = {s.key: s for s in SKILL_LIST}


def get_skill(key: str) -> Skill:
    return SKILLS[key]
