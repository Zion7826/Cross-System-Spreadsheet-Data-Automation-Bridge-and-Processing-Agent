# -*- coding: utf-8 -*-
"""
异构表格解析器：脏表 -> 干净 DataFrame + 异常清单。

处理老旧系统导出的典型问题：
- 标题行 / 空行 / 说明行混在表头之前
- 合并单元格造成的空表头
- 日期格式五花八门（2026/1/5、20260105、2026年1月5日、Excel 序列号）
- 数值带单位（"8.5小时"）、千分位、全角数字
- 干扰字符与类型不匹配（隔离而非崩溃）
"""
from __future__ import annotations

import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from .fields import match_field, normalize_header

EXCEL_EPOCH = datetime(1899, 12, 30)

_DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m/%d", "%Y年%m月%d日", "%Y%m%d",
    "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S", "%m/%d/%Y", "%d-%m-%Y", "%Y-%m", "%Y/%m",
    "%Y年%m月", "%y-%m-%d", "%Y.%-m.%-d",
]

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# 严格数字：只认"纯数字（可带千分位/货币符号/百分号/常用单位）"，用于类型推断
_STRICT_NUM_RE = re.compile(r"[+-]?[¥$￥]?\d[\d,，]*(?:\.\d+)?\s*(?:元|万|％|%)?")


@dataclass
class Anomaly:
    """一条被隔离的异常数据。"""
    table: str
    row_no: int            # 原始 Excel 行号（1-based，含表头前的所有行）
    reason: str
    raw: dict


@dataclass
class RawTable:
    name: str
    path: str | None
    df: pd.DataFrame                       # 清洗后的数据
    columns: list[str]                     # 原始表头
    header_row: int                        # 表头所在行索引（0-based，指原始文件内）
    anomalies: list[Anomaly] = field(default_factory=list)
    column_types: dict[str, str] = field(default_factory=dict)
    raw_rows: int = 0
    ocr: bool = False                      # 来源是否为扫描件（图片识别，建议人工复核）

    def summary(self) -> str:
        tag = "，来源：扫描件识别" if self.ocr else ""
        return f"{self.name}：{len(self.df)} 行 / {len(self.df.columns)} 列，异常 {len(self.anomalies)} 条{tag}"


# ---------------------------------------------------------------- 值归一化

def norm_number(v: Any):
    """把任意脏值转成 float，失败返回 None。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "null", "-", "--", "/", "无", "空"}:
        return None
    s = re.sub(r"[,，\s]", "", s)
    # 含连续字母的字符串（SO-1001 / AB12 / USD100）不是数字，防止从编号里抽出 "-1001" 之类的假数值
    if re.search(r"[A-Za-z]{2,}", s):
        return None
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def norm_date(v: Any):
    """把任意脏值转成 datetime，失败返回 None。"""
    if v is None:
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        ts = pd.Timestamp(v)
        return None if pd.isna(ts) else ts.to_pydatetime()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        if 20000 <= f <= 80000:  # Excel 序列号
            return EXCEL_EPOCH + timedelta(days=f)
        return None
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "null", "-", "--", "/", "无", "空"}:
        return None
    s = re.sub(r"\s+", " ", s)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = re.search(r"(\d{4})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    try:
        ts = pd.to_datetime(s, errors="coerce")
        return None if pd.isna(ts) else ts.to_pydatetime()
    except Exception:
        return None


def norm_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none", "null"} else s


def norm_id(v: Any) -> str:
    """关联键归一：数字工号 1001.0 -> "1001"，避免跨表关联时因类型差异对不上。"""
    if isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    return norm_text(v)


# ---------------------------------------------------------------- 表头探测

def _header_score(row: pd.Series) -> float:
    """给候选表头行打分。"""
    vals = [v for v in row.tolist() if norm_text(v)]
    if len(vals) < 2:
        return -1.0
    score = len(vals) * 10.0
    for v in vals:
        s = norm_text(v)
        if norm_number(s) is not None and norm_date(s) is None:
            score -= 8.0          # 表头一般不是纯数字
        if len(s) <= 16:
            score += 3.0
        # 命中标准字段词典是强信号
        best = max(match_field(s, k) for k in ("emp_id", "name", "dept", "date", "days", "hours", "balance", "leave_type"))
        if best >= 78:
            score += 25.0
        elif best >= 62:
            score += 10.0
    # 表头应唯一
    if len(set(vals)) < len(vals):
        score -= 15.0
    return score


def detect_header_row(raw: pd.DataFrame, max_scan: int = 20) -> int:
    best_row, best_score = 0, -1.0
    limit = min(max_scan, len(raw))
    for i in range(limit):
        s = _header_score(raw.iloc[i])
        if s > best_score:
            best_row, best_score = i, s
    return best_row


def _clean_columns(values: list) -> list[str]:
    cols, seen = [], {}
    for i, v in enumerate(values):
        name = norm_text(v)
        if not name:
            name = f"未命名列{i + 1}"
        base, k = name, 2
        while name in seen:
            name = f"{base}_{k}"
            k += 1
        seen[name] = True
        cols.append(name)
    return cols


def _infer_type(series: pd.Series, header: str, max_sample: int = 500) -> str:
    """推断列类型：date / number / text。

    优先尊重字段语义：标准字段里声明为 text 的（工号、姓名、部门、假别…）
    即使内容全是数字也按文本处理，避免工号 E1 / 001 被解析成 1.0 / 1 而丢失精度。
    大表场景只采样前 max_sample 个非空值（类型推断与全量逐格判定在典型数据上一致，
    却把 O(n) 正则扫描降为常数级）。
    """
    sample = []
    for v in series.tolist():
        if norm_text(v):
            sample.append(v)
            if len(sample) >= max_sample:
                break
    if not sample:
        return "text"

    from .fields import field_dtype, suggest_canonical
    key, score = suggest_canonical(header)
    if key and score >= 70 and field_dtype(key) == "text":
        return "text"

    header_norm = normalize_header(header)
    hint_date = any(k in header_norm for k in ("日期", "时间", "date", "time"))
    hint_num = any(k in header_norm for k in ("天数", "小时", "时长", "余额", "数量", "金额", "天数", "day", "hour", "balance"))

    date_ok = sum(1 for v in sample if norm_date(v) is not None)

    def _strict_num(v: Any) -> bool:
        """严格数字判定：原生数值，或"纯数字(可带货币符/单位)"且不含字母前缀的文本。"""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return True
        s = norm_text(v)
        if not s or re.search(r"[A-Za-z]{2,}", s):
            return False
        return _STRICT_NUM_RE.fullmatch(re.sub(r"[,，\s]", "", s)) is not None

    # 类型推断用严格数字判定：像 "SO-1001" 这种"字母+数字"的编号列不会被当成数值列
    num_ok = sum(1 for v in sample if _strict_num(v))
    n = len(sample)

    if hint_date and date_ok / n >= 0.5:
        return "date"
    if hint_num and num_ok / n >= 0.5:
        return "number"
    if date_ok / n >= 0.85 and num_ok / n < 0.95:
        return "date"
    if num_ok / n >= 0.85:
        return "number"
    return "text"


# ---------------------------------------------------------------- 向量化转换（大表性能关键路径）

_TEXT_EMPTY = {"", "nan", "none", "null"}


def _empty_row_mask(body: pd.DataFrame) -> pd.Series:
    """向量化判定"整行均为空值"（与逐格 norm_text(v)=="" 的语义一致）。"""
    mask = None
    for c in body.columns:
        col = body[c]
        e = col.isna() | col.astype(str).str.strip().str.lower().isin(_TEXT_EMPTY)
        mask = e if mask is None else (mask & e)
    if mask is None:
        return pd.Series(False, index=body.index)
    return mask


def _convert_number_col(body: pd.DataFrame, c: str, tname: str, h: int,
                        anomalies: list[Anomaly]) -> pd.Series:
    """数值列类型化：向量化快路径 + 脏值逐格回退（语义与逐格 norm_number 一致）。"""
    col = body[c]
    if pd.api.types.is_bool_dtype(col):
        # 布尔列保持逐格语义：norm_number(True) -> None -> 全部异常
        out = {}
        for idx, v in col.items():
            nv = norm_number(v)
            if nv is None and norm_text(v):
                anomalies.append(Anomaly(tname, h + 2 + idx, "数值格式无法解析", {c: str(v)}))
            out[idx] = nv
        return pd.Series(out, index=col.index, dtype=object)
    if pd.api.types.is_numeric_dtype(col):
        return col.astype(float)          # Excel 原生数值：直接转，NaN 即空值

    num = pd.to_numeric(col, errors="coerce")
    fail = col.notna() & num.isna()       # to_numeric 处理不了的脏值（千分位/单位/全角…）
    if not fail.any():
        return num.astype(float)
    # 脏值只占少数：仅对失败子集逐格回退，异常语义完全一致
    sub = col[fail]
    fixed = {idx: norm_number(v) for idx, v in sub.items()}
    for idx, nv in fixed.items():
        if nv is None:
            v = sub[idx]
            anomalies.append(Anomaly(tname, h + 2 + idx, "数值格式无法解析", {c: str(v)}))
            num[idx] = None
        else:
            num[idx] = nv
    return num.astype(float)


def _convert_date_col(body: pd.DataFrame, c: str, tname: str, h: int,
                      anomalies: list[Anomaly]) -> pd.Series:
    """日期列类型化：原生 datetime 快路径 + 逐格式向量化解析 + 残余脏值逐格回退。

    逐格式向量化与 norm_date 的 strptime 循环顺序一致，结果语义不变。
    """
    col = body[c]
    if pd.api.types.is_datetime64_any_dtype(col):
        return col
    # 原生 datetime 对象（Excel 真日期单元格）快路径
    is_dt = col.map(lambda v: isinstance(v, (datetime, pd.Timestamp)))
    out = pd.Series(pd.NaT, index=col.index, dtype="datetime64[ns]")
    if is_dt.all():
        out[:] = pd.to_datetime(col)
        return out
    if is_dt.any():
        out[is_dt] = pd.to_datetime(col[is_dt])

    rest = col.index[~is_dt]
    if len(rest):
        s = col[rest].astype(str).str.strip().str.replace(r"\s+", " ", regex=True)
        s = s.where(col[rest].notna() & s.ne("") & ~s.str.lower().isin(_TEXT_EMPTY))
        remaining = s.notna()
        # 1) Excel 序列号
        num = pd.to_numeric(s, errors="coerce")
        serial = num.where((num >= 20000) & (num <= 80000))
        from_serial = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
        ok_serial = serial.notna()
        if ok_serial.any():
            from_serial[ok_serial] = EXCEL_EPOCH + pd.to_timedelta(serial[ok_serial], unit="D")
        pending = remaining & from_serial.isna()
        # 2) 显式格式逐个尝试（顺序与 norm_date 一致）
        parsed = from_serial.copy()
        pend_idx = pending[pending].index
        pend_str = s[pend_idx]
        for fmt in _DATE_FORMATS:
            if not len(pend_idx):
                break
            got = pd.to_datetime(pend_str, format=fmt, errors="coerce")
            hit = got.notna()
            if hit.any():
                parsed.loc[pend_idx[hit]] = got[hit]
                pend_idx = pend_idx[~hit]
                pend_str = s[pend_idx]
        # 3) 宽松正则（2026年1月5日 之类）
        if len(pend_idx):
            ext = pend_str.str.extract(r"(\d{4})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})")
            ok = ext[0].notna()
            for i in ext.index[ok]:
                try:
                    parsed.loc[i] = datetime(int(ext.loc[i, 0]), int(ext.loc[i, 1]), int(ext.loc[i, 2]))
                except ValueError:
                    pass
            pend_idx = pend_idx[~ok]
        # 4) pandas 兜底（与 norm_date 最后一步一致）
        if len(pend_idx):
            got = pd.to_datetime(s[pend_idx], errors="coerce")
            parsed.loc[got.index] = got
        out[rest] = parsed

    # 异常：非空但解析失败
    bad = col.notna() & pd.Series(out.isna(), index=col.index) & ~is_dt
    for idx in bad[bad].index:
        v = col[idx]
        anomalies.append(Anomaly(tname, h + 2 + idx, "日期格式无法解析", {c: str(v)}))
    return out


# ---------------------------------------------------------------- 文件形态识别 / 解密

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}


def _is_encrypted_stream(head: bytes) -> bool:
    return head[:8] == _OLE_MAGIC


def _decrypt_stream(raw_bytes: bytes, password: str) -> io.BytesIO:
    """用 msoffcrypto 解密受密码保护的 xlsx/xls，返回解密后的字节流。"""
    try:
        import msoffcrypto
    except ImportError:
        raise ValueError(
            "该文件已加密。解密需要 msoffcrypto 组件：请在部署环境执行 "
            "pip install msoffcrypto-tool 后重试，或上传未加密版本。")
    buf = io.BytesIO(raw_bytes)
    try:
        of = msoffcrypto.OfficeFile(buf)
        of.load_key(password=password)
        out = io.BytesIO()
        of.decrypt(out)
    except Exception as ex:
        raise ValueError(f"解密失败：密码错误或文件格式不受支持（{ex}）")
    out.seek(0)
    return out


def load_image_table(path_or_bytes, name: str | None = None, llm=None,
                     image_prompt_hint: str = "") -> RawTable:
    """扫描件 / 截图 → 大模型视觉识别 → RawTable。

    识别结果整表标记 ocr=True，前端与异常日志都会提示"建议人工复核"，
    保证不把识别结果静默当成权威数据。
    """
    if llm is None:
        raise ValueError(
            "扫描件识别需要大模型视觉能力：请先在「大模型设置」页配置支持视觉的模型"
            "（如 glm-5.3-flash），或把图片内容手工整理成 Excel / CSV 后上传。")
    import base64

    if isinstance(path_or_bytes, (bytes, bytearray)):
        raw = bytes(path_or_bytes)
        tname = name or "扫描件"
    else:
        tname = name or path_or_file_name(path_or_bytes)
        with open(path_or_bytes, "rb") as fh:
            raw = fh.read()
    b64 = base64.b64encode(raw).decode()
    prompt = (
        "这是企业系统导出报表的照片/截图。请把图中表格完整转成 JSON 数组，"
        "第一层对象为表格的列名（用图中表头原文），逐行输出所有数据行，"
        "数值列输出数字，日期保持原文。不要遗漏行，也不要编造图中不存在的行列。"
        "只输出 JSON 数组，不要任何解释。" + (f"\n补充说明：{image_prompt_hint}" if image_prompt_hint else "")
    )
    try:
        txt = llm(prompt, image_b64=b64)          # chat 闭包支持 image_b64 扩展参数
    except TypeError:
        raise ValueError("当前大模型客户端不支持图片输入，请改用支持视觉的模型或接口。")
    m = re.search(r"\[.*\]", txt or "", re.S)
    if not m:
        raise ValueError("大模型未能从图片中识别出表格，请换一张更清晰的截图，或手工整理后上传。")
    try:
        records = json.loads(m.group())
    except json.JSONDecodeError:
        raise ValueError("大模型识别结果无法解析为表格，请重试或换更清晰的图片。")
    if not isinstance(records, list) or not records:
        raise ValueError("大模型识别结果为空，请换一张更清晰的截图。")

    cols: list[str] = []
    for r in records:
        for k in r.keys():
            if str(k) not in cols:
                cols.append(str(k))
    if not cols:
        raise ValueError("图片中未识别出有效数据列。")
    body = pd.DataFrame(records, columns=cols)
    body = body.map(lambda v: "" if v is None else v)
    # 大模型返回的 records 已带列名，直接作为表头，不再做表头行探测
    columns = _clean_columns(cols)
    frame = body.copy()
    frame.columns = columns
    frame = frame.reset_index(drop=True)
    anomalies: list[Anomaly] = []
    empty = _empty_row_mask(frame)
    frame = frame[~empty].reset_index(drop=True)
    types = {}
    for c in columns:
        t = _infer_type(frame[c], c)
        types[c] = t
        if t == "number":
            frame[c] = _convert_number_col(frame, c, tname, 0, anomalies)
        elif t == "date":
            frame[c] = _convert_date_col(frame, c, tname, 0, anomalies)
        else:
            frame[c] = frame[c].map(norm_text)
    tbl = RawTable(tname, None, frame, columns, 0, anomalies, types, len(records) + 1, ocr=True)
    # 整表标记"扫描件来源"，进入异常日志提示人工复核
    anomalies.append(Anomaly(tname, 1,
                             f"扫描件识别：本表来自图片识别（{len(frame)} 行 × {len(columns)} 列），数值建议人工复核后使用", {}))
    return tbl


def path_or_file_name(p) -> str:
    return str(p).replace("\\", "/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------- 载入

def load_table(path_or_file, name: str | None = None, sheet: int | str = 0,
               password: str | None = None, llm=None) -> RawTable:
    """读取 xlsx / xls / csv / 加密表格 / 扫描件图片，返回清洗后的 RawTable。"""
    if isinstance(path_or_file, (bytes, bytearray)):
        return _load_from_bytes(bytes(path_or_file), name, password, llm)
    if isinstance(path_or_file, str):
        path = path_or_file
        tname = name or path.replace("\\", "/").rsplit("/", 1)[-1]
        with open(path, "rb") as fh:
            head = fh.read(8)
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        if ext in IMAGE_EXTS:
            return load_image_table(path, name, llm=llm)
        if _is_encrypted_stream(head):
            if not password:
                raise ValueError(
                    f"「{tname}」是加密文件：请在上传区域的「文件密码」框填写打开密码后重新上传，"
                    "或提供未加密版本。")
            data = _decrypt_stream(open(path, "rb").read(), password)
            return _load_from_bytes(data.getvalue(), name, password, llm)
        raw = pd.read_excel(path, header=None, dtype=object) if not path.lower().endswith(".csv") \
            else pd.read_csv(path, header=None, dtype=object)
    else:
        path = None
        tname = name or getattr(path_or_file, "name", "表格")
        head = path_or_file.read(8)
        path_or_file.seek(0)
        ext = ("." + tname.rsplit(".", 1)[-1].lower()) if "." in str(tname) else ""
        if ext in IMAGE_EXTS:
            return load_image_table(path_or_file.read(), name, llm=llm)
        if _is_encrypted_stream(head):
            if not password:
                raise ValueError(
                    f"「{tname}」是加密文件：请填写打开密码后重新上传，或提供未加密版本。")
            data = _decrypt_stream(path_or_file.read(), password)
            return _load_from_bytes(data.getvalue(), name, password, llm)
        if tname.lower().endswith(".csv"):
            raw = pd.read_csv(path_or_file, header=None, dtype=object)
        else:
            raw = pd.read_excel(path_or_file, header=None, dtype=object)

    return _build_table(raw, tname, path)


def _load_from_bytes(data: bytes, name: str | None, password: str | None, llm) -> RawTable:
    """从内存字节载入（解密后 / 上传流）。图片走 OCR，其余按表格解析。"""
    tname = name or "表格"
    if data[:8] == _OLE_MAGIC and password:
        data = _decrypt_stream(data, password).getvalue()
    ext = ("." + tname.rsplit(".", 1)[-1].lower()) if "." in str(tname) else ""
    if ext in IMAGE_EXTS:
        return load_image_table(data, name, llm=llm)
    if tname.lower().endswith(".csv"):
        raw = pd.read_csv(io.BytesIO(data), header=None, dtype=object)
    else:
        raw = pd.read_excel(io.BytesIO(data), header=None, dtype=object)
    return _build_table(raw, tname, None)


def _build_table(raw: pd.DataFrame, tname: str, path: str | None) -> RawTable:
    """原始矩阵 -> 探测表头 -> 空行隔离 -> 逐列类型化（大表走向量化路径）。"""
    # 只删全空列；全空行保留到后面统一"异常隔离"，而不是静默丢弃
    raw = raw.dropna(axis=1, how="all").reset_index(drop=True)
    raw_rows = len(raw)
    if raw.empty:
        return RawTable(tname, path, pd.DataFrame(), [], 0, [], {}, 0)

    h = detect_header_row(raw)
    columns = _clean_columns(raw.iloc[h].tolist())
    body = raw.iloc[h + 1:].copy()
    body.columns = columns
    body = body.reset_index(drop=True)

    anomalies: list[Anomaly] = []
    # 全空行 -> 异常隔离（向量化）
    empty = _empty_row_mask(body)
    for idx in empty[empty].index:
        anomalies.append(Anomaly(tname, h + 2 + idx, "空行/无有效数据", {}))
    body = body[~empty].reset_index(drop=True)

    # 逐列类型化
    from .fields import field_dtype, suggest_canonical
    types = {}
    for c in columns:
        t = _infer_type(body[c], c)
        types[c] = t
        key_force_text = None
        k, sc = suggest_canonical(c)
        if k and sc >= 70 and field_dtype(k) == "text":
            key_force_text = k
        if t == "date":
            body[c] = _convert_date_col(body, c, tname, h, anomalies)
        elif t == "number":
            body[c] = _convert_number_col(body, c, tname, h, anomalies)
        elif key_force_text and key_force_text == "emp_id":
            body[c] = body[c].map(norm_id)
        else:
            body[c] = body[c].map(norm_text)

    return RawTable(tname, path, body, columns, h, anomalies, types, raw_rows)


def to_records(table: RawTable) -> list[dict]:
    return table.df.to_dict(orient="records")
