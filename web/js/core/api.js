/* ============================================================
   后端 API 访问层：统一超时、统一错误信封解析
   ============================================================ */
import { getApiBase, setApiBase } from "../config.js?v=2c2bf4b0";

// 对外统一出口：调用方只需从 core/api.js 取 API 相关能力
export { getApiBase, setApiBase };

/** 拼接 API 路径 */
export const api = (path) => `${getApiBase()}/api/v1${path}`;

/** 后端返回的 /files 地址可能是相对路径，需要补上 API 前缀 */
export const fileUrl = (u) => (!u ? "" : u.startsWith("http") ? u : `${getApiBase()}${u}`);

export const DEFAULT_TIMEOUT = 90000;

// 网关层（nginx/Caddy）直接返回的非 JSON 错误页，需要翻译成人话，
// 否则用户只会看到“请求失败（HTTP 413）”这种无从下手的提示。
const GATEWAY_HINTS = {
  413: "图片体积过大，请压缩或换一张更小的图片后重试",
  502: "后端服务未就绪，请稍后重试",
  504: "后端响应超时，请稍后重试",
};

function abortMessage(err, timeout) {
  return err && err.name === "AbortError"
    ? `请求超时（超过 ${Math.round(timeout / 1000)} 秒未响应）`
    : "网络异常，请确认后端服务是否正常";
}

/**
 * 发起请求并解析统一信封 { code, message, data }。
 * 所有请求都带 AbortController 超时，避免后端挂死时前端无限等待。
 */
export async function fetchJson(path, { timeout = DEFAULT_TIMEOUT, ...opts } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let resp;
  try {
    resp = await fetch(api(path), { ...opts, signal: ctrl.signal });
  } catch (err) {
    throw new Error(abortMessage(err, timeout));
  } finally {
    clearTimeout(timer);
  }

  let payload = null;
  try {
    payload = await resp.json();
  } catch {
    /* 非 JSON 响应（网关错误页等） */
  }
  if (!payload || payload.code !== 0) {
    throw new Error(
      (payload && payload.message) ||
        GATEWAY_HINTS[resp.status] ||
        `请求失败（HTTP ${resp.status}）`
    );
  }
  return payload.data;
}

export const request = fetchJson;

export const postJson = (path, body, opts = {}) =>
  fetchJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    ...opts,
  });

/* ---------- 启动探测 ---------- */

async function healthOk(base, timeout = 3000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const r = await fetch(`${base}/api/v1/health`, { cache: "no-store", signal: ctrl.signal });
    const p = await r.json();
    return !!p && p.code === 0;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 探测后端地址：同源（docker nginx 代理）优先，失败则回退本地 8000 直连。
 * 每个探测点都有超时，避免后端不可达时首屏被拖住。
 */
export async function probeApiBase() {
  const current = getApiBase();
  // 同源（docker nginx 代理）优先，失败才回退本地 8000 直连。
  // 三个探测点**并发**发起：原先串行等待最坏要 3×3s=9s 才首屏可用，
  // 并发后只需最慢的一个（≤3s）。同源命中时优先级最高，故按顺序取第一个成功者。
  const candidates = [current, "http://localhost:8000", "http://127.0.0.1:8000"];
  const results = await Promise.all(candidates.map((b) => healthOk(b)));
  const hitIndex = results.findIndex(Boolean);
  if (hitIndex === -1) return current; // 都不可用：保持默认，后续请求会提示"后端未连接"
  const base = candidates[hitIndex];
  if (base !== current) setApiBase(base);
  return base;
}
