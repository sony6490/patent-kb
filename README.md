# patent-kb

本地优先的**专利咨询项目知识库工具**：选择本地文件夹作为知识库，网页浏览/检索/下载历史文件；输入新需求，基于知识库内容生成关键词扩展、IPC/CPC 分类号扩展（含解释）与专利检索式——**每一项生成内容都带可点击的原文来源锚点**。

## 快速开始（3 步）

```
1. 安装 uv（若未装）：winget install astral-sh.uv
2. 在本目录执行：uv run patentkb
3. 浏览器自动打开 http://127.0.0.1:8322 → 网页向导：选知识库文件夹 → 填大模型 API → 首次索引
```

> 国内网络下载依赖慢时改用镜像：`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv run patentkb`

- 大模型 API：任何 **OpenAI 兼容接口**（DeepSeek / 硅基流动 / Kimi / 通义 / OpenAI / 本地 Ollama），填 base_url + api_key + model 三项即可
- API Key 只保存在本机 `~/.patentkb/config.json`
- 索引库放在知识库文件夹的 `.kbindex/` 内，**整个文件夹拷走即可迁移**（Phase 2 远程访问的基础）

## 功能

| 功能 | 成本 |
|---|---|
| 浏览 / 关键词检索 / 查看原文 / 下载 | **0 元**（SQLite FTS5 本地检索） |
| 需求分析（关键词/分类号扩展 + 检索式生成） | 按 LLM 次数计费；相同需求命中缓存 = 0 元 |
| 增量索引 | 0 元（mtime+size 对比，只解析变化文件） |

可追溯机制：分析结果中每项标"**原文验证**"（生成内容确出现在来源片段，绿色）或"**AI 推断**"（引用有效但原文未见，黄色），点击可查看来源片段原文与文件路径。

## 成本控制

- **不配 LLM 也能用**：浏览 + 检索 + 下载全部免费
- 设置页可配：每日 LLM 调用上限（默认 50，缓存命中不计）、单次 token 预警阈值（默认 8000）
- 分析前先显示 token 预估，确认后执行
- Lite Model：低成本档，可勾选"使用 Lite 模型"单次切换

## 支持的文件类型

| 类型 | 说明 |
|---|---|
| docx / xlsx / csv / pptx / txt / md | 全文解析，带定位锚点（段落/行/页/slide） |
| pdf | 有文字层 → 全文；扫描件（无文字层）→ 仅索引文件名 |
| jpg / png | 仅文件名索引（内容识别为 Phase 2 预留，默认关闭） |
| doc / xls / ppt 老格式 | 标记"不支持"，建议另存为新格式 |

## jieba 自定义词表

专利术语建议加词表提升检索召回。设置 `config.json` 中 `fts.user_dict` 指向词表文件（每行一词，可加 `词 频次 词性`），例如：

```
无线充电线圈 100 n
耦合系数 100 n
FRAND 10 n
```

## 限制说明

- 检索式仅做括号/引号配对自检，按智慧芽(PatSnap)高级检索语法生成（字段 TI/ABST/CLMS/TAC/PN/PA/IN/IPC/CPC 等），建议先在智慧芽试跑校验后再正式使用
- 生成内容"AI 推断"标记项表示原文未见、由模型依据资料推断——透明展示而非隐藏
- PyMuPDF 为 AGPL 许可（个人/内部使用无碍；需闭源分发可自行替换 pdfplumber）
- MVP 仅监听 127.0.0.1；远程/手机访问为 Phase 2（改 `server.host` + 配 `auth_token` + 建议经 Tailscale 等隧道）

## 配置文件

`~/.patentkb/config.json`（环境变量 `PATENTKB_CONFIG` 可改位置）。全部字段见 `docs/DESIGN.md`。
