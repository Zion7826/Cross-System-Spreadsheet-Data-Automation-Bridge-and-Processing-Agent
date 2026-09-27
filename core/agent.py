# -*- coding: utf-8 -*-
"""
智能体编排层：自然语言指令 -> 意图 -> 计划 -> 执行 -> 自检 -> 交付。

职责边界：
- 意图识别与表角色判定：规则优先，大模型可选增强
- 计算与判定：全部下放给规则引擎（确定性）
- 每一步都写进 trace，前端可视化"智能体做了什么"
"""
from __future__ import annotations

from typing import Any, Callable

from .exporter import export_result
from .fields import match_field
from .filters import parse_conditions
from .parser import RawTable, load_table
from .rules import SKILLS, SKILL_LIST, RunResult, Skill

# 意图关键词 -> 技能
INTENT_RULES: list[tuple[str, list[str]]] = [
    ("leave_settlement", ["年假", "年休假", "请假", "假期", "假别", "事假", "抵扣", "余额", "休假"]),
    ("overtime_import", ["加班", "ot", "工时", "调休", "值班"]),
    ("generic_map", ["通用映射", "字段映射", "映射导出"]),
    ("freeform", ["自由模式"]),
]

# 表角色判定：角色 -> (关键词字段, 权重)
ROLE_HINTS: dict[str, list[tuple[str, float]]] = {
    "leave": [("leave_type", 3.0), ("days", 2.0), ("start_date", 1.5), ("end_date", 1.5), ("emp_id", 0.5)],
    "balance": [("balance", 4.0), ("emp_id", 0.5), ("name", 0.3)],
    "overtime": [("hours", 3.0), ("date", 1.0), ("dept", 1.0), ("emp_id", 0.5), ("start_date", 1.0)],
}


def classify_intent(instruction: str, llm: Callable | None = None) -> str:
    text = (instruction or "").lower()

    # 大模型优先：语义区分"转成HR导入模板"（预置技能）与"统计劳务费"（自由模式），
    # 避免单个泛化关键词（如"工时"）把任意统计类指令锁死到预置技能
    if llm and text.strip():
        from .llm import llm_understand_instruction
        plan = llm_understand_instruction(
            llm, instruction, [{"key": s.key, "name": s.name, "desc": s.desc} for s in SKILL_LIST])
        if plan.get("skill") in SKILLS:
            return plan["skill"]

    # 关键词回退（未配置大模型或模型识别失败时）
    best, best_score = "", 0
    for key, kws in INTENT_RULES:
        score = sum(1 for k in kws if k in text)
        if score > best_score:
            best, best_score = key, score
    if best:
        return best
    if not text.strip():
        return "generic_map"          # 无指令：仅做字段整理场景
    # 关键词与模型均未命中：交给自由模式现场规划
    return "freeform"


def guess_role(columns: list[str]) -> tuple[str, float]:
    """根据表头推断该表在业务中的角色。"""
    best, best_score = "", 0.0
    for role, hints in ROLE_HINTS.items():
        score = 0.0
        for field_key, w in hints:
            m = max((match_field(c, field_key) for c in columns), default=0.0)
            if m >= 62:
                score += w * (m / 100.0)
        if score > best_score:
            best, best_score = role, score
    return best, round(best_score, 2)


class TableBridgeAgent:
    """跨系统表格数据自动化桥接与处理智能体。"""

    def __init__(self, llm: Callable | None = None):
        self.llm = llm

    # ---------------------------------------------------------------- 载入
    def load(self, files: list[tuple[str, Any]]) -> list[RawTable]:
        """files: [(显示名, 路径或文件对象)]"""
        tables = []
        for name, f in files:
            tables.append(load_table(f, name))
        return tables

    def assign_roles(self, tables: list[RawTable], forced: dict[str, str] | None = None):
        """把上传的表分配到业务角色，返回 {role: RawTable} 与分配说明。"""
        forced = forced or {}
        used: set[int] = set()
        assigned: dict[str, RawTable] = {}
        notes = []

        for role, tname in forced.items():
            for i, t in enumerate(tables):
                if t.name == tname and i not in used:
                    assigned[role] = t
                    used.add(i)
                    break

        for i, t in enumerate(tables):
            if i in used:
                continue
            role, score = guess_role(t.columns)
            if role and role not in assigned:
                assigned[role] = t
                used.add(i)
                notes.append(f"「{t.name}」识别为 {role}（置信度 {score}）")
            else:
                notes.append(f"「{t.name}」未能确定角色（最高置信 {score}），请在界面上手动指定")
        return assigned, notes

    # ---------------------------------------------------------------- 执行
    def build_value_hints(self, tables: list[RawTable], limit: int = 300) -> dict[str, list[str]]:
        """从源表提取"值提示"：指令里直接出现的部门名/姓名/工号会被识别为筛选条件。"""
        from .fields import field_dtype, suggest_canonical
        hints: dict[str, set[str]] = {}
        for t in tables:
            for col in t.columns:
                key, score = suggest_canonical(col)
                if not key or score < 70:
                    continue
                dtype = field_dtype(key)
                if key not in ("dept", "name", "emp_id") or dtype != "text":
                    continue
                vals = [str(v).strip() for v in t.df[col].tolist()
                        if v is not None and str(v).strip()]
                hints.setdefault(key, set()).update(v for v in vals if 1 < len(v) <= 20)
        return {k: sorted(v)[:limit] for k, v in hints.items()}

    def run(self, instruction: str, tables: list[RawTable],
            skill_key: str | None = None,
            role_override: dict[str, str] | None = None,
            mapping_override: dict[str, dict] | None = None,
            params: dict | None = None) -> dict:
        trace: list[dict] = []
        skill_key = skill_key or classify_intent(instruction, self.llm)
        skill: Skill = SKILLS[skill_key]

        trace.append({"步骤": "意图解析",
                      "内容": f"用户指令：{instruction or '（未填写，按上传表格自动判定）'}",
                      "结果": f"选用技能：{skill.name}"})

        assigned, notes = self.assign_roles(tables, role_override)

        # 通用映射 / 自由模式：把唯一的表当作 source；自由模式支持多表（多源聚合）
        if skill_key in ("generic_map", "freeform"):
            if skill_key == "freeform":
                assigned = {("source" if i == 0 else f"table{i + 1}"): t
                            for i, t in enumerate(tables)}
                if not assigned:
                    raise ValueError("请先上传至少一张源表")
                if len(tables) > 1:
                    notes = [f"自由模式：{len(tables)} 张表全部挂载，由执行计划决定关联方式"]
            elif "source" not in assigned:
                src = tables[0] if tables else None
                if src is None:
                    raise ValueError("请先上传至少一张源表")
                assigned["source"] = src
        else:
            missing = [r for r in skill.sources if r not in assigned]
            if missing:
                need = "、".join(skill.sources[m][0] for m in missing)
                raise ValueError(f"缺少必需的源表：{need}。请上传对应导出文件，或在界面上手动指定表角色。")

        trace.append({"步骤": "源表角色判定",
                      "内容": "；".join(notes) or "已按人工指定分配",
                      "结果": f"已分配 {len(assigned)} 张表：{', '.join(f'{k}←{v.name}' for k, v in assigned.items())}"})

        ctx = {
            "tables": assigned,
            "llm": self.llm,
            "condition_text": instruction,
            "conditions": parse_conditions(instruction, self.build_value_hints(tables)),
            "mapping_override": mapping_override or {},
            "params": params or {},
            "skill_key": skill_key,
        }
        result: RunResult = skill.run(ctx)
        result.trace = trace + result.trace
        # 同一行可能同时命中解析阶段与规则阶段的异常，按（源表，行号）去重，保留先发生的一条
        seen, deduped = set(), []
        for a in result.anomalies:
            k = (a.get("源表"), a.get("行号"))
            if k in seen:
                continue
            seen.add(k)
            deduped.append(a)
        result.anomalies = deduped
        return {"result": result, "skill": skill, "assigned": assigned}

    # ---------------------------------------------------------------- 导出
    def export(self, run_out: dict, out_path: str, date_mode: str = "date") -> str:
        result: RunResult = run_out["result"]
        skill: Skill = run_out["skill"]
        cols = skill.target_columns or None
        return export_result(result, out_path, cols, date_mode=date_mode)
