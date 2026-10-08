# 字体文件目录

将以下字体文件放到本目录，系统会自动加载（找不到时回退 PIL 默认字体，中文/阿拉伯文可能显示为空白）。

## 推荐字体
- 中文（CJK）：NotoSansCJK-Regular.ttc 或 SourceHanSansSC-Regular.otf
- 阿拉伯文：NotoSansArabic-Regular.ttf
- 拉丁：DejaVuSans.ttf（Linux 系统自带，可在 /usr/share/fonts/truetype/dejavu/ 找到）

## 下载
- Noto 系列：https://fonts.google.com/noto
- Source Han Sans：https://github.com/adobe-fonts/source-han-sans

程序按文件名自动识别（见 backend/app/image/fonts.py 中的候选列表）。
