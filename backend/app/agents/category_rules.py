"""品类 → 背景/灯光适配规则。

从 nodes.py 抽出，便于独立维护与单测。规则表同时覆盖：
① 视觉模型输出的英文粗分类（Consumer Electronics / Household Goods 等）
② 常见产品名词（中英）③ 中文品类。
ASCII 关键词用单词边界匹配，避免 ring 误匹配 spring。
"""
from __future__ import annotations

import re
from typing import Any

CATEGORY_STYLE_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("headphone", "earphone", "earbud", "headset", "speaker", "soundbar", "amplifier",
         "phone", "smartphone", "camera", "webcam", "watch", "smartwatch", "charger",
         "power bank", "keyboard", "mouse", "laptop", "notebook", "computer", "tablet",
         "monitor", "display", "television", "tv", "projector", "router", "drone",
         "consumer electronics", "electronics", "gadget",
         "耳机", "音箱", "手机", "相机", "手表", "充电", "键盘", "鼠标", "电脑", "平板",
         "显示器", "电视", "投影", "数码", "电子", "智能设备"),
        "该产品属于【数码电子】：背景用深色吸音棉/几何纹理墙/深色金属拉丝/冷灰科技感渐变，"
        "冷色轮廓光 + 品牌色光带点缀，戏剧性侧光突出金属与塑料质感；product_placement.scale 0.70-0.80。",
    ),
    (
        ("cream", "serum", "lipstick", "lip gloss", "perfume", "fragrance", "skincare",
         "cosmetic", "makeup", "beauty", "lotion", "moisturizer", "shampoo", "conditioner",
         "mask", "sunscreen", "essence",
         "面霜", "精华", "口红", "唇膏", "香水", "护肤", "化妆", "美妆", "乳液", "洗发",
         "面膜", "防晒", "彩妆", "个护"),
        "该产品属于【美妆个护】：背景用高级大理石/亚克力台面或柔光浅色系空间，珠光/水珠/丝绸质感点缀，"
        "柔光或环形光显肤质与瓶身玻璃/金属质感；product_placement.scale 0.60-0.75。",
    ),
    (
        ("jewelry", "jewellery", "ring", "necklace", "earring", "bracelet", "bangle",
         "pendant", "diamond", "gemstone", "glasses", "sunglasses", "eyewear",
         "珠宝", "首饰", "戒指", "项链", "耳环", "手镯", "吊坠", "钻石", "眼镜", "墨镜"),
        "该产品属于【珠宝首饰/眼镜】：背景用高级摄影棚/黑色镜面/深色丝绒，刻意反射与极窄景深，"
        "聚光灯突出金属与宝石折射；product_placement.scale 0.55-0.70。",
    ),
    (
        ("shoe", "sneaker", "boot", "sandal", "slipper", "shirt", "tshirt", "t-shirt",
         "dress", "jacket", "coat", "pants", "jeans", "sweater", "hoodie", "hat", "cap",
         "scarf", "bag", "backpack", "handbag", "tote", "wallet", "purse", "luggage",
         "suitcase", "apparel", "clothing", "fashion", "garment",
         "鞋", "球鞋", "靴", "服装", "外套", "裙", "裤", "帽", "围巾", "包", "背包",
         "手提", "钱包", "行李箱", "服饰"),
        "该产品属于【鞋服箱包】：背景用摄影棚纯净背景/深色织物/混凝土 + 戏剧性侧光，严禁人物，"
        "可用极简几何装饰；product_placement.scale 0.65-0.80。",
    ),
    (
        ("food", "snack", "coffee", "tea", "drink", "beverage", "juice", "wine", "beer",
         "chocolate", "candy", "honey", "sauce", "jam", "cereal", "nuts", "spice",
         "supplement", "vitamin", "food & beverage",
         "食品", "零食", "咖啡", "茶", "饮料", "果汁", "酒", "巧克力", "糖果", "蜂蜜",
         "酱", "坚果", "保健品", "维生素"),
        "该产品属于【食品饮料】：背景用高级餐桌/深色木质/石板桌面 + 暖色聚光或金色时刻光线，"
        "水珠/新鲜食材元素可点缀（保持中央留白）；product_placement.scale 0.60-0.75。",
    ),
    (
        ("bottle", "water bottle", "cup", "mug", "tumbler", "flask", "thermos", "kettle",
         "teapot", "pitcher", "glassware", "container", "lunch box", "kitchen",
         "cookware", "pan", "pot", "utensil", "cutlery", "plate", "bowl", "household",
         "home goods", "home & kitchen", "cleaner", "detergent", "storage",
         "水杯", "杯子", "保温杯", "水壶", "锅", "餐具", "厨具", "厨房", "家居",
         "日用品", "收纳", "清洁"),
        "该产品属于【家居日用/厨具水具】：背景用干净高级台面/简约生活空间 + 柔光或暖光，"
        "可用纯净渐变或大理石台面衬托，保持画面中央留白；product_placement.scale 0.60-0.75。",
    ),
    (
        ("baby", "infant", "toddler", "toy", "kids", "children", "diaper", "stroller",
         "pacifier", "nursery",
         "婴儿", "宝宝", "母婴", "玩具", "儿童", "尿布", "童车", "奶瓶"),
        "该产品属于【母婴玩具】：背景用明亮浅色/干净地毯/桌面平面 + 柔光，避免深色压抑；"
        "product_placement.scale 0.60-0.75。",
    ),
    (
        ("pet", "dog", "cat", "aquarium", "宠物", "狗", "猫", "鱼缸"),
        "该产品属于【宠物用品】：背景用现代家庭干净空间/浅色桌面 + 柔光，严禁出现动物，"
        "保持中央留白；product_placement.scale 0.55-0.70。",
    ),
    (
        ("furniture", "sofa", "couch", "bed", "mattress", "table", "desk", "chair",
         "lamp", "lighting", "cushion", "pillow", "rug", "curtain",
         "家具", "沙发", "床", "桌", "椅", "灯", "床垫", "地毯", "窗帘"),
        "该产品属于【家居家具】：允许真实室内/建筑场景（产品即场景主体），保持摆放平面真实合理，"
        "画面中央仍须干净无大型杂物；product_placement.scale 0.60-0.75。",
    ),
    (
        ("appliance", "vacuum", "blender", "mixer", "fan", "heater", "air purifier",
         "humidifier", "iron", "refrigerator", "microwave", "oven", "toaster",
         "家电", "吸尘", "搅拌", "风扇", "加湿", "空气净化"),
        "该产品属于【小家电】：背景用高级现代厨房/生活空间或摄影棚渐变，冷色或中性光突出金属与塑料质感；"
        "product_placement.scale 0.60-0.75。",
    ),
    (
        ("fitness", "sport", "yoga", "dumbbell", "barbell", "gym", "bicycle", "bike",
         "tent", "camping", "hiking", "outdoor", "tool", "drill", "wrench", "hardware",
         "运动", "健身", "瑜伽", "哑铃", "单车", "户外", "露营", "登山", "工具", "五金"),
        "该产品属于【运动户外/工具】：背景用深色工业/建筑空间或专业工作室，体积光/聚光灯，"
        "突出功能质感；product_placement.scale 0.60-0.75。",
    ),
]


def kw_match(keyword: str, text: str) -> bool:
    """关键词匹配：ASCII 用单词边界 + 可选复数（避免 ring 误匹配 spring，同时命中 headphones）。

    "headphone" 需能命中 "headphones"，"watch" 需能命中 "watches"，
    否则复数品名会漏配品类规则、标签与图标。
    """
    if keyword.isascii():
        return re.search(rf"\b{re.escape(keyword)}(?:s|es)?\b", text) is not None
    return keyword in text


# 品类 → 右上角英文标签 / 图标形状（供排版合成器使用，让不同品类视觉元素不同）
_CATEGORY_TAG_RULES: list[tuple[tuple[str, ...], str]] = [
    (("headphone", "earphone", "earbud", "headset", "speaker", "soundbar", "amplifier", "耳机", "音箱"), "AUDIO"),
    (("phone", "smartphone", "tablet", "laptop", "computer", "monitor", "television", "tv", "projector", "手机", "电脑", "平板", "电视", "投影"), "ELECTRONICS"),
    (("consumer electronics", "electronics", "gadget", "数码", "电子"), "ELECTRONICS"),
    (("camera", "webcam", "drone", "相机", "无人机"), "OPTICS"),
    (("watch", "smartwatch", "手环", "手表"), "WEARABLE"),
    (("charger", "power bank", "keyboard", "mouse", "router", "cable", "充电", "键盘", "鼠标", "路由"), "ACCESSORIES"),
    (("cream", "serum", "lipstick", "perfume", "skincare", "cosmetic", "makeup", "shampoo", "面霜", "精华", "口红", "香水", "护肤", "化妆", "美妆"), "BEAUTY"),
    (("jewelry", "necklace", "earring", "bracelet", "gemstone", "glasses", "sunglasses", "珠宝", "首饰", "眼镜", "墨镜"), "JEWELRY"),
    (("shoe", "sneaker", "shirt", "dress", "jacket", "bag", "backpack", "wallet", "luggage", "鞋", "服装", "包", "行李箱"), "APPAREL"),
    (("food", "snack", "coffee", "tea", "drink", "juice", "chocolate", "supplement", "食品", "零食", "咖啡", "饮料", "保健品"), "FOOD"),
    (("bottle", "cup", "mug", "kettle", "cookware", "kitchen", "水杯", "保温杯", "厨具", "厨房"), "HOME & KITCHEN"),
    (("baby", "toy", "diaper", "stroller", "婴儿", "母婴", "玩具"), "BABY & KIDS"),
    (("pet", "dog", "cat", "aquarium", "宠物", "猫", "狗"), "PET"),
    (("sofa", "bed", "table", "desk", "chair", "lamp", "沙发", "床", "桌", "椅", "家具"), "FURNITURE"),
    (("vacuum", "blender", "fan", "humidifier", "air purifier", "refrigerator", "吸尘", "风扇", "加湿", "家电"), "APPLIANCE"),
    (("fitness", "yoga", "camping", "bicycle", "tent", "tool", "运动", "健身", "露营", "户外", "工具"), "OUTDOOR"),
]

# 音频类产品使用声波柱状图标，其余品类使用通用对勾图标
_AUDIO_ICON_KEYWORDS = ("headphone", "earphone", "earbud", "headset", "speaker",
                        "soundbar", "amplifier", "audio", "耳机", "音箱", "音响", "音频")


def category_tag(product: dict[str, Any]) -> str:
    """右上角品类标签：优先按关键词映射为英文短标签；否则用品类原文（ASCII 才大写）。"""
    text = " ".join([
        str(product.get("category") or ""),
        str(product.get("name") or ""),
    ]).lower()
    for keywords, tag in _CATEGORY_TAG_RULES:
        if any(kw_match(k, text) for k in keywords):
            return tag
    category = (product.get("category") or "").strip()
    if category and category.isascii() and len(category) <= 18:
        return category.upper()
    return ""


# 穿戴类服装：模特实拍时人与服装/手机遮挡粘连，抠图难以干净分离，
# 改走"根据原图生成"（图生图指令编辑）模式。不含鞋/包/箱等可干净抠图的品类。
_WORN_APPAREL_KEYWORDS = (
    "jacket", "coat", "shirt", "tshirt", "t-shirt", "blouse", "dress", "skirt",
    "pants", "trousers", "jeans", "sweater", "hoodie", "suit", "blazer",
    "cardigan", "vest", "apparel", "clothing", "garment", "outfit",
    "服装", "外套", "夹克", "大衣", "风衣", "衬衫", "衬衣", "裙", "裤",
    "毛衣", "卫衣", "西装", "上衣", "套装", "礼服", "针织衫",
)


def is_worn_apparel(product: dict[str, Any]) -> bool:
    """是否为穿戴类服装（模特实拍图不宜抠图，应基于原图生成广告视觉）。"""
    text = " ".join([
        str(product.get("category") or ""),
        str(product.get("name") or ""),
        str(product.get("material") or ""),
    ]).lower()
    return any(kw_match(k, text) for k in _WORN_APPAREL_KEYWORDS)


def product_icon(product: dict[str, Any]) -> str:
    """底部图标行的图标形状：音频类 'bars'，其余 'check'。"""
    text = " ".join([
        str(product.get("category") or ""),
        str(product.get("name") or ""),
        " ".join(str(s) for s in product.get("selling_points", [])[:3]),
    ]).lower()
    return "bars" if any(kw_match(k, text) for k in _AUDIO_ICON_KEYWORDS) else "check"


def category_style_hint(product: dict[str, Any]) -> str:
    """按产品品类/名称关键词返回显式风格适配指令；未命中返回空串（由 LLM 自行推理）。"""
    text = " ".join([
        str(product.get("category") or ""),
        str(product.get("name") or ""),
        " ".join(str(s) for s in product.get("selling_points", [])[:3]),
    ]).lower()
    for keywords, hint in CATEGORY_STYLE_RULES:
        if any(kw_match(k, text) for k in keywords):
            return hint
    return ""
