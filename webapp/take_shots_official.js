/* 官方数据集实测截图：按场景分步上传（避免无关表干扰自由模式规划） */
"use strict";
const path = require("path");
const fs = require("fs");
const { chromium } = require(path.join(__dirname, "..", "..", "node_modules", "playwright-core"));

const BASE = "http://127.0.0.1:8620";
const OUT = path.join(__dirname, "shots");
const EXE = process.env.LOCALAPPDATA + "\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe";
const DS = path.join(__dirname, "..", "data", "official");

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: EXE, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const shot = (name) => page.screenshot({ path: path.join(OUT, name + ".png") });

  async function upload(page, files) {
    await page.setInputFiles("#file-input", files.map(f => path.join(DS, f)));
    await page.waitForSelector(".file-card", { timeout: 180000 });
    await page.waitForTimeout(600);
  }

  async function runAndShoot(page, instruction, shotNames) {
    await page.fill("#instruction", instruction);
    await page.click("#btn-run");
    try {
      await page.waitForSelector("#result-area .card", { timeout: 300000 });
    } catch (e) {
      await shot("ERR_" + shotNames[0]);           // 失败现场也截下来便于排查
      throw e;
    }
    await page.waitForTimeout(800);
    if (shotNames[0]) await shot(shotNames[0]);
    if (shotNames[1]) {
      await page.click('#res-tabs .tab[data-t="data"]');
      await page.waitForTimeout(500);
      await shot(shotNames[1]);
    }
  }


  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);

  // 场景一：OA 请假单 → HR 最低版本导入（13 行，真值 104 单元格 0 差异）
  await upload(page, ["OA导出请假单.xlsx"]);
  await shot("11_官方数据_请假单已上传");
  await runAndShoot(page,
    "基于 OA导出请假单，生成 HR 系统最低版本导入文件，目标列：工号（取工号）、姓名（取申请人）、部门（取部门）、缺勤类别（取请假类型）、起始时间（起始日期拼接起始时间时刻）、终止时间（结束日期拼接结束时间时刻）、合计小时（天大于0时等于天乘8，否则取小时）、免打卡（固定填 是）。这是跨系统格式转换任务，不涉及假期抵扣计算。",
    ["12_官方数据_请假导入结果"]);

  // 场景二：ERP 加班 3.6 万行 → HR 加班导入（真值 21.6 万单元格 0 差异）
  await page.click("#btn-clear");
  await page.waitForTimeout(500);
  await upload(page, ["ERP导出加班.XLSX"]);
  await runAndShoot(page,
    "基于 ERP导出加班，生成 HR 系统的导入文件，目标列依次为：工号（取工号）、姓名（取姓名）、部门（取部门）、职位（取职位）、起始日期（取起始日期）、申请加班（取申请加班，保持数值）。这是跨系统格式转换任务，不涉及加班类型判定或时长换算，请现场规划目标列。",
    ["13_官方数据_加班导入_3.6万行", "14_官方数据_加班导入结果"]);

  await browser.close();
  console.log("OFFICIAL_SHOTS_DONE");
})().catch(e => { console.error("FAIL:", e.message); process.exit(1); });
