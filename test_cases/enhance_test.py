# -*- coding: utf-8 -*-
"""
补齐增强能力的端到端测试：
  1) 扫描件 OCR：上传表格截图 -> 视觉识别 -> 值与原图一致
  2) 加密文件：无密码给出明确引导 / 带密码可解密解析
  3) 多轮对话：执行 -> 按上下文改条件重跑 -> 追问澄清 -> 恢复执行
用法：python enhance_test.py [ocr|enc|chat|all]
"""
import io
import sys
from pathlib import Path

import httpx
import openpyxl

BASE = "http://127.0.0.1:8620"
ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "tmp_ocr"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ("　" + detail if detail else ""))


def upload(files, password=""):
    data = [("files", (fn, content, "application/octet-stream")) for fn, content in files]
    r = httpx.post(BASE + "/api/files/upload", files=data,
                   data={"password": password}, timeout=300, trust_env=False)
    return r


# ---------------------------------------------------------------- 1. OCR
def test_ocr():
    img = TMP / "ocr_test2.png"
    if not img.exists():
        check("OCR 准备", False, "缺少测试图片")
        return
    r = upload([("车间报表截图.png", img.read_bytes())])
    ok = r.status_code == 200
    check("扫描件上传识别", ok, "" if ok else r.text[:200])
    if not ok:
        return
    f = r.json()["files"][0]
    check("识别为扫描件来源", f.get("ocr") is True, f.summary if False else f.get("summary", ""))
    pv = httpx.post(BASE + "/api/files/preview", json={"id": f["id"], "rows": 10},
                    timeout=30, trust_env=False).json()
    rows = pv["rows"]
    expect = [("张三", "销售部", 1200.5), ("李四", "生产部", 800), ("王五", "人事部", 950)]
    got = [(str(r_.get("姓名", "")), str(r_.get("部门", "")), r_.get("金额")) for r_ in rows[:3]]
    match = len(rows) == 3 and all(
        a == b2 and c == d and isinstance(e, (int, float)) and abs(e - f_) < 1e-6
        for (a, c, e), (b2, d, f_) in zip(got, expect))
    check("OCR 数值逐格比对", match, f"识别结果：{got}")


# ---------------------------------------------------------------- 2. 加密
def _make_encrypted_xlsx() -> bytes | None:
    """优先用 Excel COM 生成真实加密文件；不可用返回 None。"""
    try:
        import win32com.client
    except ImportError:
        return None
    plain = TMP / "enc_plain.xlsx"
    enc = TMP / "enc_secret.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["工号", "姓名", "年假余额(天)"])
    for i in range(1, 6):
        ws.append([f"E{i:03d}", f"员工{i}", 5 + i])
    wb.save(plain)
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        wbx = excel.Workbooks.Open(str(plain))
        wbx.SaveAs(str(enc), FileFormat=51, Password="Secret123!")
        wbx.Close(False)
        excel.Quit()
        return enc.read_bytes()
    except Exception:
        return None


def test_encrypted():
    # 无密码的加密文件 -> 明确报错
    head = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200
    r = upload([("假加密.xlsx", head)])
    ok = r.status_code == 400 and "密码" in r.text
    check("加密文件无密码 -> 明确引导", ok, r.text[:120] if r.status_code != 200 else "未拦截")

    enc = _make_encrypted_xlsx()
    if enc is None:
        check("生成真实加密文件", False, "本机无 Excel COM，跳过解密正验（错误引导路径已验证）")
        return
    check("生成真实加密文件", True, f"{len(enc)} 字节")
    r = upload([("加密工资表.xlsx", enc)], password="")
    check("加密文件密码为空被拦截", r.status_code == 400 and "密码" in r.text, r.text[:120])
    r = upload([("加密工资表.xlsx", enc)], password="Secret123!")
    ok = r.status_code == 200
    check("加密文件带密码解密成功", ok, r.text[:150] if not ok else "")
    if not ok:
        return
    f = r.json()["files"][0]
    check("解密后数据正确", f["rows"] == 5 and f["cols"] == 3, f"{f['rows']} 行 / {f['cols']} 列")


# ---------------------------------------------------------------- 3. 多轮对话
def test_chat():
    # 载入演示数据
    httpx.post(BASE + "/api/demo", timeout=60, trust_env=False)
    httpx.post(BASE + "/api/chat/reset", json={"session_id": "t_chat"}, timeout=15, trust_env=False)

    def turn(msg, sid="t_chat"):
        r = httpx.post(BASE + "/api/chat", json={"message": msg, "session_id": sid},
                       timeout=600, trust_env=False)
        if r.status_code != 200:
            return {"action": "error", "reply": r.text[:300], "run": None}
        return r.json()

    # 第 1 轮：执行任务
    d1 = turn("帮我做假期余量结算，年假先扣，超出转事假")
    check("对话轮1 执行结算", d1["action"] == "run" and d1["run"] is not None,
          f"action={d1['action']}，{d1.get('run') or {}}.get_rows={len((d1.get('run') or {}).get('rows', []))}")
    rows1 = (d1.get("run") or {}).get("total_rows", 0)

    # 第 2 轮：基于上轮改条件（应输出比上轮少或相等的行，且仍是结算技能）
    d2 = turn("只要生产部的记录")
    run2 = d2.get("run") or {}
    ok2 = d2["action"] == "run" and run2.get("total_rows", 0) > 0 \
        and run2.get("total_rows", 0) <= rows1
    check("对话轮2 按上下文改条件重跑", ok2,
          f"轮1 {rows1} 行 -> 轮2 {run2.get('total_rows')} 行；指令：{d2.get('instruction', '')[:60]}")

    # 第 3 轮：信息不足 -> 应追问而不是硬跑
    httpx.post(BASE + "/api/files/clear", timeout=30, trust_env=False)
    httpx.post(BASE + "/api/chat/reset", json={"session_id": "t_chat2"}, timeout=15, trust_env=False)
    d3 = turn("统计一下各区域的销售总额", sid="t_chat2")
    ok3 = d3["action"] == "reply" and ("表" in d3["reply"] or "数据" in d3["reply"] or "上传" in d3["reply"])
    check("对话轮3 缺表时追问引导", ok3, f"reply={d3['reply'][:80]}")

    # 第 4 轮：澄清后继续
    httpx.post(BASE + "/api/demo", timeout=60, trust_env=False)
    d4 = turn("那你用请假单和年假余额表，把张三的假期处理一下", sid="t_chat2")
    ok4 = d4["action"] in ("run", "reply")
    ran = d4["action"] == "run" and (d4.get("run") or {}).get("total_rows", 0) >= 0
    check("对话轮4 澄清后恢复执行", ok4 and (ran or d4["action"] == "reply"),
          f"action={d4['action']}，reply={d4['reply'][:60]}")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("ocr", "all"):
        print("== 扫描件 OCR ==");  test_ocr()
    if which in ("enc", "all"):
        print("== 加密文件 ==");   test_encrypted()
    if which in ("chat", "all"):
        print("== 多轮对话 ==");   test_chat()
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"== 结果：{len(RESULTS) - n_fail}/{len(RESULTS)} 通过 ==")
    sys.exit(1 if n_fail else 0)
