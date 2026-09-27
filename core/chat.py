# -*- coding: utf-8 -*-
"""
多轮对话编排层（交互式对话）。

职责：把"带上下文的连续对话"翻译成一次可执行的智能体任务，或一句澄清回复。

设计要点：
- 会话状态（已上传表、上一次执行结果）由服务层维护，本层只做"当轮决策"。
- 模型输出三种动作：
    reply  —— 解答疑问 / 追问澄清（例如"要按哪个部门筛？"）
    run    —— 组装一句【完整、自包含】的指令交给确定性引擎执行（含在上轮基础上的修改）
- 指令必须自包含：模型负责把历史语境合并进本轮指令，引擎无状态，可复现。
- 数值计算依旧全部由规则引擎完成，对话层不参与任何数值判定。
"""
from __future__ import annotations

import json
from typing import Callable

from .llm import json_llm


def llm_chat_turn(chat: Callable, message: str, history: list[dict],
                  tables_meta: list[dict], last_run: dict | None) -> dict:
    """一轮对话决策。

    history:  [{"role": "user"|"assistant", "content": str}]，不含本轮
    tables_meta: [{"name", "columns": [{"name","dtype"}], "samples": [..]}]
    last_run: {"instruction", "skill", "columns", "rows", "summary"} 或 None
    返回 {"action": "reply"|"run", "reply": str, "instruction": str}
    """
    if not chat:
        return {"action": "reply",
                "reply": "对话模式需要大模型支持：请先在「大模型设置」页配置 OpenAI 兼容接口。",
                "instruction": ""}

    history_text = "\n".join(f"{'用户' if h['role'] == 'user' else '助手'}：{h['content']}"
                             for h in history[-8:]) or "（这是第一轮对话）"
    last_text = "（还没有执行过任务）"
    if last_run:
        last_text = (f"上一次任务指令：{last_run.get('instruction') or '（自动识别）'}\n"
                     f"使用技能：{last_run.get('skill')}\n"
                     f"输出：{last_run.get('rows', 0)} 行，列：{last_run.get('columns')}\n"
                     f"结果概要：{last_run.get('summary')}")
    tables_text = json.dumps(tables_meta, ensure_ascii=False) if tables_meta else "（当前没有已上传的表格）"

    prompt = (
        "你是企业表格数据处理智能体的对话调度器。当前会话状态如下。\n\n"
        f"【对话历史】\n{history_text}\n\n"
        f"【已挂载的数据表】\n{tables_text}\n\n"
        f"【上一次执行结果】\n{last_text}\n\n"
        f"【用户本轮消息】\n{message}\n\n"
        "请决定本轮动作，只输出 JSON：\n"
        "{\"action\": \"reply\" 或 \"run\", "
        "\"reply\": \"回复用户的文字（reply 时必填；run 时给一句简短确认，如：好的，已按新条件重新执行）\", "
        "\"instruction\": \"完整执行指令（run 时必填）\"}\n\n"
        "决策规则：\n"
        "1. 用户在提问、闲聊、要求解释、信息不足以执行（缺表/缺条件/语义含糊）→ action=reply，"
        "在 reply 里回答或追问（追问要具体，一次只问一个关键问题）。\n"
        "2. 用户要处理/查询/统计/转换数据，或基于上一次结果做修改（如『只要生产部的』『再加一列合计』"
        "『把筛选条件去掉』）→ action=run。\n"
        "3. instruction 必须是【完整、自包含】的一句话指令：把对话历史与上一次任务的语境合并进去，"
        "让一个没看过对话的执行引擎也能照着执行。例如上一轮是『统计每个部门销售总额』，本轮说『只要华东的』，"
        "instruction 应写『统计每个部门的销售总额，只要华东区域的数据』。\n"
        "4. 不要在 instruction 里编造数据表中不存在的列名；描述用业务语言即可，字段对齐由引擎负责。\n"
        "5. 用户只是打招呼或表达感谢 → action=reply 简短回应。"
    )
    fn = json_llm(chat)
    out: dict = {}
    # 空决策（{}或字段全空）属偶发异常，重试最多 2 次
    for attempt in range(3):
        out = fn(prompt) or {}
        if any(str(out.get(k) or "").strip() for k in ("action", "reply", "instruction")):
            break
        if attempt == 2:
            out = {}
    action = str(out.get("action") or "").strip().lower()
    if action not in ("reply", "run"):
        # 模型输出不合预期：能凑合执行就执行，否则原样回复
        ins = str(out.get("instruction") or "").strip()
        if ins:
            action = "run"
        else:
            action = "reply"
    reply = str(out.get("reply") or "").strip()
    instruction = str(out.get("instruction") or "").strip()
    if action == "run" and not instruction:
        instruction = message          # 兜底：拿原话当指令
        reply = reply or "好的，按你的描述执行。"
    if action == "reply" and not reply:
        reply = "收到。请补充你想对当前数据表做什么处理，我来执行。"
    return {"action": action, "reply": reply, "instruction": instruction}
