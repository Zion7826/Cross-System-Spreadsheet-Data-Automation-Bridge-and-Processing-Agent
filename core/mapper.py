# -*- coding: utf-8 -*-
"""
字段映射：源表列 -> 标准字段 / 目标模板列。

三级策略：
1. 语义词典自动匹配（离线、确定性）
2. 大模型兜底（可选，处理未知结构报表）
3. 人工在 UI 上直接改写（永远可覆盖）
"""
from __future__ import annotations

import json
from typing import Callable

import pandas as pd

from .fields import CANONICAL_FIELDS, field_label, match_field, normalize_header


def auto_map(columns: list[str], wanted: list[str], threshold: float = 62.0,
             llm: Callable | None = None) -> dict[str, dict]:
    """为每个目标标准字段挑选源列。

    返回 {field_key: {"source": 列名|None, "score": 分, "method": 自动/LLM/未匹配}}
    """
    result: dict[str, dict] = {}
    used: set[str] = set()
    # 先算全部分数，按分数从高到低分配，避免两字段抢同一列
    pairs = []
    for key in wanted:
        for col in columns:
            pairs.append((match_field(col, key), key, col))
    pairs.sort(reverse=True)

    for score, key, col in pairs:
        if key in result:
            continue
        if col in used:
            continue
        if score >= threshold:
            result[key] = {"source": col, "score": round(score, 1), "method": "语义词典"}
            used.add(col)

    for key in wanted:
        result.setdefault(key, {"source": None, "score": 0.0, "method": "未匹配"})

    # 未匹配的字段交给大模型（若可用）
    missing = [k for k, v in result.items() if v["source"] is None]
    if missing and llm is not None:
        try:
            hint = llm(
                "你是表格字段映射助手。以下是源表格的列名，以及需要映射到的标准字段。"
                "只输出 JSON，键为标准字段 key，值为最匹配的源列名，无法确定则为 null。\n"
                f"源列名: {json.dumps(columns, ensure_ascii=False)}\n"
                f"标准字段: {json.dumps({k: field_label(k) for k in missing}, ensure_ascii=False)}"
            )
            data = json.loads(hint)
            for k, col in data.items():
                if k in result and col in columns and col not in used:
                    result[k] = {"source": col, "score": 85.0, "method": "大模型"}
                    used.add(col)
        except Exception:
            pass
    return result


def apply_mapping(df: pd.DataFrame, mapping: dict[str, dict]) -> pd.DataFrame:
    """按映射把源表重命名为标准字段名。"""
    out = pd.DataFrame(index=df.index)
    for key, info in mapping.items():
        src = info.get("source") if isinstance(info, dict) else info
        if src and src in df.columns:
            out[key] = df[src]
        else:
            out[key] = None
    return out


def merge_mapping(base: dict[str, dict], override: dict[str, str]) -> dict[str, dict]:
    """用人工选择覆盖自动映射。"""
    merged = {k: dict(v) for k, v in base.items()}
    for k, src in override.items():
        if k in merged:
            merged[k] = {"source": src or None, "score": 100.0,
                         "method": "人工指定" if src else "忽略"}
    return merged


def mapping_report(mapping: dict[str, dict]) -> list[dict]:
    rows = []
    for key, info in mapping.items():
        rows.append({
            "目标字段": field_label(key),
            "字段key": key,
            "源列": info.get("source") or "—",
            "匹配度": info.get("score", 0),
            "匹配方式": info.get("method", "—"),
        })
    return rows


def all_canonical_keys() -> list[str]:
    return list(CANONICAL_FIELDS.keys())
