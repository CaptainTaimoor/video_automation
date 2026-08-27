const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const args = process.argv.slice(2);
function arg(name, fallback = "") {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}
function has(name) {
  return args.includes(name);
}

const templateUrl = arg("--url");
const outDir = path.resolve(arg("--out", "C:\\tmp\\heygen_downloads"));
const profileDir = path.resolve(arg("--profile", "C:\\tmp\\heygen_playwright_profile"));
const timeoutMs = Number(arg("--timeout", "900000"));

if (!templateUrl) {
  console.error("Usage: node heygen_backend_capture.js --url <heygen-template-url> [--out C:\\tmp\\heygen_downloads]");
  process.exit(2);
}

fs.mkdirSync(outDir, { recursive: true });
fs.mkdirSync(profileDir, { recursive: true });

const MEDIA_RE = /\.(mp4|webm|mov|m4v|m3u8)(\?|#|$)/i;
const CDN_RE = /heygen|heygencdn|heycdn|amazonaws|cloudfront|akamai|fastly/i;
const PATH_RE = /\/(video|media|stream|hls|render)\//i;
const SKIP_RE = /\.(js|css|json|woff|woff2|png|jpg|jpeg|gif|ico|svg|html|xml|map|txt)(\?|#|$)/i;

function isVideoCandidate(url, contentType = "") {
  if (!url || SKIP_RE.test(url)) return false;
  if (/^video\//i.test(contentType)) return true;
  if (/mpegurl/i.test(contentType)) return true;
  return MEDIA_RE.test(url) || (CDN_RE.test(url) && PATH_RE.test(url));
}

function safeNameFromUrl(url, ext = "mp4") {
  try {
    const parsed = new URL(url);
    const last = parsed.pathname.split("/").filter(Boolean).pop() || "";
    const clean = decodeURIComponent(last.split("?")[0]).replace(/[<>:"/\\|?*]+/g, "_");
    if (clean && /\.[a-z0-9]{2,5}$/i.test(clean)) return clean;
  } catch {}
  return `heygen_render_${new Date().toISOString().replace(/[:.]/g, "-")}.${ext}`;
}

async function saveViaBrowser(page, url, filename) {
  const bytes = await page.evaluate(async (mediaUrl) => {
    const response = await fetch(mediaUrl, { credentials: "include" });
    if (!response.ok) throw new Error(`fetch failed ${response.status}`);
    const buffer = await response.arrayBuffer();
    return Array.from(new Uint8Array(buffer));
  }, url);
  const outPath = path.join(outDir, filename);
  fs.writeFileSync(outPath, Buffer.from(bytes));
  return outPath;
}

async function main() {
  const found = new Map();
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: "chrome",
    headless: false,
    acceptDownloads: true,
    downloadsPath: outDir,
    viewport: { width: 1365, height: 768 },
    args: ["--disable-blink-features=AutomationControlled"],
  });

  await context.addInitScript(() => {
    window.__HEYGEN_CAPTURED_MEDIA__ = [];
    const MEDIA_RE = /\.(mp4|webm|mov|m4v|m3u8)(\?|#|$)/i;
    const CDN_RE = /heygen|heygencdn|heycdn|amazonaws|cloudfront|akamai|fastly/i;
    const PATH_RE = /\/(video|media|stream|hls|render)\//i;
    const SKIP_RE = /\.(js|css|json|woff|woff2|png|jpg|jpeg|gif|ico|svg|html|xml|map|txt)(\?|#|$)/i;
    const seen = new Set();
    function emit(url, contentType = "") {
      if (!url || seen.has(url) || SKIP_RE.test(url)) return;
      if (!MEDIA_RE.test(url) && !(CDN_RE.test(url) && PATH_RE.test(url)) && !/^video\//i.test(contentType) && !/mpegurl/i.test(contentType)) return;
      seen.add(url);
      window.__HEYGEN_CAPTURED_MEDIA__.push({ url, contentType, timestamp: Date.now() });
      window.dispatchEvent(new CustomEvent("__heygenMedia", { detail: { url, contentType } }));
    }
    const originalFetch = window.fetch;
    window.fetch = function(input, init) {
      const url = typeof input === "string" ? input : input?.url || "";
      return originalFetch.apply(this, arguments).then((response) => {
        try { emit(url, response?.headers?.get("content-type") || ""); } catch {}
        return response;
      });
    };
    const OriginalXHR = window.XMLHttpRequest;
    class PatchedXHR extends OriginalXHR {
      open(method, url, ...rest) {
        this.__heygenUrl = url;
        return super.open(method, url, ...rest);
      }
      send(...sendArgs) {
        this.addEventListener("readystatechange", () => {
          if (this.readyState === 4 && this.__heygenUrl) {
            try { emit(this.__heygenUrl, this.getResponseHeader("content-type") || ""); } catch {}
          }
        });
        return super.send(...sendArgs);
      }
    }
    window.XMLHttpRequest = PatchedXHR;
    try {
      const observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) emit(entry.name, "");
      });
      observer.observe({ type: "resource", buffered: true });
    } catch {}
    setInterval(() => {
      try {
        for (const entry of performance.getEntriesByType("resource")) emit(entry.name, "");
        document.querySelectorAll("video,source").forEach((el) => {
          if (el.src && !el.src.startsWith("blob:")) emit(el.src, "");
          if (el.currentSrc && !el.currentSrc.startsWith("blob:")) emit(el.currentSrc, "");
        });
      } catch {}
    }, 2000);
  });

  const page = await context.newPage();
  page.on("response", async (response) => {
    const url = response.url();
    const contentType = response.headers()["content-type"] || "";
    if (isVideoCandidate(url, contentType)) {
      found.set(url, { url, contentType, source: "response" });
      console.log("[capture]", contentType || "unknown", url);
    }
  });
  page.on("download", async (download) => {
    const suggested = download.suggestedFilename() || safeNameFromUrl(download.url());
    const target = path.join(outDir, suggested);
    await download.saveAs(target);
    console.log("[download]", target);
  });

  await page.goto(templateUrl, { waitUntil: "domcontentloaded", timeout: 120000 });
  console.log("[open]", templateUrl);
  console.log("[info] If this Chrome profile is not logged into HeyGen, log in once in the opened browser.");

  await page.waitForTimeout(5000);
  const clicked = await page.evaluate(() => {
    const texts = ["generate", "render scene", "render", "create video", "submit"];
    const candidates = Array.from(document.querySelectorAll("button,[role=button],a,div,span"))
      .filter((el) => {
        const text = (el.innerText || el.textContent || "").trim().toLowerCase();
        if (!text) return false;
        return texts.some((needle) => text === needle || text.includes(needle));
      });
    for (const el of candidates) {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none") {
        el.click();
        return { ok: true, text: (el.innerText || el.textContent || "").trim() };
      }
    }
    return { ok: false };
  });
  console.log("[click]", JSON.stringify(clicked));

  const start = Date.now();
  let saved = "";
  while (Date.now() - start < timeoutMs) {
    await page.waitForTimeout(3000);
    const pageCaptured = await page.evaluate(() => window.__HEYGEN_CAPTURED_MEDIA__ || []);
    for (const item of pageCaptured) {
      if (isVideoCandidate(item.url, item.contentType)) {
        found.set(item.url, { ...item, source: "page" });
      }
    }

    const mp4 = Array.from(found.values()).reverse().find((item) => /\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(item.url) || /^video\//i.test(item.contentType || ""));
    if (mp4) {
      const filename = safeNameFromUrl(mp4.url, mp4.url.includes(".webm") ? "webm" : "mp4");
      console.log("[save-start]", filename);
      saved = await saveViaBrowser(page, mp4.url, filename);
      console.log("[saved]", saved);
      break;
    }
    console.log(`[wait] captured=${found.size}`);
  }

  if (!saved) {
    const listPath = path.join(outDir, `heygen_captured_${Date.now()}.json`);
    fs.writeFileSync(listPath, JSON.stringify(Array.from(found.values()), null, 2), "utf8");
    console.log("[no-direct-mp4] captured list saved:", listPath);
  }

  await context.close();
  if (!saved) process.exit(1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
