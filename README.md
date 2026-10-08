# 跨境电商 AI 广告生成 Agent

上传/拍摄产品照片，系统自动识别产品，结合知识库（RAG）与目标市场信息，生成多语言广告文案 + 一张平面广告图，可一键下载。

## 功能概览

- **产品图预处理**：格式校验、EXIF 旋转、压缩、背景去除（rembg）、质量检测（模糊/过暗/主体过小）
- **产品识别**：多模态 LLM 输出结构化 JSON（品类/卖点/目标人群/风险标记），可在线编辑
- **RAG 知识库**：文档解析（PDF/Word/TXT/MD/CSV）、混合检索（向量 + BM25 + Rerank）、元数据过滤、引用来源
- **Agent 编排**：LangGraph 7 阶段流水线（产品分析 → 营销定位 → 卖点提炼 → 广告策划 → 合规审查 → 提示词生成 → 图片生成）+ 合规审查回环 + SSE 进度流
- **广告文案**：3 个风格版本、卖点只来自「卖点提炼」结果、平台适配（Amazon 五点 / Google RSA 15+4 / TikTok 口播）、多语言本地化
- **广告图生成**：AI 生成背景 + Pillow 精确合成（主体 100% 保真、等比缩放），hero 大特写版式，背景/配色/强调色/图标按品类自适应，多尺寸预设
- **Web 前端**：零构建原生 ES Modules 单页应用（广告生成 / 知识库 / 系统设置 / 项目说明）

## 架构

```
Web 前端 (零构建 ESM) ⇄ FastAPI (/api/v1) ⇄ Services 业务层
                                    ├─ Agents 编排层 (LangGraph)
                                    ├─ RAG 层 (Chroma/Milvus + BM25 + Rerank)
                                    ├─ Image 图像层 (去背景/生成/合成/模板)
                                    └─ LLM 客户端 (GPT-4o/Claude/Qwen-VL/Gemini/mock)
存储：SQLite/PostgreSQL + Redis + 文件/S3/MinIO + 向量库
```

## 目录结构

```
ad-agent/
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── main.py
│   │   ├── api/v1/          # 路由（products/ads/kb/history/health）
│   │   ├── core/            # 配置、日志、异常、数据库、限流
│   │   ├── models/          # SQLAlchemy ORM
│   │   ├── schemas/         # Pydantic v2
│   │   ├── services/        # 业务逻辑 + LLM 客户端
│   │   ├── agents/          # LangGraph 节点、工具、图
│   │   ├── rag/             # 切分、向量库、检索、rerank
│   │   ├── image/           # 去背景、生成、合成、模板、质检
│   │   └── prompts/         # 提示词模板（独立 .txt）
│   ├── tests/               # pytest
│   └── requirements.txt
├── web/                     # 千问风格聊天式 Web 前端（零构建原生 ESM，nginx 反代）
│   ├── index.html           # 单页应用：侧边栏 + 聊天主区 + 底部输入框
│   ├── css/style.css        # 设计系统（千问风格浅色主题）
│   ├── js/main.js           # 入口：模块装配 / 事件绑定 / 启动探测
│   ├── js/shell.js          # 视图切换与侧边栏
│   ├── js/config.js         # 后端地址配置（自动探测同源/本地 8000）
│   ├── js/constants.js      # 业务常量（国家/语言/平台/风格/尺寸/流程节点）
│   ├── js/core/             # api（超时 + 错误信封）/ dom / state / modal / wiring
│   ├── js/views/            # composer / chat / history / kb / settings
│   ├── tools/               # stamp.mjs（版本戳）· check.mjs（模块链接）· dom-test.mjs（jsdom 集成测试）
│   ├── Dockerfile           # nginx 部署
│   └── nginx.conf           # /api、/files 反代 + SSE 关闭缓冲 + 缓存策略
├── data/knowledge_seed/     # 内置示例知识库（自动灌入）
├── assets/fonts/            # 字体（CJK/阿拉伯文）
├── docker-compose.yml
├── .env.example
└── README.md
```

### 前端开发约定（零构建 ESM）

前端不走打包器，改完代码后执行一次自检即可：

```bash
cd web
node tools/stamp.mjs      # 按内容哈希刷新 index.html 与 import 的 ?v= 版本戳
node tools/check.mjs      # 校验所有 import 路径与具名导出是否匹配（等价于链接期检查）
node tools/dom-test.mjs   # jsdom 集成测试：上传/参数/进度/并发/取消/弹窗/转义（需 jsdom）
```

`?v=` 由脚本统一维护，**不需要再手工 bump 版本号**（浏览器缓存旧代码的问题由此消除）。

## 环境准备

- Python 3.11
- Docker + docker-compose（可选）

## 快速开始

### 方式一：Docker（一条命令）

```bash
cd ad-agent
cp .env.example .env          # 按需修改
docker-compose up --build
```

- 前端：http://localhost:8502
- 后端 API 文档：http://localhost:8000/docs

> 开发模式默认开启热重载（`--reload`）并挂载源码目录进容器。

#### 生产部署（叠加 prod 覆盖层）

开发模式下 `docker-compose.yml` 是「源码挂载 + `--reload`」。生产环境建议叠加
`docker-compose.prod.yml` 覆盖层：关闭热重载、不挂载源码、并一并启用 Postgres
与 MinIO 替代 SQLite / 本地文件存储：

```bash
cd ad-agent
cp .env.example .env          # 按需修改
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    --profile prod up -d --build
```

- 现在跑的是**镜像内的最终代码**，不再是热重载的挂载源码。
- `--profile prod` 会一并拉起 Postgres 与 MinIO；`DATABASE_URL` /
  `STORAGE_TYPE` 已在覆盖层中切换，无需手动改 `.env`。
- 停掉：`docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile prod down`

### 方式二：本地运行

```bash
cd ad-agent/backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

# 完整依赖（含 RAG/rembg/BGE）
pip install -r requirements.txt
# 或仅核心依赖（Mock 演示，无需 GPU/重模型）
pip install -r requirements-core.txt

# 初始化环境变量
cd ..
cp .env.example .env

# 启动后端
cd backend
uvicorn app.main:app --reload --port 8000
```

另开一个终端启动前端（或使用根目录 `start.bat` / `start.sh` 一键启动后端 + 前端）：

```bash
cd ad-agent/web
python -m http.server 8502
```

> 无 API Key 时默认 `MOCK_MODE=true`，全流程可跑通（使用示例数据 + 渐变背景兜底）。

## 环境变量说明

见 `.env.example`，关键项：

| 变量 | 说明 | 默认 |
|---|---|---|
| `MOCK_MODE` | 无 Key 演示模式 | `true` |
| `LLM_PROVIDER` | openai/claude/qwen/gemini/kimi/mock | `mock` |
| `LLM_MODEL` / `LLM_API_KEY` | 模型名与密钥 | 空 |
| `LLM_MAX_RPM` | 客户端限流（每分钟请求数上限，0=不限流） | `0` |
| `LLM_THINKING` | 思考模式开关（仅 kimi-k2.x 等生效）：空/enabled/disabled | 空 |
| `LLM_REASONING_EFFORT` | 推理强度（仅 kimi-k3 生效）：low/high/max | 空 |
| `DASHSCOPE_API_KEY` | Embedding 与通义万相出图密钥（与对话模型解耦） | 空 |
| `EMBEDDING_PROVIDER` | openai/bge-m3/dashscope/mock | `mock` |
| `VECTOR_STORE_TYPE` | chroma/milvus/mock | `chroma` |
| `IMAGE_PROVIDER` | openai/dashscope/sd/flux/mock | `mock` |
| `DATABASE_URL` | SQLite/PostgreSQL 连接串 | SQLite |
| `REDIS_URL` | 缓存与队列 | 本地 |
| `STORAGE_TYPE` | local/s3/minio | `local` |

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/products/analyze` | 上传产品图，识别 + 质检 |
| GET/PUT | `/api/v1/products/{id}` | 查看 / 编辑产品 |
| POST | `/api/v1/ads/generate` | 创建生成任务，返回 task_id |
| GET | `/api/v1/ads/{task_id}` | 查询状态与结果 |
| GET | `/api/v1/ads/{task_id}/stream` | SSE 进度流 |
| POST | `/api/v1/ads/{task_id}/regenerate` | 局部重生成 |
| POST | `/api/v1/kb/documents` | 上传知识库文档 |
| GET/DELETE | `/api/v1/kb/documents/{id}` | 文档管理 |
| POST | `/api/v1/kb/search` | 检索测试 |
| GET | `/api/v1/history` | 历史记录 |
| GET | `/api/v1/metrics` | 运行指标 JSON 快照（任务/LLM 成本） |
| GET | `/api/v1/metrics/prometheus` | Prometheus 文本格式指标（可接入 Grafana） |

## 运行测试

```bash
cd backend
pytest
```

测试使用 Mock 模式 + 临时目录，无需任何 API Key。

## 常见问题

**Q：中文/阿拉伯文在广告图中显示为空白或方框？**
A：需将字体放入 `assets/fonts/`（见该目录 README）。找不到时回退 PIL 默认字体。

**Q：rembg 去背景很慢或失败？**
A：首次调用会下载模型；失败时自动降级为原图（白色背景），不影响流程。

**Q：切换真实模型后如何配置？**
A：在 `.env` 中设置 `MOCK_MODE=false`、`LLM_PROVIDER`、`LLM_API_KEY`（及对应 `LLM_BASE_URL`），重启后端即可。所有提供商均走 OpenAI 兼容端点。

示例（月之暗面 Kimi）：

```bash
LLM_PROVIDER=kimi
LLM_MODEL=kimi-k2.6          # 原生多模态，可关闭思考模式；追求更强推理可换 kimi-k3
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.moonshot.cn/v1
LLM_TEMPERATURE=1            # kimi-k3 / kimi-k2.x 只接受 1，客户端对这类模型会自动省略该参数
LLM_THINKING=disabled        # 关闭思考：更快更省；设为 enabled 提升质量
LLM_MAX_RPM=3                # 上游账号的组织级 RPM 上限，客户端据此限流
DASHSCOPE_API_KEY=sk-yyy     # Embedding 与出图仍走阿里云，需单独配置
```

注意事项：
- 换用第三方对话模型后，`LLM_API_KEY` 不再被 RAG 检索与出图使用，请务必配置 `DASHSCOPE_API_KEY`（或改用其他 Embedding/图像提供商），否则知识库会降级为本地 mock、出图回退纯色背景。
- 若上游账号有 RPM 上限（Kimi 新账号默认组织级 3），务必设置 `LLM_MAX_RPM`，否则流水线连续调用会大量触发 429。

**Q：生产环境如何切换 PostgreSQL / Milvus / S3？**
A：修改 `.env` 的 `DATABASE_URL`、`VECTOR_STORE_TYPE=milvus` + `MILVUS_URI`、`STORAGE_TYPE=s3` 并填入对应密钥。

## 里程碑

| 阶段 | 状态 |
|---|---|
| M0 基础骨架 | ✅ |
| M1 产品识别 | ✅ |
| M2 RAG 知识库 | ✅ |
| M3 Agent 编排 | ✅ |
| M4 文案生成 | ✅ |
| M5 图片生成/合成 | ✅ |
| M6 Web 前端 | ✅ |
| M7 部署与测试 | ✅ |

## 已知限制

- 异步任务默认使用 FastAPI BackgroundTasks（单进程内）；多进程/分布式请启用 Celery + Redis。
- Milvus、BGE-M3、真实图像生成需额外服务/模型，均提供降级兜底。
- 阿拉伯文 RTL 依赖 arabic-reshaper + python-bidi（已列入依赖）。
