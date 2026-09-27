/* 系统界面截图脚本：生成操作手册/案例所需图片 */
"use strict";
const path = require("path");
const fs = require("fs");
const { chromium } = require(path.join(__dirname, "..", "..", "node_modules", "playwright-core"));

const BASE = "http://127.0.0.1:8620";
const OUT = path.join(__dirname, "shots");
const EXE = process.env.LOCALAPPDATA + "\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe";

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: EXE, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const shot = (name) => page.screenshot({ path: path.join(OUT, name + ".png") });

  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  await shot("01_工作台_初始");

  // 载入演示数据
  await page.click("#btn-demo");
  await page.waitForSelector(".file-card");
  await page.waitForTimeout(500);
  await shot("02_工作台_已载入数据");

  // 填入指令并运行
  await page.fill("#instruction", "做假期余量结算：先扣年假额度，超出部分自动转成事假，年假和事假分行记录");
  await page.click("#btn-run");
  await page.waitForSelector("#result-area .card", { timeout: 60000 });
  await page.waitForTimeout(600);
  await shot("03_执行结果_总览");

  // 执行轨迹已在默认 tab；切到字段映射
  await page.click('#res-tabs .tab[data-t="map"]');
  await page.waitForTimeout(400);
  await shot("04_字段映射");

  // 结果数据
  await page.click('#res-tabs .tab[data-t="data"]');
  await page.waitForTimeout(400);
  await shot("05_结果数据");

  // 计算明细
  await page.click('#res-tabs .tab[data-t="detail"]');
  await page.waitForTimeout(400);
  await shot("06_计算明细");

  // 异常日志
  await page.click('#res-tabs .tab[data-t="anom"]');
  await page.waitForTimeout(400);
  await shot("07_异常日志");

  // 通用映射页
  await page.click('.nav-item[data-page="generic"]');
  await page.waitForTimeout(300);
  await page.click("#btn-gn-demo");
  await page.waitForSelector("#gn-preview table", { timeout: 30000 });
  await page.waitForTimeout(400);
  await shot("08_通用映射");

  // 验收自检
  await page.click('.nav-item[data-page="verify"]');
  await page.waitForTimeout(200);
  await page.click("#btn-verify");
  await page.waitForSelector("#verify-list .card", { timeout: 120000 });
  await page.waitForTimeout(400);
  await shot("09_验收自检");

  // 大模型设置
  await page.click('.nav-item[data-page="settings"]');
  await page.waitForTimeout(500);
  await shot("10_大模型设置");

  await browser.close();
  console.log("SHOTS_DONE:", fs.readdirSync(OUT).join(", "));
})().catch(e => { console.error("FAIL:", e.message); process.exit(1); });
