# -*- coding: utf-8 -*-
"""表格智能体核心包：解析 / 映射 / 规则 / 导出 / 编排。"""
from .fields import CANONICAL_FIELDS, normalize_header, match_field, field_label
from .parser import load_table, RawTable
from .mapper import auto_map, apply_mapping
from .filters import parse_conditions, build_mask, describe_conditions
from .rules import SKILLS, get_skill, RunResult
from .exporter import export_result, write_target_template
from .agent import TableBridgeAgent

__all__ = [
    "CANONICAL_FIELDS", "normalize_header", "match_field", "field_label",
    "load_table", "RawTable", "auto_map", "apply_mapping",
    "parse_conditions", "build_mask", "describe_conditions",
    "SKILLS", "get_skill", "RunResult", "export_result", "write_target_template",
    "TableBridgeAgent",
]
