/**
 * Capture the exact Facebook URL Dola opens (window.open / FB tab).
 *   node capture_oauth_open.mjs --uid 61562945431350
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const INPUT = path.join(ROOT, "input", "fb_cookies.txt");
const OUT = path.join(ROOT, "output", "debug");
const DOLA_HOME = "https://www.dola.com/chat/";
const CHROME_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36";

function parseArgs(argv) {
  const args = { uid: "" };
  for (let i = 0; i < argv.length; i++) if (argv[i] === "--uid") args.uid = argv[++i];
  return args;
}

function parseCookieHeader(text) {
  const cookies = {};
  for (const part of String(text || "").split(";")) {
    const piece = part.trim();
    if (!piece.includes("=")) continue;
    const idx = piece.indexOf("=");
    cookies[piece.slice(0, idx).trim()] = piece.slice(idx + 1).trim();
  }
  return cookies;
}

function loadUid(uid) {
  for (const line of fs.readFileSync(INPUT, "utf8").split(/\r?\n/)) {
    if (!line.includes(`c_user=${uid}`)) continue;
    return parseCookieHeader(line);
  }
  throw new Error(`uid ${uid} not in fb_cookies.txt`);
}

function findChrome() {
  const cands = [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    path.join(process.env.LOCALAPPDATA || "", "Google\\Chrome\\Application\\chrome.exe"),
  ];
  for (const c of cands) if (fs.existsSync(c)) return c;
  throw new Error("Chrome not found");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function clickFacebookIcon(page) {
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll("button")].find((b) => (b.innerText || "").trim() === "Log In");
    if (btn) btn.click();
  });
  await sleep(1000);
  return page.evaluate(() => {
    const google = [...document.querySelectorAll("button")].find((b) => (b.innerText || "").includes("Continue with Google"));
    if (!google) return "";
    const gr = google.getBoundingClientRect();
    let root = google.parentElement;
    for (let i = 0; i < 8 && root; i++) {
      if ((root.innerText || "").includes("Scan QR")) break;
      root = root.parentElement;
    }
    const icons = [...(root || document).querySelectorAll("div.button-PgvIWh, div[class*='clickable']")]
      .map((n) => {
        const r = n.getBoundingClientRect();
        return { n, x: r.x + r.width / 2, w: r.width, h: r.height, top: r.top };
      })
      .filter((c) => c.w >= 40 && c.w <= 56 && c.h >= 40 && c.h <= 56 && c.top > gr.bottom - 5 && c.top < gr.bottom + 140)
      .sort((a, b) => a.x - b.x);
    const uniq = [];
    for (const c of icons) if (!uniq.some((u) => Math.abs(u.x - c.x) < 5)) uniq.push(c);
    const fb = uniq[1];
    if (!fb) return "";
    fb.n.click();
    return `facebook-icon#1/${uniq.length}`;
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.uid) throw new Error("--uid required");
  fs.mkdirSync(OUT, { recursive: true });
  const fbCookies = loadUid(args.uid);

  const browser = await puppeteer.launch({
    executablePath: findChrome(),
    headless: false,
    defaultViewport: { width: 1280, height: 900 },
    args: ["--disable-blink-features=AutomationControlled", "--window-size=1280,900"],
  });
  const page = (await browser.pages())[0] || (await browser.newPage());
  await page.setUserAgent(CHROME_UA);
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    const realOpen = window.open.bind(window);
    window.open = function (url, name) {
      window.__lastOpen = String(url || "");
      return realOpen(url, name || "fb_dola_oauth");
    };
  });

  await page.goto("https://www.facebook.com/", { waitUntil: "domcontentloaded" });
  await page.setCookie(
    ...Object.entries(fbCookies).map(([name, value]) => ({
      name,
      value,
      domain: ".facebook.com",
      path: "/",
      secure: true,
    })),
  );
  await page.reload({ waitUntil: "domcontentloaded" });
  await sleep(500);

  await page.goto(DOLA_HOME, { waitUntil: "domcontentloaded" });
  await sleep(2500);

  const pageTokens = await page.evaluate(() => {
    const html = document.documentElement.outerHTML;
    const hex = [...html.matchAll(/dola\.com(?:\\u00252F|%2F|%252F|\\\/|\/)([a-f0-9]{15,20})/gi)].map((m) => m[1]);
    const xd = [...html.matchAll(/xd_arbiter[^"'`]{0,240}/gi)].map((m) => m[0]).slice(0, 8);
    const origins = [...html.matchAll(/origin=https[^"'`\s]{0,200}/gi)].map((m) => m[0]).slice(0, 8);
    return { hex: [...new Set(hex)], xd, origins };
  });
  console.log("pageTokens", JSON.stringify(pageTokens, null, 2));

  const before = new Set(await browser.pages());
  console.log("click", await clickFacebookIcon(page));
  let fbTab = null;
  for (let i = 0; i < 80 && !fbTab; i++) {
    for (const p of await browser.pages()) {
      if (before.has(p) || p.isClosed()) continue;
      try {
        if (/facebook\.com/i.test(p.url()) && p.url() !== "about:blank") fbTab = p;
      } catch {
        // ignore
      }
    }
    await sleep(250);
  }
  const openUrl = await page.evaluate(() => window.__lastOpen || "");
  const fbUrl = fbTab ? fbTab.url() : "";
  console.log("window.open", openUrl.slice(0, 500));
  console.log("fbTab", fbUrl.slice(0, 500));

  const params = {};
  try {
    const u = new URL(fbUrl || openUrl);
    for (const [k, v] of u.searchParams.entries()) params[k] = v;
  } catch {
    // ignore
  }

  const out = { uid: args.uid, openUrl, fbUrl, params, pageTokens };
  const outPath = path.join(OUT, `${args.uid}_oauth_open.json`);
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log("saved", outPath);
  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
