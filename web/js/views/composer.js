/* ============================================================
   输入区：附件选择 / 非常规格式转换 / 自适应高度
   ============================================================ */
import { DEFAULT_MAX_FILE_MB, OK_IMAGE_EXTS } from "../constants.js?v=2c2bf4b0";
import { $, toast } from "../core/dom.js?v=2c2bf4b0";
import { state } from "../core/state.js?v=2c2bf4b0";

export const maxFileMb = () => state.maxFileMb || DEFAULT_MAX_FILE_MB;

/* ---------- 附件预览 ---------- */
export function clearAttach() {
  state.attached = null;
  $("attachPreview").hidden = true;
  $("attachImg").src = "";
  $("attachName").textContent = "";
  $("fileInput").value = "";
}

export function pickFile() {
  $("fileInput").click();
}

function setAttached(file, dataUrl, note) {
  state.attached = { file, dataUrl };
  $("attachImg").src = dataUrl;
  $("attachName").textContent = note || file.name;
  $("attachPreview").hidden = false;
  $("chatInput").focus();
}

/* ---------- 自适应高度 ---------- */
export function autoGrow() {
  const el = $("chatInput");
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 132)}px`;
}

/* ---------- 格式处理 ---------- */

/* 用 <img>+canvas 将非常规格式（如相机 MPO，取第一帧）转成 JPEG */
async function convertToJpeg(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.getContext("2d").drawImage(img, 0, 0);
    const blob = await new Promise((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("convert failed"))), "image/jpeg", 0.92)
    );
    const name = file.name.replace(/\.[^.]+$/, "") + ".jpg";
    return new File([blob], name, { type: "image/jpeg" });
  } finally {
    URL.revokeObjectURL(url);
  }
}

const fileExt = (file) => (file.name.split(".").pop() || "").toLowerCase();

export async function onFileChosen(file) {
  if (!file) return;
  if (file.size > maxFileMb() * 1024 * 1024) {
    toast(`图片超过 ${maxFileMb()}MB 限制`);
    return;
  }

  // 白名单格式：原样使用
  if (OK_IMAGE_EXTS.includes(fileExt(file))) {
    const reader = new FileReader();
    reader.onload = () => setAttached(file, reader.result);
    reader.readAsDataURL(file);
    return;
  }

  // 非常规格式（MPO/HEIC/BMP/GIF 等）：尝试转 JPEG（MPO 等相机格式通常能取到首帧）
  const ext = (fileExt(file) || file.type || "未知").toUpperCase();
  try {
    const converted = await convertToJpeg(file);
    const reader = new FileReader();
    reader.onload = () => setAttached(converted, reader.result, `${file.name}（已转 JPEG）`);
    reader.readAsDataURL(converted);
    toast(`已自动将 ${ext} 转换为 JPEG`);
  } catch {
    toast(`不支持的图片格式：${ext}，请改用 JPG/PNG/WebP`);
  }
}

export function initComposer() {
  $("btnAttach").onclick = pickFile;
  $("chipAttach").onclick = pickFile;
  $("btnRemoveAttach").onclick = clearAttach;
  $("fileInput").onchange = (e) => onFileChosen(e.target.files[0]);
  $("chatInput").addEventListener("input", autoGrow);
  autoGrow();
}
