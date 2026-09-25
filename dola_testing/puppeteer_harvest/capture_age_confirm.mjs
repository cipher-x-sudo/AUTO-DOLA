/**
 * Capture the exact network request fired by Dola "Confirm Your Age" → Confirm.
 *
 *   node capture_age_confirm.mjs --uid 61589169932294
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
  const args = { uid: "", keepOpen: true };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--uid") args.uid = argv[++i];
    if (argv[i] === "--no-keep-open") args.keepOpen = false;
  }
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

async function sleep(ms) {
  await new Promise((r) => setTimeout(r, ms));
}

async function clickContinueAs(page) {
  return page.evaluate(() => {
    const nodes = [...document.querySelectorAll("button, [role='button'], a, div[role='button']")];
    for (const node of nodes) {
      const text = (node.innerText || "").replace(/\s+/g, " ").trim();
      const low = text.toLowerCase();
      if (!low.startsWith("continue as") || low.includes("cancel") || text.length > 40) continue;
      node.click();
      return text;
    }
    return "";
  });
}

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
  const captured = [];

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
    window.open = (url, name) => realOpen(url, name || "fb_dola_oauth");
  });

  // Capture ALL dola.com XHR/fetch around Confirm click
  await page.setRequestInterception(true);
  page.on("request", (req) => {
    const url = req.url();
    if (url.includes("dola.com") || url.includes("ciciai.com") || url.includes("byteimg") === false) {
      if (/dola\.com|ciciai\.com|ibyteimg\.com|snssdk|byteoversea|tiktok/i.test(url)) {
        const entry = {
          at: new Date().toISOString(),
          phase: "request",
          method: req.method(),
          url,
          headers: req.headers(),
          postData: req.postData() || "",
        };
        // keep interesting ones in memory always; dump all dola POSTs
        if (req.method() !== "GET" || /age|birth|adult|confirm|gate|user|alice|passport|onboard/i.test(url)) {
          captured.push(entry);
          console.log(`REQ ${req.method()} ${url.slice(0, 180)}`);
          if (entry.postData) console.log(`  body: ${entry.postData.slice(0, 300)}`);
        }
      }
    }
    req.continue().catch(() => {});
  });
  page.on("response", async (res) => {
    const url = res.url();
    if (!/dola\.com/i.test(url)) return;
    if (res.request().method() === "GET" && !/age|birth|adult|confirm|gate|user\/|alice|passport|onboard/i.test(url)) return;
    let body = "";
    try {
      body = (await res.text()).slice(0, 800);
    } catch {
      body = "";
    }
    captured.push({
      at: new Date().toISOString(),
      phase: "response",
      status: res.status(),
      url,
      body,
    });
    console.log(`RES ${res.status()} ${url.slice(0, 180)}`);
    if (body) console.log(`  resp: ${body.slice(0, 250)}`);
  });

  // FB cookies
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
  await sleep(800);

  await page.goto(DOLA_HOME, { waitUntil: "domcontentloaded" });
  await sleep(2000);

  const before = new Set(await browser.pages());
  console.log("click facebook...", await clickFacebookIcon(page));
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
  if (!fbTab) throw new Error("no FB tab");
  console.log("fb tab", fbTab.url().slice(0, 120));
  await fbTab.bringToFront();
  await sleep(1500);
  console.log("consent", await clickContinueAs(fbTab));

  // Wait for age modal on Dola
  await page.bringToFront();
  const deadline = Date.now() + 90_000;
  let sawAge = false;
  while (Date.now() < deadline) {
    const info = await page.evaluate(() => {
      const text = (document.body?.innerText || "").replace(/\s+/g, " ");
      const buttons = [...document.querySelectorAll("button, [role='button']")].map((b) =>
        (b.innerText || "").replace(/\s+/g, " ").trim(),
      );
      return {
        has: /confirm your age|at least 18 years old/i.test(text),
        buttons: buttons.filter(Boolean).slice(0, 30),
      };
    });
    if (info.has) {
      sawAge = true;
      console.log("AGE MODAL visible. buttons:", info.buttons.join(" | "));
      await page.screenshot({ path: path.join(OUT, `${args.uid}_age_modal.png`), fullPage: true });
      break;
    }
    await sleep(500);
  }
  if (!sawAge) {
    console.log("Age modal not seen. Dumping page buttons...");
    console.log(
      await page.evaluate(() =>
        [...document.querySelectorAll("button,[role=button]")]
          .map((b) => (b.innerText || "").replace(/\s+/g, " ").trim())
          .filter(Boolean)
          .slice(0, 40),
      ),
    );
    await page.screenshot({ path: path.join(OUT, `${args.uid}_no_age_modal.png`), fullPage: true });
  } else {
    // Mark capture window
    const mark = Date.now();
    console.log(">>> CLICKING Confirm now");
    const clicked = await page.evaluate(() => {
      const nodes = [...document.querySelectorAll("button, [role='button'], a")];
      for (const node of nodes) {
        const text = (node.innerText || "").replace(/\s+/g, " ").trim();
        if (text === "Confirm") {
          node.click();
          return text;
        }
      }
      // fallback
      for (const node of nodes) {
        const text = (node.innerText || "").replace(/\s+/g, " ").trim().toLowerCase();
        if (text === "confirm" || text.startsWith("confirm")) {
          node.click();
          return text;
        }
      }
      return "";
    });
    console.log("clicked:", clicked);
    await sleep(5000);
    const afterClick = captured.filter((c) => new Date(c.at).getTime() >= mark - 500);
    console.log("\n=== REQUESTS AROUND CONFIRM CLICK ===");
    for (const c of afterClick) {
      if (c.phase === "request") {
        console.log(`\n${c.method} ${c.url}`);
        console.log("headers:", JSON.stringify(c.headers, null, 2).slice(0, 800));
        console.log("postData:", c.postData);
      } else {
        console.log(`\n← ${c.status} ${c.url}`);
        console.log("body:", c.body);
      }
    }
    fs.writeFileSync(path.join(OUT, `${args.uid}_age_confirm_capture.json`), JSON.stringify({ afterClick, all: captured }, null, 2));
    console.log("saved", path.join(OUT, `${args.uid}_age_confirm_capture.json`));
  }

  if (args.keepOpen) {
    console.log("keep-open: browser stays up");
    await new Promise(() => {});
  } else {
    await browser.close();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
