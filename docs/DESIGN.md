# patent-kb 设计文档

**本地优先的专利咨询项目知识库工具 · Phase 1 MVP**

- 形态：Python FastAPI 单应用 + SQLite（FTS5）+ 单文件原生 HTML 前端
- 部署：`uv run patentkb` 一条命令启动，网页三步向导完成设置
- 核心卖点：所有 LLM 生成内容（关键词扩展 / IPC·CPC 分类号扩展及解释 / 专利检索式）均带可点击的原文来源锚点，后端做引用合法性校验，透明区分"原文验证"与"AI 推断"

---

## 1. 产品功能范围

### 1.1 MVP 做什么

| 功能 | 说明 |
|---|---|
| 首次设置向导 | 网页三步：选知识库文件夹 → 填 LLM API（base_url/api_key/model）→ 首次索引 |
| 文件浏览 | 目录树浏览知识库，chunk 文本预览、下载、在资源管理器中定位 |
| 关键词检索 | SQLite FTS5 全文检索（jieba 预分词），零 token 成本，返回高亮 snippet + 定位信息（页码/sheet/幻灯片/段落） |
| 需求分析扩展 | 输入新需求 → 检索 top-N 相关片段 → LLM 生成关键词扩展、IPC/CPC 分类号扩展及解释、专利检索式；每项带来源锚点 |
| 增量索引 | mtime+size 增量扫描，解析结果零重复成本 |
| 成本控制 | 需求 hash 缓存、执行前 token 预估、每日调用上限、轻量/主力双模型路由 |
| 索引与用量管理 | 失败文件清单、索引状态、用量统计 |

### 1.2 MVP 明确不做

- embedding 语义检索（配置占位默认关）
- 图片内容默认解析（仅文件名索引；vision 识图配置项默认关，接口预留）
- OCR、向量库、前端构建工具链
- 老格式 doc/xls/ppt 解析（标记 unsupported）
- 多用户鉴权启用、远程访问（host 固定 127.0.0.1；auth_token 配置位与中间件钩子已预留 = Phase 2 占位）

---

## 2. 用户流程

### 2.1 首次设置

```
uv run patentkb → 浏览器打开 http://127.0.0.1:8322
  ① 输入知识库文件夹绝对路径 → 后端校验存在/可写 → 显示可索引文件统计预览
  ② 填 base_url / api_key / model（可选 lite_model）→ [测试连接] 发最小请求验证
  ③ [创建知识库并首次索引] → 进度条 → 进入主界面
```

### 2.2 日常检索（零成本）

输入关键词 → 文件卡片列表（文件名/路径/类型徽章/anchor"Sheet1 第 2-40 行"）+ 高亮 snippet + [查看原文][下载][打开所在文件夹]。

### 2.3 需求分析（LLM 按次触发）

输入新需求描述 → [分析] → 先显示预估 token 与今日用量 → 确认后执行 → 结果三块：

1. **关键词 chips**：绿色"原文验证"（词确实出现在来源 chunk）/ 黄色"AI 推断"（引用有效但原文未见）→ 点击弹出来源 chunk 原文 + 文件路径链接
2. **分类号卡片**：IPC/CPC 码 + 解释，同样双色徽标 + 可点击引用
3. **检索式**：代码块 + [复制] + 括号/引号配对自检状态 + 依据说明

底部显示本次 token 用量、缓存命中与否、今日已用次数。

---

## 3. 核心模块

```
patentkb/
├── main.py        # 入口：组装 app、uvicorn 127.0.0.1:8322
├── config.py      # config.json 读写、dataclass、掩码回显
├── db.py          # sqlite3 + 写锁 + 建表迁移（PRAGMA user_version）
├── fts.py         # jieba 分词、FTS 查询构建（AND→OR 降级）、snippet、引用校验归一化
├── indexer.py     # 增量扫描、解析调度、切块入三表
├── llm.py         # OpenAI 兼容客户端、缓存、usage_log、限额、token 估算
├── analyzer.py    # 编排：检索→编号喂数→LLM→JSON 解析→引用校验
├── prompts.py     # prompt 模板 + PROMPT_VERSION（参与缓存键）
├── parsers/       # docx / xlsx+csv / pdf(文字层预检) / pptx / text / 图片伪chunk
├── routes/        # wizard / browse / search / analyze / files / admin
└── static/index.html  # 单文件前端，无构建无 CDN
```

接口约定：

- `parse(abs_path) -> (chunks: list[Chunk], status, error)`；`Chunk = (content, anchor)`
- `scan(kb_path, full) -> ScanReport(added/updated/removed/failed/skipped)`
- `search(query, limit) -> list[Hit(chunk_id, file_id, snippet, rank)]`
- `analyze(request_text, use_lite) -> AnalyzeResult(keywords, classifications, patent_query, usage)`

---

## 4. 数据结构

### 4.1 SQLite（`<知识库>/.kbindex/index.db`，随文件夹整体迁移）

```sql
files(id, rel_path UNIQUE, filename, ext, mtime, size,
      status,            -- pending|ok|failed|skipped_no_text|unsupported|truncated
      error_msg, parsed_at, meta_json)

chunks(id, file_id→files, chunk_index, content, anchor_json, char_count)
-- anchor: {"type":"pdf","page":[3,5]} / {"type":"xlsx","sheet":"Sheet1","rows":[2,40]}
--          {"type":"pptx","slide":[1,4]} / {"type":"docx","para":[10,58]}

fts_chunks  -- FTS5 虚拟表：tokens(jieba 分词空格连接) + chunk_id/file_id(UNINDEXED)

llm_cache(cache_key PK, request_type, response_json, model, created_at)
-- cache_key = sha256(需求归一化 + top_chunk_ids + model + PROMPT_VERSION)

usage_log(id, ts, request_type, model, prompt_tokens, completion_tokens, cache_hit, error)
```

### 4.2 config.json（`~/.patentkb/config.json`，PATENTKB_CONFIG 可覆盖）

```json
{
  "kb_path": "D:\\patents\\项目A",
  "server":  {"host": "127.0.0.1", "port": 8322, "auth_token": ""},
  "llm":     {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-...",
              "model": "deepseek-chat", "lite_model": "",
              "timeout_sec": 60, "max_output_tokens": 2000},
  "fts":     {"user_dict": "", "snippet_max_chars": 200},
  "analyzer": {"top_n_chunks": 15, "max_chunk_chars": 4000},
  "cost":    {"daily_llm_call_limit": 50, "warn_tokens_per_call": 8000},
  "features": {"embedding": false, "vision": false},
  "indexer": {"exclude_dirs": [".kbindex", ".git", "node_modules", "__pycache__"],
              "max_rows_per_sheet": 5000, "max_pdf_pages": 300}
}
```

api_key 回显掩码（`sk-***abc`），PUT 传空表示不变。

---

## 5. 文件解析策略

| 类型 | 库 | 策略 | anchor |
|---|---|---|---|
| docx | python-docx | 段落+表格按序拼接，~1000 字符段落边界切块 | para[起,止] |
| xlsx/csv | openpyxl read_only / csv | 逐 sheet，30 行一块（超 2000 字符细分），5000 行截断 | sheet+rows |
| pdf | PyMuPDF | 前 5 页均字符 <50 → skipped_no_text 只索引文件名；否则按页聚合，300 页截断 | page[起,止] |
| pptx | python-pptx | 每 4 slide 一块（含备注） | slide[起,止] |
| txt/md | 内置 | UTF-8（GBK 回退），行边界切块 | lines[起,止] |
| jpg/png | — | 文件名+路径伪 chunk；UI 识图按钮读 vision 配置 | filename_only |
| doc/xls/ppt | — | unsupported | — |

单文件失败不阻断整批；截断标记 truncated。

---

## 6. API（/api 前缀；检索/浏览零 LLM 成本）

| 端点 | 说明 |
|---|---|
| `GET /wizard/status` · `POST /wizard/setup` · `POST /config/test-llm` · `GET/PUT /config` | 向导与配置；setup 校验路径可写并建 .kbindex |
| `GET /browse?path=` | 目录内容（过滤 exclude_dirs） |
| `GET /file/chunks?file_id=` | 文本预览 |
| `GET /file/download?file_id=` | 下载（resolve 后必须仍在 kb_path 内，防穿越） |
| `POST /file/reveal` | explorer /select 定位（本地版限定） |
| `GET /search?q=&limit=` | jieba 分词，多词 AND 优先、无结果降级 OR；高亮 snippet |
| `POST /analyze/estimate` | 先免费检索后估 token，不调 LLM |
| `POST /analyze` | 返回 keywords[] / classifications[] / patent_query / usage |
| `POST /analyze/image` | MVP 预留：vision=false 时 403 + 开启指引 |
| `GET /admin/index/status` · `POST /admin/index/scan` · `GET /admin/files?status=` · `GET /admin/usage?days=` | 管理端 |

错误：超每日限额 429；LLM 超时/不可达 502。

### /analyze 响应结构

```json
{
  "keywords": [{"text":"无线充电线圈","verified":true,
                "sources":[{"chunk_id":12,"rel_path":"...","snippet":"...","anchor":{}}]}],
  "classifications": [{"code":"H02J50/90","explanation":"...","verified":false,"sources":[...]}],
  "patent_query": {"query":"TI=(...) AND CPC=(H02J50)","syntax_ok":true,"explanation":"...","sources":[...]},
  "usage": {"model":"deepseek-chat","prompt_tokens":2100,"completion_tokens":350,
            "cache_hit":false,"daily_used":"3/50"}
}
```

---

## 7. 可追溯性机制（核心设计）

1. **检索先行**：FTS 检索 top-15 chunk，编号 C1..C15（含文件路径）喂给 LLM
2. **强制引用**：Prompt 要求每个生成项输出 `source_refs:["C3"]`
3. **后端校验**：引用的词/码确实出现在所指 chunk → `verified:true`；引用有效但原文未见 → `verified:false`（黄色"AI 推断"，锚定其依据 chunk）；引用无效（如 C99）→ 一次"合法 JSON"重试，仍败标黄不丢弃
4. **前端透明**：双色徽标 + 点击 chip 弹来源浮层（chunk 原文 + 文件路径 + 打开/下载）

> 设计取舍：严格"必须原文可见"会误杀合理推断（新分类号、同义词）。透明优于隐藏——可追溯不等于零推断，但绝不静默编造。

---

## 8. 成本控制方案

### 8.1 三档方案矩阵

| 档位 | 配置 | 每次分析成本 | 适用场景 | 取舍 |
|---|---|---|---|---|
| **零成本档** | 不配 LLM（或 limit=0） | 0 | 只需浏览+检索+下载；试用 | 分析功能置灰 |
| **低成本档（默认推荐）** | lite 模型（如 deepseek-chat / 硅基流动 Qwen 小模型），每日上限 20，缓存开启 | ~0.001–0.005 元；重复需求 0（缓存） | 个人日常，每天十几次分析 | 分类号推断质量略低；重分析场景可单次切换主力模型 |
| **完整档** | 双模型路由（lite 做关键词扩展，主力做分类号/检索式）+ 上限 100 + vision 开启 | 重度使用每天 <1 元级 | 团队共享知识库、高频交付 | 管两套配置；vision 按次计费需二次确认 |

### 8.2 控制机制

- 检索永远零成本；LLM 仅 /analyze（及预留 vision）触发
- 需求归一化 + chunk 组合 hash 缓存：命中免费且不计限额
- 执行前强制 token 预估（warn_tokens_per_call 超限前端二次确认）
- usage_log 驱动每日限额（daily_llm_call_limit），429 兜底
- mtime+size 增量索引，零重复解析
- embedding / vision 默认关

---

## 8.5 借鉴 WeKnora 的精简优化（已实现，2026-11-20）

对比腾讯 WeKnora（重型通用 RAG 框架）后，在「保持极简、零检索成本、不加 Docker/向量库」约束下的四项优化。四项均**未新增数据库表**，`.kbindex/` 仍随文件夹自包含迁移。

| 优化 | 实现 | 取舍 |
|---|---|---|
| 可追溯加强 | `verify_score()` 多维度校验（整体包含/分词覆盖/bigram 重叠）；非法引用 C99 显式告警；检索式加 verified/coverage；PROMPT_VERSION=v3-traceable | 字符串近似，非语义蕴含；透明展示数值供判断 |
| 同义词召回 | 查询时双向扩展（内置种子表 + `<kb>/.kbindex/synonyms.json` 覆盖），无需重建索引 | 可控召回，非真语义检索；未登录词无效 |
| 多轮追问 | 前端内存会话 + 复用同一批 chunk（不重跑 FTS），缓存键含历史摘要防串味 | 每轮计费；刷新页面会话清空 |
| 按需 OCR | 默认关闭，浏览页对"无文字层"文件单文件触发；RapidOCR 本地 CPU；OCR 文本写现有 chunks 表、保持 status 不变防增量覆盖 | 需装 rapidocr（含 opencv，约百 MB）；低质量扫描识别率有限；单次≤20 文件 |

**暂缓（需放宽极简约束才值得做）**：本地 embedding（首次下载 50–100MB 模型 + 索引膨胀 + 启动分支，除非接受模型文件与复杂度）、LLM 查询改写（破坏检索零成本）、tesseract（系统级安装违背双击即用）、云端 OCR（数据出本机 + 计费）。

---

## 9. Phase 2 展望（架构预留，不实现）

- **自包含迁移**：索引库在知识库文件夹的 `.kbindex/` 内，文件夹拷贝到远程电脑即完成迁移
- **远程访问路径**：config `server.host` 改 `0.0.0.0`（或 Tailscale 内网 IP）+ `auth_token` 非空启用中间件校验 Authorization 头（代码钩子已就位）+ 建议经 Tailscale/frp 隧道而非公网裸奔
- **移动端**：前端响应式改造 + 大按钮触控适配；SQLite WAL 模式支持多读
- **embedding 开关**：features.embedding 配置位，未来接入向量化不改变现有表结构（chunks 表直接可用）

---

## 10. 风险与边界

| 风险 | 决策 |
|---|---|
| FTS5 中文分词 | jieba 预分词（unicode61 按字切不可用；trigram 膨胀 3 倍且 2 字词失效） |
| jieba 专利术语切分不佳 | user_dict 配置化（知识库内放词表文件），运营配置非代码问题 |
| 大 xlsx 索引体积 | 5000 行截断 + truncated 标记 + openpyxl read_only 流式 |
| LLM 引用校验边界 | verified/inferred 双标记，透明优于隐藏 |
| 检索式质量 | 仅括号/引号配对自检；按智慧芽(PatSnap)高级检索语法生成，建议先在智慧芽试跑校验 |
| SQLite 并发 | 单连接 + threading.Lock 写串行；单人场景无压力 |
| PyMuPDF AGPL | 个人/内部使用无碍，README 注明；可换 pdfplumber（Apache-2.0） |
| 路径安全 | rel_path POSIX 分隔符存储；下载接口 resolve 后前缀校验 |
| LLM 非法 JSON | 一次格式重试，仍败 502 明确报错 |
