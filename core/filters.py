# -*- coding: utf-8 -*-
"""
自然语言筛选条件解析：把
  "把生产部 2026年1月到3月 加班时长大于等于2小时的记录..."
转成结构化条件列表，再编译成 pandas 布尔掩码。

解析策略（一句话可同时命中多个条件）：
1. 时间段：1月到3月 / 2026-01-01 至 2026-03-31
2. 数值比较：加班时长大于等于2小时（操作符按长度优先匹配，避免"大于等于"被读成"大于"）
3. 显式等值：部门=生产部 / 假别 是 年假 / 姓名 包含 张
4. 值提示：直接在指令里出现的数据值（部门名、姓名、工号），来自源表实际取值
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

OPS = {
    ">=": "ge", "≥": "ge", "大于等于": "ge", "不少于": "ge", "不小于": "ge", "至少": "ge",
    "<=": "le", "≤": "le", "小于等于": "le", "不大于": "le", "不超过": "le", "最多": "le",
    ">": "gt", "大于": "gt", "超过": "gt", "高于": "gt",
    "<": "lt", "小于": "lt", "低于": "lt", "不足": "lt",
    "=": "eq", "==": "eq", "等于": "eq", "是": "eq", "为": "eq",
    "!=": "ne", "<>": "ne", "不等于": "ne", "不是": "ne",
    "包含": "contains", "含有": "contains", "属于": "contains",
}
_SORTED_OPS = sorted(OPS.items(), key=lambda kv: -len(kv[0]))

FIELD_ALIASES = {
    "部门": "dept", "所属部门": "dept", "科室": "dept", "单位": "dept",
    "姓名": "name", "员工": "name", "人员": "name",
    "工号": "emp_id", "员工编号": "emp_id",
    "日期": "date", "加班日期": "date", "请假日期": "date", "时间": "date",
    "开始日期": "start_date", "结束日期": "end_date",
    "天数": "days", "请假天数": "days", "休假天数": "days",
    "时长": "hours", "小时": "hours", "小时数": "hours", "加班时长": "hours", "工时": "hours",
    "假别": "leave_type", "假期类型": "leave_type", "类型": "leave_type",
    "余额": "balance", "年假余额": "balance", "剩余年假": "balance",
}

# 可做"值提示"的字段：指令里直接出现数据值就过滤（假别不做值提示，避免"转成事假"这类描述被误当筛选）
VALUE_HINT_FIELDS = {"dept", "name", "emp_id"}

_TEXT_ALIASES = {a: k for a, k in FIELD_ALIASES.items()
                 if k in ("dept", "name", "emp_id", "leave_type")}
_NUMERIC_ALIASES = {a: k for a, k in FIELD_ALIASES.items()
                    if k in ("days", "hours", "balance")}

_RANGE_RE = re.compile(
    r"(?:(\d{4})\s*[年\-/\.])?\s*(\d{1,2})\s*月?\s*(?:到|至|~|—|-)\s*(?:(\d{4})\s*[年\-/\.])?\s*(\d{1,2})\s*月?")
_DATE_RE = re.compile(r"(?:(\d{4})\s*[年\-/\.])\s*(\d{1,2})\s*[月\-/\.]\s*(\d{1,2})\s*日?")


def parse_conditions(text: str, value_hints: dict | None = None) -> list[dict]:
    if not text or not text.strip():
        return []
    text = text.strip()
    conds: list[dict] = []

    # 1) 时间段
    if re.search(r"到|至|~|—", text):
        m = _RANGE_RE.search(text)
        if m and int(m.group(2)) <= 12 and int(m.group(4)) <= 12:
            y1 = int(m.group(1)) if m.group(1) else datetime.now().year
            m1, m2 = int(m.group(2)), int(m.group(4))
            y2 = int(m.group(3)) if m.group(3) else y1
            start = datetime(y1, m1, 1)
            end = datetime(y2 + (1 if m2 == 12 else 0), (m2 % 12) + 1, 1) - pd.Timedelta(days=1)
            conds.append({"field": "date", "op": "between", "value": [start, end],
                          "raw": m.group(0)})

    # 1b) 单月：2026年2月 / 今年3月（区间已命中时跳过）
    if not any(c["field"] == "date" for c in conds):
        m = re.search(r"(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月", text)
        if m and int(m.group(2)) <= 12:
            y = int(m.group(1)) if m.group(1) else datetime.now().year
            mi = int(m.group(2))
            start = datetime(y, mi, 1)
            end = datetime(y + (1 if mi == 12 else 0), (mi % 12) + 1, 1) - pd.Timedelta(days=1)
            conds.append({"field": "date", "op": "between", "value": [start, end],
                          "raw": m.group(0)})

    # 2) 数值比较
    for alias, key in _NUMERIC_ALIASES.items():
        i = text.find(alias)
        if i == -1:
            continue
        after = text[i + len(alias): i + len(alias) + 14]
        best = None
        for op_txt, op in _SORTED_OPS:
            if op not in ("gt", "ge", "lt", "le", "eq", "ne"):
                continue
            j = after.find(op_txt)
            if j == -1:
                continue
            if best is None or j < best[0] or (j == best[0] and len(op_txt) > len(best[1])):
                best = (j, op_txt, op)
        if best is None:
            continue
        vm = re.search(r"-?\d+(?:\.\d+)?", after[best[0] + len(best[1]):])
        if vm:
            conds.append({"field": key, "op": best[2], "value": float(vm.group()), "raw": alias + after})

    # 3) 显式等值 / 包含（按子句拆分）
    for part in re.split(r"[;；,，、]|并且|且|同时", text):
        part = part.strip()
        if not part:
            continue
        for alias, key in _TEXT_ALIASES.items():
            if alias not in part:
                continue
            rest = part.split(alias, 1)[1]
            m = re.match(r"\s*[:=]|^\s*(等于|是|为|包含|含有|属于|不是|不等于)", rest)
            if not m:
                continue
            op = "contains" if re.search(r"包含|含有|属于", m.group(0)) else \
                ("ne" if re.search(r"不是|不等于", m.group(0)) else "eq")
            val = rest[m.end():].strip(" :：=。，,")
            val = re.split(r"的|，|,|；|;|\s", val)[0] if val else ""
            val = _unwrap_quotes(val)
            # 元语言黑名单：LLM 指令里的结构性描述（如「以工号为关联键」）
            # 不是筛选值，误入条件会把整表过滤成 0 行
            if val and val not in _META_VALUES:
                conds.append({"field": key, "op": op, "value": val, "raw": part})
            break

    # 4) 值提示：指令中直接出现的数据值
    for field_key, values in (value_hints or {}).items():
        if field_key not in VALUE_HINT_FIELDS:
            continue
        for v in values:
            if v and len(v) >= 2 and v in text and not any(
                    c["field"] == field_key and str(c["value"]) == v for c in conds):
                conds.append({"field": field_key, "op": "eq", "value": v, "raw": f"指令含数据值「{v}」"})
                break

    return conds


_QUOTES = "「」『』【】\"'“”‘’`"

# 指令元语言：这些"值"描述的是处理方式而不是数据内容，不能当筛选条件
_META_VALUES = {
    "关联键", "关联", "键", "匹配键", "主键", "外键", "唯一键",
    "维度", "指标", "汇总", "合计", "明细", "升序", "降序",
    "空", "非空", "全部", "所有", "条件", "字段", "列",
}


def _unwrap_quotes(val: str) -> str:
    """剥离条件值两侧成对的包裹引号（「生产部」→ 生产部）。只剥一层，避免误伤正文。"""
    val = val.strip()
    while len(val) >= 2 and val[0] in _QUOTES and val[-1] in _QUOTES:
        val = val[1:-1].strip()
    return val


def _resolve_column(df: pd.DataFrame, field_key: str):
    """把条件里的规范字段名解析为源表实际列名（支持异构表头）。"""
    if field_key in df.columns:
        return field_key
    from .fields import match_field
    best, best_score = None, 62.0
    for c in df.columns:
        s = match_field(c, field_key)
        if s > best_score:
            best, best_score = c, s
    return best


def build_mask(df: pd.DataFrame, conds: list[dict]) -> pd.Series:
    """把条件列表编译成布尔掩码（无条件则全 True）。

    语义约定：对 between / 数值比较，取值为空（NaT / NaN）的行**不会被排除**——
    无法证明其不符合条件的行交给后续规则校验去隔离并记录，避免脏数据被静默丢弃。
    """
    mask = pd.Series([True] * len(df), index=df.index)
    if df.empty:
        return mask
    for c in conds:
        f, op, val = c.get("field"), c.get("op"), c.get("value")
        col_name = _resolve_column(df, f)
        if col_name is None:
            continue
        col = df[col_name]
        try:
            if op == "between":
                s, e = pd.Timestamp(val[0]), pd.Timestamp(val[1])
                c2 = pd.to_datetime(col, errors="coerce")
                m = ((c2 >= s) & (c2 <= e)) | c2.isna()
            elif op == "contains":
                m = col.astype(str).str.contains(str(val), case=False, na=False)
            elif op == "eq":
                if pd.api.types.is_numeric_dtype(col):
                    try:
                        m = col == float(val)
                    except (TypeError, ValueError):
                        m = col.astype(str).str.strip() == str(val).strip()
                else:
                    m = col.astype(str).str.strip() == str(val).strip()
            elif op == "ne":
                m = col.astype(str).str.strip() != str(val).strip()
            else:
                num = pd.to_numeric(col, errors="coerce")
                ops = {"gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
                       "lt": lambda a, b: a < b, "le": lambda a, b: a <= b}
                if num.notna().any():
                    m = ops[op](num, float(val))
                    m = m | num.isna()
                else:
                    # 日期列比较：转数值全失败时按日期比较
                    dtv = pd.to_datetime(col, errors="coerce")
                    ref = pd.Timestamp(val)
                    m = ops[op](dtv, ref)
                    m = m | dtv.isna()
            mask &= m.fillna(False)
        except Exception:
            continue
    return mask


def describe_conditions(conds: list[dict]) -> str:
    if not conds:
        return "（无筛选条件，处理全量数据）"
    op_txt = {"eq": "=", "ne": "≠", "gt": ">", "ge": "≥", "lt": "<", "le": "≤",
              "contains": "包含", "between": "介于"}
    out = []
    for c in conds:
        v = c["value"]
        if c["op"] == "between":
            v = f"{v[0]:%Y-%m-%d} ~ {v[1]:%Y-%m-%d}"
        out.append(f"{c['field']} {op_txt.get(c['op'], c['op'])} {v}")
    return " 且 ".join(out)
