# -*- coding: utf-8 -*-
"""
跨系统表格数据自动化桥接与处理智能体 · Web 服务端

启动：python server.py   （默认 127.0.0.1:8620）
接口：FastAPI；前端：static/ 下的单页应用
大模型：OpenAI 兼容格式，在「设置」页配置（base_url / api_key / model），
        配置落盘 llm_config.json，仅用于意图识别与未知表头映射兜底，
        数值计算与逻辑判定始终由确定性规则引擎完成。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR.parent            # agent_app/
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(BASE_DIR))

from core.agent import TableBridgeAgent, guess_role               # noqa: E402
from core.chat import llm_chat_turn                               # noqa: E402
from core.exporter import export_result                           # noqa: E402
from core.fields import field_label                               # noqa: E402
from core.llm import build_llm                                    # noqa: E402
from core.parser import IMAGE_EXTS, RawTable, load_table          # noqa: E402
from core.rules import SKILL_LIST, SKILLS, RunResult, Skill       # noqa: E402
import verify as verify_mod                                       # noqa: E402

DEMO_DIR = APP_DIR / "data" / "demo"
OUT_DIR = BASE_DIR / "outputs"
LLM_CFG = BASE_DIR / "llm_config.json"
MAX_PREVIEW_ROWS = 500
MAX_HISTORY = 40

app = FastAPI(title="表格桥接智能体")

LOCK = threading.Lock()
STATE: dict[str, Any] = {"files": {}, "runs": {}, "chats": {}}


# ------------------------------------------------------------------ 工具

def _load_llm_cfg() -> dict:
    if LLM_CFG.exists():
        try:
            return json.loads(LLM_CFG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_llm_cfg(cfg: dict):
    LLM_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_llm():
    cfg = _load_llm_cfg()
    if not cfg.get("base_url") or not cfg.get("api_key"):
        return None
    return build_llm(cfg["base_url"], cfg["api_key"], cfg.get("model") or "gpt-4o-mini",
                     timeout=90)


def _unique_name(name: str) -> str:
    names = {t.name for t in STATE["files"].values()}
    if name not in names:
        return name
    i = 2
    while f"{name} ({i})" in names:
        i += 1
    return f"{name} ({i})"


def _register_table(t: RawTable, fid: str | None = None) -> dict:
    fid = fid or uuid.uuid4().hex[:10]
    STATE["files"][fid] = t
    return _file_info(t, fid)


def _jsonify(v: Any) -> Any:
    """把引擎输出转成 JSON 安全结构。"""
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d") if isinstance(v, date) and not isinstance(v, datetime) \
            else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, float):
        return None if v != v else round(v, 6)
    if isinstance(v, dict):
        return {str(k): _jsonify(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonify(x) for x in v]
    return v


def _run_payload(run_id: str, run_out: dict) -> dict:
    res: RunResult = run_out["result"]
    skill: Skill = run_out["skill"]

    mapping = {}
    m_detail = res.detail.get("mapping") or {}
    for role, tbl in run_out["assigned"].items():
        m = m_detail.get(role) or {}
        mapping[role] = {
            "table": tbl.name,
            "columns": list(tbl.columns),
            "fields": [{"key": k, "label": field_label(k),
                        "source": (v.get("source") if isinstance(v, dict) else v) or "",
                        "score": (v.get("score", 0) if isinstance(v, dict) else 0),
                        "method": (v.get("method", "") if isinstance(v, dict) else "")}
                       for k, v in m.items()],
        }

    out = res.output
    rows = [_jsonify(r) for r in out.head(MAX_PREVIEW_ROWS).to_dict(orient="records")]

    detail = {}
    if res.detail.get("settlement"):
        detail["settlement"] = [_jsonify(r) for r in res.detail["settlement"]]
        detail["balance_left"] = _jsonify(res.detail.get("balance_left") or {})

    return {
        "run_id": run_id,
        "skill_key": skill.key,
        "skill_name": skill.name,
        "trace": res.trace,
        "mapping": mapping,
        "columns": list(out.columns),
        "rows": rows,
        "total_rows": len(out),
        "truncated": len(out) > MAX_PREVIEW_ROWS,
        "anomalies": [_jsonify(a) for a in res.anomalies],
        "detail": detail,
    }


# ------------------------------------------------------------------ API

@app.get("/api/skills")
def api_skills():
    return {"skills": [{"key": s.key, "name": s.name, "desc": s.desc,
                        "sources": [{"role": r, "label": info[0]}
                                    for r, info in s.sources.items()]}
                       for s in SKILL_LIST]}


@app.get("/api/settings")
def api_get_settings():
    cfg = _load_llm_cfg()
    return {"base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
            "api_key_set": bool(cfg.get("api_key")), "llm_ready": get_llm() is not None}


@app.post("/api/settings")
def api_set_settings(payload: dict):
    cfg = _load_llm_cfg()
    if "base_url" in payload:
        cfg["base_url"] = (payload["base_url"] or "").strip()
    if "model" in payload:
        cfg["model"] = (payload["model"] or "").strip()
    # key 留空 = 保留原 key（避免回显泄露）
    if payload.get("api_key"):
        cfg["api_key"] = payload["api_key"].strip()
    _save_llm_cfg(cfg)
    return api_get_settings()


@app.post("/api/settings/test")
def api_test_settings(payload: dict | None = None):
    cfg = _load_llm_cfg()
    if payload:
        base = (payload.get("base_url") or cfg.get("base_url", "")).strip()
        key = (payload.get("api_key") or cfg.get("api_key", "")).strip()
        model = (payload.get("model") or cfg.get("model") or "gpt-4o-mini").strip()
    else:
        base, key, model = cfg.get("base_url", ""), cfg.get("api_key", ""), cfg.get("model", "")
    if not base or not key:
        return {"ok": False, "message": "Base URL 与 API Key 不能为空"}
    llm = build_llm(base, key, model)
    if llm is None:
        return {"ok": False, "message": "无法初始化客户端（缺少 requests？）"}
    try:
        reply = llm("只回复两个字：正常")
        return {"ok": True, "message": f"连接成功，模型回复：{reply[:40]}"}
    except Exception as ex:
        return {"ok": False, "message": f"连接失败：{ex}"}


@app.post("/api/files/upload")
async def api_upload(files: list[UploadFile] = File(...), password: str = Form("")):
    with LOCK:
        llm = get_llm()
        items = []
        for f in files:
            fname = f.filename or "table.xlsx"
            suffix = Path(fname).suffix or ".xlsx"
            tmp = OUT_DIR / "_tmp" / f"{uuid.uuid4().hex}{suffix}"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(await f.read())
            try:
                t = load_table(str(tmp), _unique_name(Path(fname).stem or "表格"),
                               password=password or None, llm=llm)
            except Exception as ex:
                hint = ""
                if suffix.lower() in IMAGE_EXTS and "大模型" in str(ex):
                    hint = "（扫描件识别需先在「大模型设置」配置支持视觉的模型）"
                raise HTTPException(400, f"解析 {fname} 失败：{ex}{hint}")
            items.append(_register_table(t))
        return {"files": items}


def _file_info(t: RawTable, fid: str) -> dict:
    role, score = guess_role(t.columns)
    return {
        "id": fid, "name": t.name, "rows": len(t.df), "cols": len(t.columns),
        "columns": t.columns, "anomalies": len(t.anomalies),
        "ocr": bool(t.ocr),
        "detected_role": role or "", "role_score": score,
        "summary": t.summary(),
    }


@app.post("/api/demo")
def api_demo():
    with LOCK:
        STATE["files"].clear()
        STATE["runs"].clear()
        items = []
        for name in ["ERP_加班记录导出.xlsx", "OA_请假单导出.xlsx", "HR_年假余额导出.xlsx"]:
            p = DEMO_DIR / name
            if p.exists():
                t = load_table(str(p), name.replace(".xlsx", ""))
                items.append(_register_table(t))
        return {"files": items}


@app.get("/api/demo/file")
def api_demo_file(name: str):
    p = (DEMO_DIR / name).resolve()
    if not str(p).startswith(str(DEMO_DIR.resolve())) or not p.exists():
        raise HTTPException(404, "演示文件不存在")
    return FileResponse(str(p), filename=name)


@app.post("/api/files/clear")
def api_clear():
    with LOCK:
        STATE["files"].clear()
        STATE["runs"].clear()
    return {"ok": True}


@app.get("/api/files")
def api_list_files():
    with LOCK:
        return {"files": [_file_info(t, fid) for fid, t in STATE["files"].items()]}


@app.post("/api/files/remove")
def api_remove_file(payload: dict):
    with LOCK:
        STATE["files"].pop(payload.get("id") or "", None)
        return {"files": [_file_info(t, fid) for fid, t in STATE["files"].items()]}


@app.post("/api/files/preview")
def api_preview(payload: dict):
    fid = payload.get("id") or ""
    with LOCK:
        t = STATE["files"].get(fid)
    if not t:
        raise HTTPException(404, "源表不存在或已失效")
    n = int(payload.get("rows") or 10)
    head = t.df.head(n)
    cols = list(head.columns)
    rows = [_jsonify(r) for r in head.to_dict(orient="records")]
    return {"columns": cols, "rows": rows, "total": len(t.df)}


@app.post("/api/run")
def api_run(payload: dict):
    instruction: str = payload.get("instruction") or ""
    file_ids: list[str] = payload.get("file_ids") or []
    skill_key: str = payload.get("skill_key") or ""
    role_override_ids: dict = payload.get("role_override") or {}
    mapping_override: dict = payload.get("mapping_override") or {}
    params: dict = payload.get("params") or {}

    with LOCK:
        missing = [fid for fid in file_ids if fid not in STATE["files"]]
        if missing:
            raise HTTPException(400, "部分源表已失效，请重新上传")
        tables = [STATE["files"][fid] for fid in file_ids]
        if not tables:
            raise HTTPException(400, "请先上传源表或载入演示数据")
        role_override = {role: STATE["files"][fid].name
                         for role, fid in role_override_ids.items() if fid in STATE["files"]}
        try:
            agent = TableBridgeAgent(llm=get_llm())
            run_out = agent.run(instruction, tables, skill_key=skill_key or None,
                                role_override=role_override,
                                mapping_override=mapping_override,
                                params=params)
        except Exception as ex:
            raise HTTPException(400, str(ex))
        run_id = uuid.uuid4().hex[:10]
        STATE["runs"][run_id] = {"run_out": run_out, "instruction": instruction,
                                 "file_ids": file_ids, "role_override": role_override_ids}
        return _run_payload(run_id, run_out)


@app.post("/api/export")
def api_export(payload: dict):
    run_id = payload.get("run_id") or ""
    date_mode = payload.get("date_mode") or "date"
    with LOCK:
        run = STATE["runs"].get(run_id)
        if not run:
            raise HTTPException(404, "运行结果已过期，请重新执行")
        run_out = run["run_out"]
        skill: Skill = run_out["skill"]
        stamp = datetime.now().strftime("%m%d_%H%M")
        safe_name = re.sub(r'[\\/:*?"<>|]', "-", skill.name)
        fname = f"{safe_name}_{stamp}.xlsx"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / fname
        TableBridgeAgent(llm=get_llm()).export(run_out, str(out_path), date_mode=date_mode)
    return FileResponse(str(out_path), filename=fname,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/verify")
def api_verify():
    rows = verify_mod.run_all()
    return {"checks": rows}


# ------------------------------------------------------------------ 多轮对话

def _tables_meta(tables: list[RawTable]) -> list[dict]:
    """给对话层看的表结构摘要（表头 + 类型 + 样本行）。"""
    import math as _math
    meta = []
    for t in tables:
        cols = []
        for c in t.columns:
            dt = str(t.df[c].dtype)
            cols.append({"name": str(c),
                         "dtype": "number" if ("int" in dt or "float" in dt)
                         else ("date" if "datetime" in dt else "text")})
        samples = []
        for _, r in t.df.head(3).iterrows():
            rec = {str(k): ("" if v is None or (isinstance(v, float) and _math.isnan(v)) else str(v))
                   for k, v in r.items()}
            if any(v != "" for v in rec.values()):
                samples.append(rec)
        meta.append({"name": t.name, "columns": cols, "samples": samples})
    return meta


@app.post("/api/chat")
def api_chat(payload: dict):
    message: str = (payload.get("message") or "").strip()
    sid: str = payload.get("session_id") or uuid.uuid4().hex[:12]
    if not message:
        raise HTTPException(400, "消息不能为空")
    llm = get_llm()
    if not llm:
        raise HTTPException(400, "对话模式需要大模型支持：请先在「大模型设置」页配置并测试连接")

    with LOCK:
        tables = list(STATE["files"].values())
        sess = STATE["chats"].setdefault(sid, {"history": [], "last_run": None})

    decision = llm_chat_turn(llm, message, sess["history"], _tables_meta(tables),
                             sess.get("last_run"))
    reply = decision["reply"]
    run_payload = None

    if decision["action"] == "run":
        if not tables:
            reply = "当前还没有数据表：请先在工作台上传源表或载入演示数据（对话模式与工作台共用数据源），我再执行。"
        else:
            try:
                with LOCK:
                    agent = TableBridgeAgent(llm=llm)
                    run_out = agent.run(decision["instruction"], tables)
                    run_id = uuid.uuid4().hex[:10]
                    STATE["runs"][run_id] = {
                        "run_out": run_out, "instruction": decision["instruction"],
                        "file_ids": list(STATE["files"].keys()), "role_override": {}}
                run_payload = _run_payload(run_id, run_out)
                res: RunResult = run_out["result"]
                sess["last_run"] = {
                    "instruction": decision["instruction"],
                    "skill": run_payload["skill_name"],
                    "columns": run_payload["columns"],
                    "rows": run_payload["total_rows"],
                    "summary": (f"输出 {run_payload['total_rows']} 行，"
                                f"异常 {len(run_payload['anomalies'])} 条"),
                }
                reply = decision["reply"] or \
                    f"已执行（{run_payload['skill_name']}），输出 {run_payload['total_rows']} 行。"
            except Exception as ex:
                reply = f"执行失败：{ex}。可以换个说法重试，或先补齐上面提到的条件。"

    sess["history"].append({"role": "user", "content": message})
    sess["history"].append({"role": "assistant", "content": reply})
    if len(sess["history"]) > MAX_HISTORY:
        sess["history"] = sess["history"][-MAX_HISTORY + 10:]

    return {"session_id": sid, "action": decision["action"], "reply": reply,
            "instruction": decision.get("instruction") or "", "run": run_payload}


@app.post("/api/chat/reset")
def api_chat_reset(payload: dict):
    sid = payload.get("session_id") or ""
    with LOCK:
        STATE["chats"].pop(sid, None)
    return {"ok": True}


# 静态前端
app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8620, log_level="warning")
