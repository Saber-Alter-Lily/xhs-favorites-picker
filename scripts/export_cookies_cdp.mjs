import fs from "node:fs";
import path from "node:path";

const port = Number(process.argv[2] || "9333");
const outputPath = process.argv[3];
if (!outputPath) {
  console.error("Usage: node export_cookies_cdp.mjs <port> <output-file>");
  process.exit(2);
}

const base = `http://127.0.0.1:${port}`;

async function fetchJson(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
  if (!res.ok) throw new Error(`HTTP ${res.status} from ${url}`);
  return await res.json();
}

async function cdpCall(wsUrl, method, params = {}) {
  return await new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timer = setTimeout(() => {
      try { ws.close(); } catch {}
      reject(new Error(`CDP timeout calling ${method}`));
    }, 10000);

    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({ id: 1, method, params }));
    });

    ws.addEventListener("message", (event) => {
      let msg;
      try {
        msg = JSON.parse(String(event.data));
      } catch (err) {
        clearTimeout(timer);
        try { ws.close(); } catch {}
        reject(err);
        return;
      }
      if (msg.id !== 1) return;
      clearTimeout(timer);
      try { ws.close(); } catch {}
      if (msg.error) {
        reject(new Error(`CDP ${method} failed: ${msg.error.message || JSON.stringify(msg.error)}`));
      } else {
        resolve(msg.result || {});
      }
    });

    ws.addEventListener("error", () => {
      clearTimeout(timer);
      reject(new Error("CDP WebSocket connection failed"));
    });
  });
}

function domainRank(domain) {
  if (domain === ".xiaohongshu.com") return 3;
  if (domain === "www.xiaohongshu.com") return 2;
  if (domain.endsWith(".xiaohongshu.com")) return 1;
  return 0;
}

try {
  const version = await fetchJson(`${base}/json/version`);
  if (!version.webSocketDebuggerUrl) {
    throw new Error("Chrome did not expose webSocketDebuggerUrl");
  }

  const result = await cdpCall(version.webSocketDebuggerUrl, "Storage.getCookies");
  const all = Array.isArray(result.cookies) ? result.cookies : [];
  const relevant = all.filter((c) => typeof c.domain === "string" && c.domain.includes("xiaohongshu.com"));

  if (relevant.length === 0) {
    throw new Error("No xiaohongshu.com cookies found in the dedicated browser profile. Log in there first.");
  }

  const chosen = new Map();
  for (const c of relevant) {
    if (!c || typeof c.name !== "string" || typeof c.value !== "string") continue;
    const prev = chosen.get(c.name);
    if (!prev || domainRank(c.domain) >= domainRank(prev.domain)) {
      chosen.set(c.name, c);
    }
  }
  const cookies = Object.fromEntries([...chosen.entries()].map(([name, c]) => [name, c.value]));

  if (!cookies.a1 || !cookies.web_session) {
    const keys = Object.keys(cookies).sort().join(", ");
    throw new Error(`Login cookies are incomplete. Found keys: ${keys || "none"}`);
  }

  const payload = {
    version: 1,
    platform: "xhs",
    createdAt: new Date().toISOString(),
    cookies,
  };
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, JSON.stringify(payload, null, 2) + "\n", { encoding: "utf8", mode: 0o600 });
  console.log(`Captured ${Object.keys(cookies).length} Xiaohongshu cookie keys from the dedicated browser profile.`);
  console.log(`Has a1: ${Boolean(cookies.a1)}`);
  console.log(`Has web_session: ${Boolean(cookies.web_session)}`);
  console.log(`Has webId: ${Boolean(cookies.webId)}`);
  process.exit(0);
} catch (err) {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
}
