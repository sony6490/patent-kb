# 专利项目经验知识库-by WB（原 patent-kb）

本地优先的**专利咨询项目知识库工具**：选择本地文件夹作为知识库，在同一个「检索」页里做关键词检索与目录浏览——结果直接给出文件所在的逐级目录并可一键定位；输入新需求，基于知识库内容生成关键词扩展、IPC/CPC 分类号扩展（含解释）与专利检索式——**每一项生成内容都带可点击的原文来源锚点**。

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
| 检索 + 目录浏览 / 查看原文 / 下载 / 定位到文件所在目录 | **0 元**（SQLite FTS5 本地检索） |
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

## 同义词召回（零成本）

查询时自动按同义词扩展（双向：搜「对位」能召回原文「定位」），不重建索引、不花 token。内置种子表覆盖无线充电/线圈/定位/耦合/热成像等常见术语；自定义表放 `<知识库>/.kbindex/synonyms.json`，格式 `{"无线": ["无线充电","无线供电"]}`，改后即时生效。设置页可关闭（`fts.synonyms_enabled`）。

## 多轮追问

分析结果下方输入追问，复用同一批资料片段（不重检索）。每轮为一次 LLM 调用；会话仅存页面内存，刷新即清空。

## 按需 OCR（默认关闭）

设置页开启 `features.ocr` 后，浏览页对"无文字层"的扫描 PDF/图片点「OCR」按钮，仅识别该文件（本地 CPU，零 API 费用），结果写入索引并可检索。单次最多 20 文件，不批跑全库。首次使用需 `uv sync` 安装 rapidocr-onnxruntime。

## 限制说明

- 检索式仅做括号/引号配对自检，按智慧芽(PatSnap)高级检索语法生成（字段 TI/ABST/CLMS/TAC/PN/PA/IN/IPC/CPC 等），建议先在智慧芽试跑校验后再正式使用
- 生成内容"AI 推断"标记项表示原文未见、由模型依据资料推断——透明展示而非隐藏
- PyMuPDF 为 AGPL 许可（个人/内部使用无碍；需闭源分发可自行替换 pdfplumber）
- MVP 仅监听 127.0.0.1；远程/手机访问为 Phase 2（改 `server.host` + 配 `auth_token` + 建议经 Tailscale 等隧道）

## 回归测试

```powershell
uv run pytest tests/test_regression.py -q
```

用例固化关键路径：LLM 坏 JSON 不污染缓存、历史脏缓存自愈、OCR/download 路径越界防护、同义词召回、引用校验、切库往返检索恢复、OCR chunk 不被增量扫描覆盖、检索式分类号噪音剔除；检索结果展示项目名与完整路径、API 费用计算（DeepSeek 官方峰谷定价）与缓存命中零费用、模型列表内置补全与接口故障降级、schema 幂等迁移。测试全程不联网、不调用真实 LLM。

## 配置文件

`~/.patentkb/config.json`（环境变量 `PATENTKB_CONFIG` 可改位置）。全部字段见 `docs/DESIGN.md`。

## 提交前脱敏自检

本仓库会同步到 GitHub，**不要提交任何本机特有信息**（用户名、盘符绝对路径、私有目录名、客户目录结构）。

```powershell
uv run python scripts/check_desensitize.py          # 检查已跟踪文件
uv run python scripts/check_desensitize.py --staged # 只查暂存区
```

命中即退出码 1 并打印 `文件:行号 [类型] 内容`。建议装成本地钩子自动拦截：

```sh
printf '#!/bin/sh\nexec "$(git rev-parse --show-toplevel)/.venv/Scripts/python.exe" "$(git rev-parse --show-toplevel)/scripts/check_desensitize.py" --staged\n' > .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

运维脚本（`scripts/`）的知识库路径一律运行期传入，不写入代码：

```powershell
python scripts/p1_ocr_driver.py <知识库根目录>   # 或设 PATENTKB_KB_ROOT
```

`p1_ocr_driver.py` 在服务已启动时可自动从 `/api/wizard/status` 取 `kb_path`，无需任何参数。
