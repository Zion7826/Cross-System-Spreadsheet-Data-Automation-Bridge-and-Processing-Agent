# -*- coding: utf-8 -*-
"""
可选大模型适配层（OpenAI 兼容接口）。

定位：只做"理解与建议"——意图识别、未知表头映射、条件解析兜底。
数值计算与逻辑判定一律走确定性规则引擎，绝不交给模型算数，保证零失真。

未配置 key 时全部返回 None，应用自动降级为纯规则模式（离线可用）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


def build_llm(base_url: str | None = None, api_key: str | None = None,
              model: str | None = None, timeout: int = 30) -> Callable | None:
    """返回一个 chat(prompt)->str 的函数；不可用时返回 None。"""
    base_url = (base_url or os.getenv("LLM_BASE_URL") or "").strip().rstrip("/")
    api_key = (api_key or os.getenv("LLM_API_KEY") or "").strip()
    model = (model or os.getenv("LLM_MODEL") or "gpt-4o-mini").strip()
    if not base_url or not api_key or requests is None:
        return None

    url = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
    # 本机网关/本机模型服务：绕过系统代理（沙箱环境的 HTTP_PROXY 会劫持 localhost 请求）
    _local = any(h in base_url for h in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]"))

    def chat(prompt: str, system: str = "你是严谨的企业数据处理助手，只输出要求的内容。",
             image_b64: str | None = None) -> str:
        """发送对话；传 image_b64 时走 OpenAI 视觉格式（扫描件表格识别）。"""
        content = prompt
        if image_b64:
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"}}]
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": content}],
                   "temperature": 0}
        if _local:
            with requests.Session() as s:
                s.trust_env = False
                r = s.post(url, headers=headers, json=payload, timeout=timeout)
        else:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    return chat


def json_llm(chat: Callable | None):
    """包装成"返回 JSON"的调用：失败自动重试，最终失败返回空 dict。"""
    if chat is None:
        return lambda *a, **k: {}

    def call(prompt: str) -> dict:
        full = prompt + "\n只输出 JSON，不要任何解释和代码块标记。"
        for attempt in range(3):
            try:
                txt = chat(full)
                m = re.search(r"\{.*\}", txt, re.S)
                if m:
                    return json.loads(m.group())
            except Exception:
                pass  # 网关偶发超时/限流，重试
        return {}
    return call


def llm_understand_instruction(chat: Callable | None, instruction: str, skills: list[dict]) -> dict:
    """用大模型把自然语言指令解析成 {skill, conditions, params}；失败返回 {}。"""
    if not chat:
        return {}
    fn = json_llm(chat)
    return fn(
        "把用户的业务指令解析为执行计划，输出 JSON：\n"
        "{\"skill\": 技能key, \"condition_text\": 筛选条件原文, \"params\": {}}\n"
        f"可选技能：{json.dumps(skills, ensure_ascii=False)}\n"
        "技能选择规则：\n"
        "1. 指令给出了明确的目标列清单/目标格式（如\"生成包含A、B、C列的导入文件\"、"
        "\"对齐某模板格式\"），或明说不涉及抵扣换算计算时，一律选 freeform，"
        "由它现场规划目标列与表达式（预置技能的输出是内置演示模板，对不齐自定义格式）。\n"
        "2. 只有指令明确要求【年假抵扣/超出转事假的结算计算】才选 leave_settlement；"
        "明确要求【加班类型判定/时长换算筛选】才选 overtime_import。\n"
        "3. 指令要求按「通用映射页已配置的目标模板」导出时才选 generic_map；"
        "其余开放性统计任务选 freeform。\n"
        f"用户指令：{instruction}"
    )


def llm_plan_freeform(chat: Callable | None, instruction: str,
                      tables: list[dict]) -> dict:
    """自由模式：让大模型把"任意指令 + 任意表（可多表）"规划成可执行的定义。

    tables: [{name, columns: [{name, dtype}], samples: [样本行dict]}]
    返回 {"joins": [...], "target_columns": [...], "filters": [...]}；失败返回 {}。
    """
    if not chat:
        return {}
    fn = json_llm(chat)
    multi = len(tables) > 1
    ref_rule = (
        "（多表模式：若做 keyed 关联（inner/left/right/outer），合并后列名统一为「表名_列名」，"
        "所有 source/field/expr 变量都用这个格式，例如 销售订单表_单价；"
        "若全部为 union 纵向合并（同构表），列名保持原样，直接用原列名引用）"
        if multi else "（单表模式：source/field/expr 变量直接写源表列名）"
    )
    join_rule = (
        "【跨表关联规则】多表时必须输出 joins 数组，按顺序执行合并：\n"
        "{\"joins\": [{\"left\": \"表A名\", \"right\": \"表B名\", "
        "\"left_key\": \"表A_工号\", \"right_key\": \"表B_员工编号\", \"how\": \"inner|left|right|outer\"}}\n"
        "第一条的 left 是起始表；后续条目的 left_key 引用「当前已合并结果中的列名」（表名_列名格式）。\n"
        "只有当两张表结构相同（同列名、纵向追加）时才用 {\"how\": \"union\"}（不需要键）。\n"
        "关联键选择语义上唯一标识同一实体的列（如工号、订单号）；人员标识格式可能不同（数字 vs 文本），引擎会自动归一。\n"
        if multi else "单表时 joins 输出空数组 []。\n"
    )
    prompt = (
        "你是数据转换规划器。用户上传了一张或多张表，并用自然语言描述了想要的结果。\n"
        "你的任务：输出「目标模板列定义」「筛选条件」和（多表时的）「跨表关联计划」，"
        "交给下游确定性引擎逐行执行。只输出 JSON。\n\n"
        "输出格式：\n"
        "{\"joins\": [...],\n"
        " \"target_columns\": [{\"label\": \"目标列名\", \"source\": \"来源列名或空串\", "
        "\"dtype\": \"text|number|date\", \"expr\": \"计算表达式或空串\"}],\n"
        " \"filters\": [{\"field\": \"列名\", \"op\": \"eq|ne|contains|gt|ge|lt|le\", \"value\": 值}],\n"
        " \"aggregate\": {\"group_by\": [\"列名\"], "
        "\"measures\": [{\"label\": \"统计列名\", \"op\": \"sum\", \"source\": \"被统计列名\"}]}}\n\n"
        "目标列规则：\n"
        "1. 目标列必须完整覆盖用户想要输出的每一列，顺序按用户描述或常理排列。\n"
        "2. 直接取源列的：填 source=来源列名，expr 留空；需要计算的：填 expr，用源列名做变量，"
        "只允许 + - * / ( ) 和函数 round(x,n)/abs/min/max/int/float/str。\n"
        "   【表达式惯用法（务必遵守）】\n"
        "   a. 日期列(date类型)是时间戳对象，不能与字符串直接相加；"
        "拼日期+时刻一律写：str(起始日期)[:10] + \" \" + str(起始时间)\n"
        "   b. 条件取值用 Python 三元式：天数*8 if 天数 and float(天数)>0 else (小时 or 0)\n"
        "   c. 混合运算先把值转数字：float(金额)*0.9；文本拼接先把两边都 str()。\n"
        "   d. 固定常量列：expr 直接写 \"是\" 这样的字面量。 \n"
        f"3. 列引用格式 {ref_rule}\n"
        "4. dtype：文本=text，数值=number，日期=date。金额/数量/天数一律 number。\n"
        "5. 用户没提的列不要自行发挥添加；用户明确要的列一个都不能漏。\n\n"
        "筛选规则：\n"
        "6. 把指令里的所有行筛选条件拆进 filters。field 写来源列名；"
        "如果筛选的是计算出来的列（源表不存在），field 写目标列名。\n"
        "7. value：数值直接写数字；文本写原文；日期写 YYYY-MM-DD。日期区间用两条 ge + le 表达。\n"
        "8. 没有任何筛选时 filters 给空数组 []。\n\n"
        "汇总统计规则：\n"
        "9. 如果指令要求按某人/某部门/某月等维度汇总（每组一行）而不是逐行明细，必须输出 aggregate：\n"
        "   group_by 填分维度列在 target_columns 中的「目标列名」；measures 的 op 可选 sum/mean/max/min/count，"
        "label 是输出的统计列名（也必须出现在 target_columns 中），source 填被统计列的目标列名（通常与 label 相同）。\n"
        "   例：target_columns 含 销售区域(source=区域列) 与 销售总额(source=金额列)，则 aggregate="
        "{\"group_by\":[\"销售区域\"],\"measures\":[{\"label\":\"销售总额\",\"op\":\"sum\",\"source\":\"销售总额\"}]}。\n"
        "   例如『统计每个区域的销售总额』→ group_by=[\"销售区域\"], "
        "measures=[{\"label\":\"销售总额\",\"op\":\"sum\",\"source\":\"金额列\"}]。\n"
        "   逐行明细输出（不分组）时 aggregate 的 group_by 给空数组。\n\n"
        + join_rule +
        f"\n各表结构：{json.dumps(tables, ensure_ascii=False)}\n"
        f"用户指令：{instruction}"
    )
    return fn(prompt)
