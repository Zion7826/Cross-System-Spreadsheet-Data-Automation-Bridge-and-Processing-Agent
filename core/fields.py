# -*- coding: utf-8 -*-
"""
字段语义词典：把异构表头归一到标准字段。

设计原则：
1. 归一化（全半角、大小写、标点、空白）后匹配，抗格式差异；
2. 精确同义词 > 包含匹配 > 模糊匹配（difflib），阈值可控；
3. 对易混淆字段（天/小时）做互斥惩罚，避免误判——数值正确性优先。
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# 标准字段: key -> (中文标签, 同义词表, 目标数据类型)
CANONICAL_FIELDS: dict[str, tuple[str, list[str], str]] = {
    "emp_id": ("工号", [
        "工号", "员工编号", "员工号", "职工编号", "职工号", "人事编号", "人员编号",
        "员工id", "人员id", "工号no",
        "empid", "emp_id", "employee_id", "employeeid", "empno", "emp_no",
        "employee_no", "staff_id", "staffid", "staff_no", "personnel_no",
    ], "text"),
    "name": ("姓名", [
        "姓名", "员工姓名", "职工姓名", "人员姓名", "名字", "员工名称", "人员名称",
        "name", "employee_name", "emp_name", "staff_name", "fullname", "full_name",
    ], "text"),
    "dept": ("部门", [
        "部门", "所属部门", "部门名称", "所属单位", "单位", "科室", "车间", "组织", "组织架构",
        "dept", "department", "org", "org_name", "unit", "section",
    ], "text"),
    "date": ("日期", [
        "日期", "加班日期", "请假日期", "发生日期", "业务日期", "单据日期", "出勤日期",
        "考勤日期", "制单日期", "时间", "date", "work_date", "ot_date", "leave_date", "biz_date",
    ], "date"),
    "start_date": ("开始日期", [
        "开始日期", "起始日期", "请假开始", "加班开始", "开始时间", "起始时间", "开始",
        "start_date", "begin_date", "start_time", "from_date", "start",
    ], "date"),
    "end_date": ("结束日期", [
        "结束日期", "终止日期", "请假结束", "加班结束", "结束时间", "结束",
        "end_date", "to_date", "end_time", "finish_date", "end",
    ], "date"),
    "days": ("天数", [
        "天数", "请假天数", "休假天数", "总天数", "请假时长", "时长天", "申请天数", "假勤天数",
        "days", "leave_days", "duration_days", "total_days", "day_count",
    ], "number"),
    "hours": ("小时数", [
        "小时数", "加班小时", "加班时长", "加班时数", "工时", "时长", "小时", "工作时长",
        "hours", "ot_hours", "duration_hours", "work_hours", "hour", "duration",
    ], "number"),
    "leave_type": ("假别", [
        "假别", "假期类型", "请假类型", "假种", "假期类别", "类型", "类别",
        "leave_type", "type", "absence_type", "holiday_type", "category",
    ], "text"),
    "balance": ("年假余额", [
        "年假余额", "剩余年假", "可用年假", "年假剩余", "年度剩余", "年假结余", "余额", "结余", "剩余额度",
        "annual_leave_balance", "balance", "remain", "remaining", "quota", "left_days",
    ], "number"),
    "reason": ("事由", [
        "事由", "原因", "备注", "说明", "加班事由", "请假事由", "描述", "摘要",
        "remark", "reason", "comment", "memo", "note", "description", "desc",
    ], "text"),
}

# 互斥惩罚：命中左侧关键词时，对指定字段扣分
_MUTEX_RULES: list[tuple[str, list[str], int]] = [
    ("days", ["小时", "hour", "h)", "(h", "钟点"], -45),
    ("hours", ["天", "day", "d)", "(d"], -35),
    ("date", ["天数", "小时", "余额"], -60),
    ("balance", ["天数", "小时", "日期"], -50),
]

_PUNCT = re.compile(r"[\s\(\)（）\[\]【】\{\}〈〉《》、,，。.·:：;；/\\\-_|*#\"'`~!！?？]+")


def normalize_header(raw) -> str:
    """表头归一化：全角转半角、去标点空白、转小写。"""
    s = "" if raw is None else str(raw)
    s = unicodedata.normalize("NFKC", s)
    s = s.strip().lower()
    s = _PUNCT.sub("", s)
    return s


def _fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() * 100


# 完全同义词索引：归一化表头 -> 所属字段。用于"排他"判定——
# 表头已精确属于某字段时，不允许被包含/模糊匹配到其他字段
# （如 'name' 不应因 'org_name' 的包含关系被误配成 dept）。
_EXACT_INDEX: dict[str, str] = {}


def _build_exact_index():
    if _EXACT_INDEX:
        return
    for key, (label, synonyms, _) in CANONICAL_FIELDS.items():
        for s in [label, *synonyms]:
            ns = normalize_header(s)
            if ns and ns not in _EXACT_INDEX:
                _EXACT_INDEX[ns] = key


def match_field(raw_header, field_key: str) -> float:
    """返回原始表头与某标准字段的匹配分（0-100）。"""
    _build_exact_index()
    label, synonyms, _ = CANONICAL_FIELDS[field_key]
    n = normalize_header(raw_header)
    if not n:
        return 0.0
    owner = _EXACT_INDEX.get(n)
    if owner and owner != field_key:
        # 表头已精确属于另一字段：只给兜底分，禁止语义误抢
        return 30.0
    normed_syns = [normalize_header(s) for s in synonyms]
    normed_syns.append(normalize_header(label))

    best = 0.0
    for syn in normed_syns:
        if not syn:
            continue
        if n == syn:
            best = max(best, 100.0)
        elif syn in n or n in syn:
            # 包含匹配：越短越强，避免"请假开始日期"被误判成"日期"
            shorter, longer = (syn, n) if len(syn) <= len(n) else (n, syn)
            best = max(best, 78.0 + 12.0 * (len(shorter) / max(len(longer), 1)))
    if best < 100.0:
        best = max(best, max(_fuzzy(n, syn) for syn in normed_syns))

    # 互斥惩罚
    for target, kws, penalty in _MUTEX_RULES:
        if target == field_key and any(k in n for k in kws):
            best += penalty
    return max(0.0, min(100.0, best))


def field_label(key: str) -> str:
    return CANONICAL_FIELDS.get(key, (key, [], "text"))[0]


def field_dtype(key: str) -> str:
    return CANONICAL_FIELDS.get(key, (key, [], "text"))[2]


def suggest_canonical(raw_header, threshold: float = 62.0):
    """给单个表头推荐标准字段。"""
    scored = sorted(
        ((match_field(raw_header, k), k) for k in CANONICAL_FIELDS),
        reverse=True,
    )
    if not scored:
        return None, 0.0
    score, key = scored[0]
    return (key if score >= threshold else None), score
