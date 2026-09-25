/**
 * Capture Facebook Sahara GDP GraphQL (Continue as) for HTTP replay fixes.
 *
 *   node capture_sahara.mjs --uid 61562945431350
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

const SAHARA = [
  "useSaharaCometConsentPromptInteractionsServerMutation",
  "useSaharaCometConsentPromptValidationServerMutation",
  "useSaharaCometConsentPostPromptOutcomeServerMutation",
];

function parseArgs(argv) {
  const args = { uid: "", keepOpen: false };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--uid") args.uid = argv[++i];
    if (argv[i] === "--keep-open") args.keepOpen = true;
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

function parseFormBody(raw) {
  const out = {};
  for (const part of String(raw || "").split("&")) {
    if (!part) continue;
    const i = part.indexOf("=");
    const k = decodeURIComponent((i >= 0 ? part.slice(0, i) : part).replace(/\+/g, " "));
    const v = decodeURIComponent((i >= 0 ? part.slice(i + 1) : "").replace(/\+/g, " "));
    out[k] = v;
  }
  return out;
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

  const attachSpy = async (page) => {
    await page.setRequestInterception(true);
    page.on("request", (req) => {
      const url = req.url();
      if (req.method() === "POST" && /facebook\.com\/api\/graphql/i.test(url)) {
        const form = parseFormBody(req.postData() || "");
        const friendly = form.fb_api_req_friendly_name || "";
        if (SAHARA.some((s) => friendly.includes(s)) || /Sahara|Consent/i.test(friendly)) {
          let variables = form.variables || "";
          try {
            variables = JSON.parse(variables);
          } catch {
            // keep string
          }
          const entry = {
            at: new Date().toISOString(),
            phase: "request",
            friendly,
            doc_id: form.doc_id || "",
            variables,
            form_keys: Object.keys(form),
            form_meta: {
              __user: form.__user,
              __req: form.__req,
              __rev: form.__rev,
              __spin_r: form.__spin_r,
              __spin_t: form.__spin_t,
              __hs: form.__hs,
              __comet_req: form.__comet_req,
              av: form.av,
            },
          };
          captured.push(entry);
          console.log(`REQ ${friendly} doc=${form.doc_id}`);
          const varsStr = typeof variables === "string" ? variables : JSON.stringify(variables);
          console.log(`  vars: ${varsStr.slice(0, 400)}`);
        }
      }
      req.continue().catch(() => {});
    });
    page.on("response", async (res) => {
      const url = res.url();
      if (!/facebook\.com\/api\/graphql/i.test(url)) return;
      const req = res.request();
      const form = parseFormBody(req.postData() || "");
      const friendly = form.fb_api_req_friendly_name || "";
      if (!SAHARA.some((s) => friendly.includes(s)) && !/Sahara|Consent/i.test(friendly)) return;
      let body = "";
      try {
        body = (await res.text()).slice(0, 2500);
      } catch {
        body = "";
      }
      captured.push({ at: new Date().toISOString(), phase: "response", friendly, status: res.status(), body });
      console.log(`RES ${res.status()} ${friendly}`);
      console.log(`  body: ${body.slice(0, 300)}`);
    });
  };

  const page = (await browser.pages())[0] || (await browser.newPage());
  await page.setUserAgent(CHROME_UA);
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    const realOpen = window.open.bind(window);
    window.open = (url, name) => realOpen(url, name || "fb_dola_oauth");
  });
  await attachSpy(page);

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
  console.log("fb tab", fbTab.url().slice(0, 160));
  await attachSpy(fbTab);
  await fbTab.bringToFront();
  await sleep(2000);
  console.log("consent", await clickContinueAs(fbTab));
  await sleep(8000);

  const outPath = path.join(OUT, `${args.uid}_sahara_capture.json`);
  fs.writeFileSync(outPath, JSON.stringify({ uid: args.uid, captured }, null, 2));
  console.log("saved", outPath, "entries", captured.length);

  if (args.keepOpen) {
    await new Promise(() => {});
  } else {
    await browser.close();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
