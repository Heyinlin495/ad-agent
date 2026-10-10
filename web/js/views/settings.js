/* ============================================================
   系统设置：后端运行配置（只读）
   ============================================================ */
import { request } from "../core/api.js?v=7d6d0fde";
import { $, esc } from "../core/dom.js?v=7d6d0fde";

export async function loadSettings() {
  const grid = $("settingsGrid");
  try {
    const [health, settings] = await Promise.all([
      request("/health", { timeout: 10000 }),
      request("/settings", { timeout: 10000 }),
    ]);
    const rows = [
      ["应用", `${health.app} v${health.version}`],
      ["环境 / 模式", `${health.environment} · ${health.mock_mode ? "Mock" : "真实模型"}`],
      ["LLM", `${settings.llm_provider} / ${settings.llm_model}`],
      ["视觉模型", settings.vision_model || "-"],
      ["Embedding", `${settings.embedding_provider} / ${settings.embedding_model}`],
      ["向量库", settings.vector_store_type || "-"],
      ["图像生成", settings.image_provider || "-"],
      ["重排序", settings.rerank_provider || "-"],
      ["存储", settings.storage_type || "-"],
      ["上传上限", `${settings.max_upload_size_mb ?? "-"} MB`],
      ["服务状态", health.status === "ok" ? "● 正常" : "● 异常"],
    ];
    grid.innerHTML = rows
      .map(([k, v]) => `<div class="setting-item"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`)
      .join("");
  } catch (err) {
    grid.innerHTML = `<div class="side-empty">${esc(err.message)}</div>`;
  }
}
