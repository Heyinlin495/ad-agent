/* ============================================================
   知识库：文档列表 / 上传入库 / 检索测试 / 删除
   ============================================================ */
import { fetchJson, postJson, request } from "../core/api.js?v=2c2bf4b0";
import { $, esc, toast } from "../core/dom.js?v=2c2bf4b0";
import { appConfirm } from "../core/modal.js?v=2c2bf4b0";

let kbDocInstalled = false;

/** 文档列表删除按钮改为在 #kbDocList 上做容器级事件委托，重建 innerHTML 后无需重绑 */
function installKbDocDelegate() {
  if (kbDocInstalled) return;
  const root = $("kbDocList");
  if (!root) return;
  kbDocInstalled = true;
  root.addEventListener("click", (e) => {
    const el = e.target.closest('[data-action="kb-del"]');
    if (!el || !root.contains(el)) return;
    deleteKbDoc(Number(el.dataset.id), el);
  });
}

/** 拉取文档列表。返回是否成功（成功即视为已加载，供壳层避免重复导航重拉）。 */
export async function loadKbDocs() {
  const box = $("kbDocList");
  try {
    const docs = await request("/kb/documents", { timeout: 20000 });
    if (!docs.length) {
      box.innerHTML = `<div class="side-empty">知识库为空，可上传文档入库</div>`;
      return true;
    }
    box.innerHTML = docs.map((d) => `
      <div class="kb-doc">
        <div>
          <div class="doc-title">${esc(d.title)}</div>
          <div class="doc-meta">#${d.id} · ${esc(d.source_type)} · ${esc(d.country || "-")}/${esc(d.platform || "-")} · ${d.chunk_count} 块 · ${esc(d.status)}</div>
        </div>
        <button class="btn danger doc-del" type="button" data-action="kb-del" data-id="${d.id}" aria-label="删除文档 ${esc(d.title)}">删除</button>
      </div>`).join("");
    return true;
  } catch (err) {
    box.innerHTML = `<div class="side-empty">${esc(err.message)}</div>`;
    return false;
  }
}

async function deleteKbDoc(id, btn) {
  const okToDelete = await appConfirm(`确认删除文档 #${id}？`, { title: "删除知识库文档", okText: "删除", danger: true });
  if (!okToDelete) return;
  btn.disabled = true;
  try {
    await request(`/kb/documents/${id}`, { method: "DELETE" });
    toast("已删除");
    loadKbDocs();
  } catch (err) {
    btn.disabled = false;
    toast(err.message);
  }
}

export async function uploadKbDoc() {
  const fileInput = $("kbFile");
  const file = fileInput.files[0];
  if (!file) {
    toast("请先选择文件");
    return;
  }

  const btn = $("btnKbUpload");
  const fd = new FormData();
  fd.append("file", file);
  fd.append("title", $("kbTitle").value.trim());
  fd.append("country", $("kbCountry").value.trim());
  fd.append("platform", $("kbPlatform").value.trim());

  btn.disabled = true;
  btn.textContent = "上传中…";
  try {
    const data = await fetchJson("/kb/documents", { method: "POST", body: fd, timeout: 180000 });
    toast(`已入库：${data.chunk_count} 块`);
    fileInput.value = "";
    $("kbTitle").value = "";
    $("kbCountry").value = "";
    $("kbPlatform").value = "";
    loadKbDocs();
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "上传入库";
  }
}

export async function searchKb() {
  const query = $("kbQuery").value.trim();
  if (!query) {
    toast("请输入检索词");
    return;
  }
  const box = $("kbSearchResults");
  const btn = $("btnKbSearch");
  box.innerHTML = `<div class="side-empty">检索中…</div>`;
  btn.disabled = true;
  try {
    const data = await postJson("/kb/search", { query, top_k: 5 }, { timeout: 60000 });
    const hits = (data && (data.results || data.hits || data)) || [];
    const arr = Array.isArray(hits) ? hits : [];
    if (!arr.length) {
      box.innerHTML = `<div class="side-empty">无匹配结果</div>`;
      return;
    }
    box.innerHTML = arr.map((h) => {
      const content = h.content || "";
      return `
        <div class="kb-hit">
          <span class="hit-title">${esc(h.title || `文档 #${h.document_id}`)}</span>
          ${h.score != null ? `<span class="hit-score">${Number(h.score).toFixed(3)}</span>` : ""}
          <div class="hit-content">${esc(content.slice(0, 160))}${content.length > 160 ? "…" : ""}</div>
        </div>`;
    }).join("");
  } catch (err) {
    box.innerHTML = `<div class="side-empty">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

export function initKb() {
  installKbDocDelegate();
  $("btnKbUpload").onclick = uploadKbDoc;
  $("btnKbSearch").onclick = searchKb;
  $("kbQuery").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) searchKb();
  });
}
