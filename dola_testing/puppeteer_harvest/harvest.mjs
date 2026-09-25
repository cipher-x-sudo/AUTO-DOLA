/**
 * Dola UI click harvest — real Chrome, click Login → Facebook → Continue as.
 *
 * Manual (you click, script waits for cookies):
 *   node harvest.mjs --uid 100086274619350 --manual --headed
 *
 * Auto clicks on Dola + FB popup:
 *   node harvest.mjs --uid 100086274619350 --headed
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const DEFAULT_INPUT = path.join(ROOT, "input", "fb_cookies.txt");
const DEFAULT_OUTPUT = path.join(ROOT, "output");
const DOLA_HOME = "https://www.dola.com/chat/";
const DOLA_AID = "495671";
const DOLA_FB_PLATFORM_APP_ID = "2204";
const CHROME_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36";

const AUTH_FRAGMENTS = ["sessionid", "sid_guard", "sid_tt", "uid_tt"];
const PUBLIC_DOLA = new Set([
  "ttwid",
  "s_v_web_id",
  "hook_slardar_session_id",
  "biz_trace_id",
  "flow_user_country",
  "store-idc",
  "store-country-code",
  "store-country-code-src",
  "i18next",
  "dbx-web-theme",
  "conversation_list_v2_group_mode",
  "_ga",
  "_ga_5mr93b9jt5",
  "_gcl_au",
  "passport_csrf_token",
  "passport_csrf_token_default",
  "passport_csrf_token_wap_state",
  "reg-store-region",
  "odin_tt",
]);

function parseArgs(argv) {
  const args = {
    file: DEFAULT_INPUT,
    output: DEFAULT_OUTPUT,
    uid: "",
    all: false,
    headed: true,
    manual: false,
    keepOpen: false,
    timeoutMs: 180_000,
    chrome: "",
    dailyLimit: 2,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = argv[i + 1];
    if (a === "--file" && next) {
      args.file = path.resolve(next);
      i++;
    } else if (a === "--output" && next) {
      args.output = path.resolve(next);
      i++;
    } else if (a === "--uid" && next) {
      args.uid = String(next).trim();
      i++;
    } else if (a === "--chrome" && next) {
      args.chrome = path.resolve(next);
      i++;
    } else if (a === "--timeout" && next) {
      args.timeoutMs = Number(next) || args.timeoutMs;
      i++;
    } else if (a === "--daily-limit" && next) {
      args.dailyLimit = Number(next) || 2;
      i++;
    } else if (a === "--all") args.all = true;
    else if (a === "--headed") args.headed = true;
    else if (a === "--headless") args.headed = false;
    else if (a === "--manual") args.manual = true;
    else if (a === "--auto") args.manual = false;
    else if (a === "--keep-open") args.keepOpen = true;
  }
  return args;
}

function findChrome(explicit) {
  if (explicit && fs.existsSync(explicit)) return explicit;
  const candidates = [
    process.env.CHROME_PATH,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    path.join(process.env.LOCALAPPDATA || "", "Google\\Chrome\\Application\\chrome.exe"),
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error("Chrome/Edge not found. Pass --chrome path\\to\\chrome.exe");
}

function parseCookieHeader(text) {
  const cookies = {};
  for (const part of String(text || "").split(";")) {
    const piece = part.trim();
    if (!piece || !piece.includes("=")) continue;
    const idx = piece.indexOf("=");
    const name = piece.slice(0, idx).trim();
    const value = piece.slice(idx + 1).trim();
    if (name && value) cookies[name] = value;
  }
  return cookies;
}

function loadFacebookRows(filePath) {
  const raw = fs.readFileSync(filePath, "utf8");
  const rows = [];
  for (const line of raw.split(/\r?\n/)) {
    const cleaned = line.trim();
    if (!cleaned || cleaned.startsWith("#")) continue;
    const parsed = parseCookieHeader(cleaned);
    if (parsed.c_user && parsed.xs) rows.push(parsed);
  }
  return rows;
}

function isDolaAuth(name) {
  const lower = String(name || "").toLowerCase();
  if (PUBLIC_DOLA.has(lower)) return false;
  return AUTH_FRAGMENTS.some((frag) => lower.includes(frag));
}

function filterDolaCookies(cookies) {
  const seen = new Set();
  const out = [];
  for (const cookie of cookies) {
    const name = String(cookie.name || "").trim();
    const value = String(cookie.value || "");
    const domain = String(cookie.domain || "");
    if (!name || !value || seen.has(name)) continue;
    if (!domain.toLowerCase().includes("dola.com")) continue;
    seen.add(name);
    out.push({
      name,
      value,
      domain: ".dola.com",
      path: cookie.path || "/",
      secure: cookie.secure !== false,
      httpOnly: Boolean(cookie.httpOnly),
      sameSite: cookie.sameSite || undefined,
    });
  }
  return out;
}

function facebookPuppeteerCookies(cookies) {
  const httpOnly = new Set(["xs", "datr", "sb"]);
  return Object.entries(cookies).map(([name, value]) => ({
    name,
    value,
    domain: ".facebook.com",
    path: "/",
    secure: true,
    httpOnly: httpOnly.has(name),
    sameSite: "None",
  }));
}

async function sleep(ms) {
  await new Promise((r) => setTimeout(r, ms));
}

async function listButtons(page, limit = 40) {
  return page.evaluate((max) => {
    const nodes = [...document.querySelectorAll("button, [role='button'], a")];
    const out = [];
    for (const node of nodes) {
      const text = (node.innerText || node.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
      if (!text || text.length > 80) continue;
      const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
      if (!visible) continue;
      out.push(text.slice(0, 80));
      if (out.length >= max) break;
    }
    return out;
  }, limit);
}

/** Click short UI labels on Dola / Facebook. Skips legal "by continuing..." text. */
async function clickLabel(page, labels, { timeoutMs = 5000, maxLen = 42 } = {}) {
  const needles = labels.map((t) => t.toLowerCase());
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const clicked = await page.evaluate(
      ({ needles, maxLen }) => {
        const nodes = [...document.querySelectorAll("button, [role='button'], a, div, span, img")];
        for (const node of nodes) {
          const text = (node.innerText || node.getAttribute("aria-label") || node.getAttribute("alt") || "")
            .replace(/\s+/g, " ")
            .trim()
            .toLowerCase();
          if (!text || text.length > maxLen) continue;
          if (text.includes("by continuing") || text.includes("agree to dola")) continue;
          const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
          if (!visible) continue;
          if (!needles.some((n) => text === n || text.startsWith(n))) continue;
          (node.closest("button, [role='button'], a") || node).click();
          return text.slice(0, 80);
        }
        return "";
      },
      { needles, maxLen },
    );
    if (clicked) return clicked;
    await sleep(250);
  }
  return "";
}

async function clickFacebookOnDola(page) {
  // Tracked via IDE browser: Log In → modal → 2nd circular icon under Google (phone, Facebook, Apple)
  await clickLabel(page, ["log in", "login", "sign in"], { timeoutMs: 3000 });
  await sleep(800);

  // Wait for login modal
  await page
    .waitForFunction(() => (document.body?.innerText || "").includes("Log In to Unlock More Features") || (document.body?.innerText || "").includes("Continue with Google"), {
      timeout: 8000,
    })
    .catch(() => {});

  const byText = await clickLabel(page, ["continue with facebook", "facebook"], { timeoutMs: 1500 });
  if (byText?.includes("facebook")) return byText;

  const iconHit = await page.evaluate(() => {
    const google = [...document.querySelectorAll("button")].find((b) => (b.innerText || "").includes("Continue with Google"));
    if (!google) return "";
    const gr = google.getBoundingClientRect();
    let root = google.parentElement;
    for (let i = 0; i < 8 && root; i++) {
      if ((root.innerText || "").includes("Scan QR") || (root.innerText || "").includes("Log In to Unlock")) break;
      root = root.parentElement;
    }
    root = root || document.body;
    const icons = [...root.querySelectorAll("div.button-PgvIWh, div[class*='clickable']")]
      .map((n) => {
        const r = n.getBoundingClientRect();
        return { n, x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height, top: r.top };
      })
      .filter((c) => c.w >= 40 && c.w <= 56 && c.h >= 40 && c.h <= 56 && c.top > gr.bottom - 5 && c.top < gr.bottom + 140)
      .sort((a, b) => a.x - b.x);
    const uniq = [];
    for (const c of icons) {
      if (!uniq.some((u) => Math.abs(u.x - c.x) < 5)) uniq.push(c);
    }
    // Order: phone(0), Facebook(1), Apple(2)
    const fb = uniq[1] || uniq.find(Boolean);
    if (!fb) return "";
    fb.n.click();
    return `facebook-icon#${uniq.indexOf(fb)}/${uniq.length}`;
  });
  return iconHit || "";
}

async function clickContinueAs(page, timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const clicked = await page.evaluate(() => {
      const nodes = [...document.querySelectorAll("button, [role='button'], a, div[role='button']")];
      // Prefer the blue "Continue as …" button only (never Cancel / combined parent text).
      for (const node of nodes) {
        const text = (node.innerText || node.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
        const low = text.toLowerCase();
        if (!low.startsWith("continue as")) continue;
        if (low.includes("cancel")) continue;
        if (text.length > 40) continue;
        const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
        if (!visible) continue;
        node.click();
        return text.slice(0, 80);
      }
      for (const node of nodes) {
        const text = (node.innerText || node.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim().toLowerCase();
        if (!(text === "allow" || text === "allow all" || text === "continue")) continue;
        const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
        if (!visible) continue;
        node.click();
        return text.slice(0, 80);
      }
      return "";
    });
    if (clicked) return clicked;
    await sleep(400);
  }
  return "";
}

function tokenFromUrl(url) {
  const text = String(url || "");
  const hashIdx = text.indexOf("#");
  const query = hashIdx >= 0 ? text.slice(hashIdx + 1) : text.split("?")[1] || "";
  try {
    return new URLSearchParams(query.replace(/&amp;/g, "&")).get("access_token") || "";
  } catch {
    const m = text.match(/access_token=([^&]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }
}

async function dolaLoginWithToken(page, accessToken, openid) {
  // Keep/restore Dola tab, then passport login so session cookies land on .dola.com
  if (!/dola\.com/i.test(page.url())) {
    await page.goto(DOLA_HOME, { waitUntil: "domcontentloaded", timeout: 60_000 });
    await sleep(800);
  }
  return page.evaluate(
    async ({ accessToken, openid, aid, platformAppId, home }) => {
      const q = new URLSearchParams({
        device_platform: "web",
        os_type: "1",
        terminal_type: "2",
        aid,
        account_sdk_source: "web",
        passport_jssdk_version: "2.0.1-verify-center.1",
        language: "en",
      });
      const csrf =
        document.cookie
          .split(";")
          .map((x) => x.trim())
          .find((x) => x.startsWith("passport_csrf_token="))
          ?.split("=")
          .slice(1)
          .join("=") || "";
      const body = new URLSearchParams({
        platform_app_id: platformAppId,
        access_token: accessToken,
        openid,
      });
      const headers = {
        accept: "application/json, text/plain, */*",
        "content-type": "application/x-www-form-urlencoded",
        origin: "https://www.dola.com",
        referer: home,
      };
      if (csrf) headers["x-tt-passport-csrf-token"] = csrf;
      const out = [];
      for (const p of ["/passport/web/auth/login_only/", "/passport/web/auth/login/"]) {
        const res = await fetch(`https://www.dola.com${p}?${q}`, {
          method: "POST",
          headers,
          body: body.toString(),
          credentials: "include",
        });
        out.push({ path: p, status: res.status, body: (await res.text()).slice(0, 200) });
      }
      return out;
    },
    {
      accessToken,
      openid,
      aid: DOLA_AID,
      platformAppId: DOLA_FB_PLATFORM_APP_ID,
      home: DOLA_HOME,
    },
  );
}

async function waitForFacebookTab(browser, dolaPage, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    for (const p of await browser.pages()) {
      if (p === dolaPage || p.isClosed()) continue;
      let url = "";
      try {
        url = p.url();
      } catch {
        continue;
      }
      if (!url || url === "about:blank") continue;
      if (/facebook\.com/i.test(url)) return p;
    }
    await sleep(250);
  }
  return null;
}

async function waitForDolaAuth(browser, timeoutMs, onTick) {
  const started = Date.now();
  let lastLog = 0;
  while (Date.now() - started < timeoutMs) {
    const cookies = filterDolaCookies(await browser.defaultBrowserContext().cookies());
    if (cookies.some((c) => isDolaAuth(c.name))) return cookies;
    if (onTick && Date.now() - lastLog > 5000) {
      lastLog = Date.now();
      await onTick(Math.round((Date.now() - started) / 1000));
    }
    await sleep(800);
  }
  return filterDolaCookies(await browser.defaultBrowserContext().cookies());
}

async function prepareMainPage(browser) {
  const pages = await browser.pages();
  const page = pages[0] || (await browser.newPage());
  for (const p of await browser.pages()) {
    if (p !== page && !p.isClosed()) await p.close().catch(() => {});
  }
  await page.setUserAgent(CHROME_UA);
  await page.setViewport({ width: 1280, height: 900 });
  // FB.login normally opens a popup WINDOW. Strip features so Chrome opens a TAB
  // in the SAME window, while keeping opener for xd_arbiter → Dola auto login.
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    const realOpen = window.open.bind(window);
    window.open = function (url, name) {
      // IMPORTANT: do not pass width/height features (those force a separate window).
      return realOpen(url, name || "fb_dola_oauth");
    };
  });
  await page.setExtraHTTPHeaders({
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Google Chrome";v="153", "Chromium";v="153", "Not A(Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
  });
  return page;
}

async function attachDolaNetworkSpy(page, hits) {
  page.on("request", (req) => {
    const url = req.url();
    const low = url.toLowerCase();
    if (
      low.includes("age") ||
      low.includes("birthday") ||
      low.includes("verify") ||
      low.includes("adult") ||
      low.includes("consent") ||
      low.includes("passport") ||
      low.includes("alice")
    ) {
      hits.push({
        phase: "request",
        method: req.method(),
        url: url.slice(0, 300),
        post: (req.postData() || "").slice(0, 400),
      });
      console.log(`  net → ${req.method()} ${url.slice(0, 160)}`);
    }
  });
  page.on("response", async (res) => {
    const url = res.url();
    const low = url.toLowerCase();
    if (!(low.includes("age") || low.includes("birthday") || low.includes("verify") || low.includes("adult") || low.includes("passport") || low.includes("alice"))) {
      return;
    }
    let body = "";
    try {
      body = (await res.text()).slice(0, 400);
    } catch {
      body = "";
    }
    hits.push({ phase: "response", status: res.status(), url: url.slice(0, 300), body });
    console.log(`  net ← ${res.status()} ${url.slice(0, 140)}`);
  });
}

async function handleAgeVerification(page, { timeoutMs = 45_000, outputDir, cUser } = {}) {
  await page.bringToFront().catch(() => {});
  if (!/dola\.com/i.test(page.url())) {
    await page.goto(DOLA_HOME, { waitUntil: "domcontentloaded", timeout: 60_000 }).catch(() => {});
  }
  await sleep(1500);

  const started = Date.now();
  let clicked = "";
  while (Date.now() - started < timeoutMs) {
    const info = await page.evaluate(() => {
      const body = (document.body?.innerText || "").replace(/\s+/g, " ").trim();
      const low = body.toLowerCase();
      const ageLike =
        low.includes("age") ||
        low.includes("birthday") ||
        low.includes("years old") ||
        low.includes("date of birth") ||
        low.includes("confirm you") ||
        low.includes("over 18") ||
        low.includes("18+") ||
        low.includes("verify");
      const buttons = [...document.querySelectorAll("button, [role='button'], a, div[role='button']")]
        .map((n) => (n.innerText || n.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim())
        .filter((t) => t && t.length <= 40)
        .slice(0, 25);
      return { ageLike, snippet: body.slice(0, 240), buttons };
    });

    if (info.ageLike || info.buttons.some((b) => /confirm|continue|agree|yes|i'?m|verify|submit|ok/i.test(b))) {
      console.log(`  age-gate? text="${info.snippet.slice(0, 120)}"`);
      console.log(`  age-gate buttons: ${info.buttons.slice(0, 12).join(" | ") || "(none)"}`);
    }

    // Prefer explicit confirm / age labels
    clicked =
      (await clickLabel(page, ["confirm", "i confirm", "confirm age", "verify", "i am 18", "i'm 18", "yes, i am", "continue"], {
        timeoutMs: 1200,
        maxLen: 48,
      })) ||
      (await page.evaluate(() => {
        const needles = ["confirm", "continue", "agree", "verify", "submit", "yes"];
        const nodes = [...document.querySelectorAll("button, [role='button'], a")];
        for (const node of nodes) {
          const text = (node.innerText || node.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim().toLowerCase();
          if (!text || text.length > 36) continue;
          if (text.includes("cancel") || text.includes("log out") || text.includes("close")) continue;
          if (!needles.some((n) => text === n || text.startsWith(n))) continue;
          const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
          if (!visible) continue;
          node.click();
          return text;
        }
        return "";
      }));

    if (clicked) {
      console.log(`  age/confirm click: ${clicked}`);
      await sleep(2000);
      break;
    }
    await sleep(800);
  }

  try {
    const dir = path.join(outputDir, "debug");
    fs.mkdirSync(dir, { recursive: true });
    await page.screenshot({ path: path.join(dir, `${cUser}_after_login.png`), fullPage: true });
  } catch {
    // ignore
  }
  return clicked;
}

async function harvestOne(browser, facebookCookies, { timeoutMs, outputDir, manual }) {
  const cUser = facebookCookies.c_user;
  const context = browser.defaultBrowserContext();
  const dolaPage = await prepareMainPage(browser);
  const netHits = [];
  await attachDolaNetworkSpy(dolaPage, netHits);

  const shot = async (name, page = dolaPage) => {
    try {
      const dir = path.join(outputDir, "debug");
      fs.mkdirSync(dir, { recursive: true });
      await page.screenshot({ path: path.join(dir, `${cUser}_${name}.png`), fullPage: true });
    } catch {
      // ignore
    }
  };

  await dolaPage.goto("https://www.facebook.com/", { waitUntil: "domcontentloaded", timeout: 60_000 });
  await context.setCookie(...facebookPuppeteerCookies(facebookCookies));
  await dolaPage.reload({ waitUntil: "domcontentloaded", timeout: 60_000 });
  await sleep(1000);
  console.log(`  facebook cookies set: ${dolaPage.url().slice(0, 80)}`);

  await dolaPage.goto(DOLA_HOME, { waitUntil: "domcontentloaded", timeout: 60_000 });
  await sleep(2000);
  console.log(`  dola tab: ${dolaPage.url().slice(0, 120)}`);
  console.log(`  dola buttons: ${(await listButtons(dolaPage)).slice(0, 12).join(" | ") || "(none)"}`);
  await shot("dola_home");

  let fbTab = null;

  if (manual) {
    console.log("");
    console.log("  MANUAL — Dola tab pe: Log In → Facebook (FB new tab mein khulega)");
    console.log("  phir FB tab pe Continue as … dabao");
    console.log(`  Waiting ${Math.round(timeoutMs / 1000)}s...`);
    console.log("");
    fbTab = await waitForFacebookTab(browser, dolaPage, timeoutMs);
  } else {
    const tabPromise = waitForFacebookTab(browser, dolaPage, 40_000);
    const fbHit = await clickFacebookOnDola(dolaPage);
    console.log(`  dola click: ${fbHit || "none"}`);
    if (!fbHit || !String(fbHit).includes("facebook")) {
      await sleep(600);
      console.log(`  dola click2: ${(await clickFacebookOnDola(dolaPage)) || "none"}`);
    }

    fbTab = await tabPromise;
    if (!fbTab) {
      console.log("  FB tab wait extra...");
      fbTab = await waitForFacebookTab(browser, dolaPage, 20_000);
    }
  }

  if (!fbTab) {
    if (/facebook\.com/i.test(dolaPage.url())) {
      fbTab = dolaPage;
      console.log("  FB opened in same tab (fallback)");
    } else {
      throw new Error("Facebook tab nahi khuli. Dola pe Facebook icon click check karo.");
    }
  } else {
    console.log(`  fb tab: ${fbTab.url().slice(0, 140)}`);
  }

  await fbTab.bringToFront().catch(() => {});
  await sleep(1000);
  await shot("fb_tab", fbTab);

  if (!manual) {
    const consent = await clickContinueAs(fbTab, 35_000);
    console.log(`  consent: ${consent || "none — Continue as khud dabao"}`);
  }

  let accessToken = "";
  const tokenWait = Date.now() + (manual ? timeoutMs : 45_000);
  let cookies = [];
  while (Date.now() < tokenWait && !accessToken) {
    try {
      if (!fbTab.isClosed()) accessToken = tokenFromUrl(fbTab.url());
    } catch {
      break;
    }
    cookies = filterDolaCookies(await context.cookies());
    if (cookies.some((c) => isDolaAuth(c.name))) {
      console.log("  Dola auth cookies already set via opener callback");
      break;
    }
    await sleep(400);
  }

  if (accessToken && !cookies.some((c) => isDolaAuth(c.name))) {
    console.log(`  access_token: ${accessToken.slice(0, 12)}...`);
    await dolaPage.bringToFront().catch(() => {});
    const me = await dolaPage.evaluate(async (token) => {
      const res = await fetch(`https://graph.facebook.com/me?fields=id&access_token=${encodeURIComponent(token)}`);
      return res.json();
    }, accessToken);
    const openid = String(me?.id || cUser);
    console.log(`  openid: ${openid}`);
    const loginResults = await dolaLoginWithToken(dolaPage, accessToken, openid);
    console.log(`  dola login: ${loginResults.map((r) => `${r.path}:${r.status}`).join(", ")}`);
  } else if (!cookies.some((c) => isDolaAuth(c.name))) {
    console.log("  waiting for Dola opener auto-login...");
    await dolaPage.bringToFront().catch(() => {});
  }

  if (!cookies.some((c) => isDolaAuth(c.name))) {
    cookies = await waitForDolaAuth(browser, Math.min(timeoutMs, 90_000), async (sec) => {
      console.log(`  waiting for Dola auth cookies... ${sec}s`);
      try {
        if (fbTab && !fbTab.isClosed() && /facebook\.com/i.test(fbTab.url())) {
          await clickContinueAs(fbTab, 1200);
        }
      } catch {
        // ignore
      }
    });
  }

  if (!cookies.some((c) => isDolaAuth(c.name))) {
    await shot("fail");
    throw new Error(`No Dola auth cookies. names=${cookies.map((c) => c.name).join(",") || "(none)"}`);
  }

  // Post-login: stay on Dola, intercept network, clear age/confirm gate if shown.
  await dolaPage.bringToFront().catch(() => {});
  await dolaPage.goto(DOLA_HOME, { waitUntil: "domcontentloaded", timeout: 60_000 }).catch(() => {});
  const ageClick = await handleAgeVerification(dolaPage, { timeoutMs: 40_000, outputDir, cUser });
  console.log(`  age/confirm result: ${ageClick || "none visible"}`);

  // refresh cookies after age gate
  cookies = filterDolaCookies(await context.cookies());

  // dump network spy
  try {
    const dir = path.join(outputDir, "debug");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, `${cUser}_network.json`), JSON.stringify(netHits, null, 2));
    console.log(`  network hits saved: ${netHits.length}`);
  } catch {
    // ignore
  }

  return { cUser, cookies, ageClick, netHits };
}

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}_${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`;
}

function writeOutputs(outputDir, cUser, cookies, dailyLimit) {
  fs.mkdirSync(outputDir, { recursive: true });
  const profilesDir = path.join(outputDir, "profiles");
  fs.mkdirSync(profilesDir, { recursive: true });
  const stem = `${cUser}_${stamp()}`;
  const jsonPath = path.join(outputDir, `${stem}.json`);
  const txtPath = path.join(outputDir, `${stem}.txt`);
  const profilePath = path.join(profilesDir, `${cUser}.json`);
  fs.writeFileSync(jsonPath, JSON.stringify(cookies, null, 2));
  fs.writeFileSync(txtPath, cookies.map((c) => `${c.name}=${c.value}`).join("; "));
  fs.writeFileSync(profilePath, JSON.stringify(cookies, null, 2));
  const bulkPath = path.join(outputDir, "auto_dola_bulk_import.json");
  fs.writeFileSync(
    bulkPath,
    JSON.stringify(
      {
        version: 1,
        default_daily_limit: dailyLimit,
        profiles: [{ name: cUser, daily_limit: dailyLimit, enabled: true, cookies }],
        failures: [],
      },
      null,
      2,
    ),
  );
  return { jsonPath, names: cookies.map((c) => c.name).sort() };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.uid && !args.all) {
    console.error("Pass --uid <c_user> or --all");
    process.exit(2);
  }
  const rows = loadFacebookRows(args.file);
  const selected = args.all ? rows : rows.filter((r) => String(r.c_user) === args.uid);
  if (!selected.length) {
    console.error(`No matching cookie row for uid=${args.uid || "(all)"}`);
    process.exit(1);
  }

  const chromePath = findChrome(args.chrome);
  console.log(`Chrome: ${chromePath}`);
  console.log(`UA: ${CHROME_UA}`);
  console.log(`Mode: ${args.manual ? "MANUAL clicks" : "AUTO clicks"} | headed=${args.headed} | keepOpen=${args.keepOpen} | accounts=${selected.length}`);

  const browser = await puppeteer.launch({
    executablePath: chromePath,
    headless: args.headed ? false : true,
    defaultViewport: { width: 1280, height: 900 },
    args: [
      "--disable-blink-features=AutomationControlled",
      "--no-first-run",
      "--no-default-browser-check",
      "--window-size=1280,900",
      "--disable-features=IsolateOrigins,site-per-process",
    ],
  });
  {
    const pages = await browser.pages();
    for (let i = 1; i < pages.length; i++) {
      await pages[i].close().catch(() => {});
    }
  }

  let ok = 0;
  let failed = 0;
  try {
    for (const row of selected) {
      console.log(`\n[${row.c_user}] start`);
      try {
        const result = await harvestOne(browser, row, {
          timeoutMs: args.timeoutMs,
          outputDir: args.output,
          manual: args.manual,
        });
        const paths = writeOutputs(args.output, result.cUser, result.cookies, args.dailyLimit);
        console.log(`[${row.c_user}] OK ${paths.names.filter((n) => isDolaAuth(n)).join(", ")}`);
        console.log(`[${row.c_user}] saved ${paths.jsonPath}`);
        if (result.ageClick) console.log(`[${row.c_user}] age/confirm: ${result.ageClick}`);
        ok += 1;
      } catch (err) {
        console.error(`[${row.c_user}] failed: ${err?.message || err}`);
        failed += 1;
      }
    }
  } finally {
    if (args.keepOpen) {
      console.log("\n--keep-open: browser open rahega. Ctrl+C se band karo.");
      await new Promise(() => {});
    } else {
      await browser.close().catch(() => {});
    }
  }
  console.log(`\nDone ok=${ok} failed=${failed}`);
  if (failed) process.exitCode = 1;
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
