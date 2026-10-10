/* ============================================================
   业务常量（与后端枚举及原 Streamlit 前端保持一致）
   ============================================================ */

export const COUNTRIES = {
  US: "美国", JP: "日本", DE: "德国", FR: "法国",
  ES: "西班牙", KR: "韩国", SA: "沙特阿拉伯", CN: "中国",
};

export const LANGUAGE_NAMES = {
  en: "英语", zh: "中文", ja: "日语", ko: "韩语",
  de: "德语", fr: "法语", es: "西班牙语", ar: "阿拉伯语",
};

export const PLATFORMS = ["Amazon", "Meta", "TikTok", "Google", "独立站"];

export const STYLES = { promo: "促销", premium: "高级", humor: "幽默", emotion: "情感", minimal: "极简" };

export const SIZES = {
  "1:1": "1:1 (1080×1080)",
  "4:5": "4:5 (1080×1350)",
  "9:16": "9:16 竖版 (1080×1920)",
  "16:9": "16:9 横版 (1920×1080)",
  amazon: "Amazon 主图 (2000×2000)",
};

/** 需要 RTL 排版的投放语言 */
export const RTL_LANGUAGES = new Set(["ar"]);

/** 后端 LangGraph 节点（英文 key）→ 中文显示名（七阶段生图流水线） */
export const FLOW_NODES = [
  ["analyze_image", "产品分析"],
  ["market_strategy", "营销定位"],
  ["sell_points", "卖点提炼"],
  ["ad_plan", "广告策划"],
  ["compliance_review", "合规审查"],
  ["prompt_gen", "提示词生成"],
  ["image_compose", "图片生成"],
];

export const NODE_NAMES = Object.fromEntries(FLOW_NODES);

/**
 * 流水线节点 → 步骤条下标（每个节点各占一格，共 7 格）。
 * 不再把多个节点压成一个宏观阶段：让「每个节点」都有独立的进度格。
 */
export const NODE_INDEX = Object.fromEntries(FLOW_NODES.map(([key], i) => [key, i]));

/** 广告图重生成的风格快捷键 */
export const REGEN_HINTS = [
  "清新简约浅色背景",
  "深色高级感，冷色调",
  "节日促销氛围，暖色",
  "杂志大片质感",
  "留白多、极简",
];

/** 后端按扩展名校验，故白名单也以扩展名为准（MIME 仅作参考，MPO 常被标为 image/jpeg） */
export const OK_IMAGE_EXTS = ["jpg", "jpeg", "png", "webp"];

/** 后端 /settings 未取到时的兜底上传上限（MB） */
export const DEFAULT_MAX_FILE_MB = 50;
