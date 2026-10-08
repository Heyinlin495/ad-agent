/**
 * AdAgent Web 前端配置（ES Module）
 *
 * API_BASE 为空字符串时：优先走当前站点（docker 中由 nginx 反向代理到后端）；
 * 若同源探测失败（本地静态服务器直连场景），启动时会自动回退到
 * http://localhost:8000 直连后端。
 * 也可在浏览器控制台手动指定：setApiBase('http://192.168.x.x:8000')
 */
export const DEFAULT_API_BASE = "";

let apiBase = DEFAULT_API_BASE;

export function getApiBase() {
  return apiBase;
}

export function setApiBase(base) {
  apiBase = base || "";
  if (typeof window !== "undefined") window.API_BASE = apiBase;
  return apiBase;
}
