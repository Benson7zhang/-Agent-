# Smart FinQA 财报智能问答系统

## 项目简介

Smart FinQA 面向上市公司财务分析场景，围绕财务报告 PDF、公司基础信息、结构化数据库、问题清单和研报数据，构建从数据解析、人工复核、可信问数到结果导出的本地系统。项目同时提供批处理 CLI 和 Web 工作台，两者复用同一事实、查询、计算和证据链路。

当前版本已经建立事实状态、安全查询、计算算子、页级证据和评测契约，但尚未完成真实财报准确率验收。现有正则抽取结果固定进入 `NEEDS_REVIEW`，未经人工复核不会参与问数；仓库内合成评测也不能作为真实准确率证据。

Web 版本当前定位为无认证的本地单机工具，强制只监听回环地址；它不是可直接部署到公网或局域网的多人系统。开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，架构边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，人工复核见 [docs/TRUSTED_FACTS.md](docs/TRUSTED_FACTS.md)，评测契约见 [docs/EVALUATION.md](docs/EVALUATION.md)。

核心目标：

- 将上市公司财报 PDF 和附件数据转化为可查询的结构化财务数据库。
- 将自然语言问题解析为可执行的 SQL 查询、研报检索或综合分析任务。
- 支持多轮问答、趋势分析、排名分析、图表生成和研报引用溯源。
- 自动生成 `result_2.xlsx`、`result_3.xlsx` 和 `result/*.jpg`，满足批量运行和结果复现要求。

---

## 核心能力

| 能力 | 说明 |
|---|---|
| 财报 PDF 解析 | 解析附件2财务报告，识别公司、股票代码、报告期和财务报表内容。 |
| 可信事实层 | 候选事实保存数值、单位、口径、页码、行列、置信度和复核状态；只有 `VALIDATED` 事实可用于问数。 |
| 安全 Text-to-SQL | `QuerySpec` 编译为参数化 SQL，经 AST 白名单、可信事实连接、行数限制和审计后执行。 |
| 多轮上下文 | 使用不可变 `SessionState` 处理公司切换、显式时间锚点和澄清。 |
| 证据型 RAG | 对研报逐页切块，支持元数据过滤、混合检索、确定性重排和页级原文引用。 |
| 计算算子 | 支持比率、期间比较、行业均值和多指标 Top-N 交集，并显式处理零分母等错误。 |
| 真实评测契约 | 计算字段召回率、事实精确率、单元格准确率、问数准确率和精确来源定位；真实集还需通过源文件与标注文件哈希校验。 |
| Agent 任务编排 | 将复杂问题拆解为 SQL 查询、检索、推理和图表生成等子任务。 |
| 图表生成 | 根据趋势、排名、结构占比等问题生成折线图、柱状图、饼图等图片。 |
| 结果导出 | 按要求生成 `result_2.xlsx`、`result_3.xlsx`，并记录 SQL、引用、图表和运行日志。 |
| 失败可见 | 未复核事实、冲突、无页码证据、不安全 SQL、结果截断和外部服务失败都会明确暴露。 |

---

## 技术栈

- **语言与数据处理**：Python、Pandas、OpenPyXL
- **数据库**：SQLite、MySQL
- **文档解析**：pypdf、pdftotext、RapidOCR（PP-OCRv6 + ONNX Runtime）
- **问答与 Agent**：Text-to-SQL、轻量级 RAG、任务规划、规则化推理
- **可视化**：Matplotlib
- **配置与日志**：YAML、环境变量、结构化运行日志
- **测试**：pytest
- **Web**：FastAPI、React、TypeScript、Vite、TanStack Query/Table、ECharts、PDF.js

---

## 系统架构

```text
用户问题 / 附件问题清单
        │
        ▼
统一 QuerySpec / SessionState
        │
        ├── SQL 子任务 ─► 参数化 SQL + AST 校验 ─► VALIDATED 事实投影
        ├── 计算子任务 ─► 注册计算算子 ─────────► 可解释计算结果
        ├── 检索子任务 ─► 页级研报证据 ─────────► 文件/页码/原文
        │
        ├── 图表子任务 ─► Matplotlib ─────► result/*.jpg
        │
        ▼
回答生成与结果导出
        │
        ├── result_2.xlsx
        ├── result_3.xlsx
        └── outputs/run_log.json
```

---

## 目录结构

```text
smart-finqa/
├── 数据/
│   ├── 测试数据/                    # 测试数据目录
│   └── 全量数据/                    # 全量数据目录
├── 样例数据/                        # 样例数据目录
├── smart_finqa/                     # 核心程序模块
│   ├── config.py                    # 配置读取与环境变量管理
│   ├── cli.py                       # 可安装命令行入口
│   ├── core.py                      # 报告期解析、数值标准化、SQL 安全校验
│   ├── database.py                  # 数据库建表、入库、查询与缓存
│   ├── ingestion.py                 # PDF 文本解析与元信息识别
│   ├── extraction.py                # 财务指标抽取
│   ├── facts.py                     # 财务事实与复核状态
│   ├── task2.py                     # 任务二槽位识别与查询意图构建
│   ├── sql_planner.py               # SQL 查询规划
│   ├── safe_query.py                # QuerySpec、SessionState、AST 校验与审计
│   ├── calculations.py              # 财务计算算子
│   ├── kb.py                        # 研报知识库与检索
│   ├── evaluation.py                # 金标评测契约与指标
│   ├── planner.py                   # 任务三子任务规划
│   ├── quality.py                   # 回答格式化与质量控制
│   └── pipeline.py                  # 主流程调度
├── web/                             # React 问数与复核工作台
├── tests/                           # 单元测试与回归测试
├── docs/                            # 架构、事实复核与评测契约
├── outputs/                         # 数据库、日志和运行记录
├── result/                          # 图表输出目录
├── run_pipeline.py                  # 命令行入口
├── run.sh                           # 一键运行脚本
├── pyproject.toml                   # 包元数据、依赖和工具配置
├── CONTRIBUTING.md                  # 开发与贡献规范
├── config.example.yaml              # 配置示例
├── requirements.txt                 # 兼容安装入口
├── result_2.xlsx                    # 任务二结果文件
└── result_3.xlsx                    # 任务三结果文件
```

---

## 数据输入

程序会根据 `--full-data` 参数自动定位数据目录。

当 `--full-data true` 时，候选目录优先级为：

1. `数据/测试数据/`
2. `数据/全量数据/`
3. `测试数据/`
4. `全量数据/`
5. `样例数据/`
6. 当前目录

当 `--full-data false` 时，默认使用 `样例数据/`，若不存在则使用当前目录。

主要附件：

| 附件 | 用途 |
|---|---|
| `附件1：*上市公司基本信息*.xlsx` | 公司名称、简称、股票代码等基础信息。 |
| `附件2：财务报告/` | 上市公司财务报告 PDF。 |
| `附件3：数据库-表名及字段说明.xlsx` | 数据库表结构、字段名和字段含义。 |
| `附件4：问题汇总.xlsx` | 任务二多轮问答问题。 |
| `附件5：研报数据/` | 个股研报、行业研报和研报元数据。 |
| `附件6：问题汇总.xlsx` | 任务三综合分析问题。 |

正式数据首次入库前，可先执行只读审计。命令只读取数据目录，并且拒绝把报告写回该目录；默认不计算全文件 SHA-256，只有显式传入 `--include-sha256` 才会计算：

```powershell
python -m smart_finqa.dataset_audit `
  --dataset-dir "正式数据" `
  --output "outputs/formal-baseline/dataset-audit.json"
```

审计报告统计文件、公司主数据、四张目标表 schema、问题集、财报元数据与权威版本选择、研报标题元数据覆盖和金标准备度。它不执行事实抽取或问数评测；没有通过版本化真实金标校验时，准确率固定标记为 `unavailable`。

程序只写入 `outputs/`、`result/`、`result_2.xlsx` 和 `result_3.xlsx`，不会修改原始数据附件。

---

## 环境要求

- Python 3.10 及以上版本。
- macOS、Linux 或 Windows。
- 本地复现推荐使用 SQLite；多人共享或大规模数据可使用 MySQL。
- 扫描 PDF 可使用本地 RapidOCR（PP-OCRv6 + ONNX Runtime）；默认仅处理低文本页、检测到财务报表标题的页面或含大尺寸栅格图像的混合页，并保留原文本层证据。

创建隔离环境并安装运行依赖：

```bash
python -m venv .venv
# 激活 .venv 后执行
python -m pip install -r requirements.txt
```

虚拟环境激活方式及开发依赖安装见 [CONTRIBUTING.md](CONTRIBUTING.md)。MySQL 驱动是可选依赖，使用 MySQL 前执行 `python -m pip install -e ".[mysql]"`；启用 OCR 前执行 `python -m pip install -e ".[ocr]"`。

主要依赖：

- `openpyxl`：读取和写入 Excel。
- `pypdf`：解析 PDF 文本。
- `pandas`：处理表格与结果导出。
- `matplotlib`：生成图表图片。
- `pyyaml`：读取 YAML 配置。
- `psutil`：监控运行资源。
- `mysql-connector-python`：可选的 MySQL 后端支持。
- `pytest`、`pytest-cov`、`ruff`：开发与 CI 工具。

---

## 快速运行

本地默认使用 SQLite。首次使用应先运行入库，检查 `validation_report.json` 并复核候选事实：

```bash
smart-finqa --mode ingest --full-data true --workers 4 --config config.example.yaml
```

完成事实复核后再运行 `task2` 或 `task3`。`all` 模式同样执行可信门检查；没有 `VALIDATED` 事实时会失败，不会直接使用自动抽取候选。复核接口和 SQL 清单见 [docs/TRUSTED_FACTS.md](docs/TRUSTED_FACTS.md)。

也可使用兼容入口：`python run_pipeline.py --mode all --full-data true --config config.example.yaml`。

也可以使用一键脚本：

```bash
bash run.sh
```

### 启动 Web 工作台

Web API 启动时需要数据目录中的附件 1 公司表和附件 3 schema 表；缺失时会明确失败，不会使用内置假数据。分别启动 API、worker 和前端：

```powershell
# 终端 1：安装并启动 API
python -m pip install -e ".[dev]"
smart-finqa-web --base-dir . --cors-origin http://127.0.0.1:5173

# 终端 2：处理持久入库任务
smart-finqa-worker --base-dir .

# 终端 3：前端
cd web
npm install
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。当前 Web 上传入口只接受财务报告 PDF；研报仍通过附件 5 的批处理链路导入。上传后需要显式创建入库任务；任务、运行事件和产物写入 `outputs/web_storage/`，每个任务使用独立目录和独立 staging SQLite。Pipeline 不直接写权威数据库，只有持有当前文档尝试和有效租约的 worker 才能在一个事务中提升事实、宽表基础投影、产物及任务终态。worker 会在长任务期间自动续租；服务重启后真正过期的运行中任务会标为 `INTERRUPTED`，只能由用户显式重试。

已有批处理基线可通过 `python -m smart_finqa.web_bootstrap` 注册到 Web 控制面。导入默认使用原子复制，存储副本不会随原 PDF 的后续修改而变化。`--file-mode hardlink` 仅用于明确接受共享文件 inode 风险的专家场景；使用它时，源 PDF 目录在 Web 存储的整个生命周期内都必须保持不可变，否则来源证据和已登记 SHA-256 会失真。

未配置认证时，`smart-finqa-web` 对非回环 `--host` 会直接拒绝启动。API 只接受不透明资源 ID，不提供任意 SQL 执行接口，也不向浏览器返回服务器文件路径。

运行模式：

| 模式 | 说明 |
|---|---|
| `all` | 完整流程：入库 + 任务二 + 任务三。 |
| `ingest` | 仅执行财报 PDF 解析和数据库入库。 |
| `task2` | 基于已有数据库生成 `result_2.xlsx`。 |
| `task3` | 基于已有数据库和研报数据生成 `result_3.xlsx`。 |

常用命令：

```bash
# 完整流程
smart-finqa --mode all --full-data true

# 仅入库
smart-finqa --mode ingest --full-data true

# 仅生成任务二结果
smart-finqa --mode task2 --full-data true

# 仅生成任务三结果
smart-finqa --mode task3 --full-data true
```

---

## 命令行参数

| 参数 | 说明 |
|---|---|
| `--base-dir` | 项目根目录，默认当前目录。 |
| `--mode` | 运行模式，可选 `ingest`、`task2`、`task3`、`all`。 |
| `--full-data` | 是否优先使用测试数据或全量数据目录。 |
| `--workers` | PDF 入库并发 worker 数量。 |
| `--incremental` | 是否启用增量入库，默认开启。 |
| `--config` | YAML 配置文件路径。 |
| `--log-level` | 日志级别，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR`。 |
| `--enable-cache` | 是否启用查询缓存。 |

---

## 配置方式

程序支持通过 YAML 文件或环境变量配置数据库、日志、缓存、知识库和 LLM 参数。只有显式传入的命令行参数才会覆盖配置值；指定但不存在的配置文件会直接报错。YAML 中的数据库密码或 LLM API Key 留空时，分别从 `MYSQL_PASSWORD`、`LLM_API_KEY` 读取。

### SQLite 配置

SQLite 适合本地复现和结果生成，无需启动数据库服务。

```bash
export DB_BACKEND=sqlite
export SQLITE_DB_PATH=outputs/finance.db
smart-finqa --mode all --full-data true
```

### MySQL 配置

MySQL 适合全量数据量较大或需要多人共享数据库的场景。使用前需先启动 MySQL，并创建数据库：

```sql
CREATE DATABASE smart_finqa DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

设置环境变量：

```bash
export DB_BACKEND=mysql
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_USER=root
export MYSQL_PASSWORD="your-password"
export MYSQL_DB=smart_finqa

smart-finqa --mode all --full-data true
```

也可以在 `config.example.yaml` 中配置：

```yaml
database:
  backend: mysql
  host: 127.0.0.1
  port: 3306
  user: root
  password: ""  # 留空时读取 MYSQL_PASSWORD
  database: smart_finqa
  sqlite_path: outputs/finance.db
```

### LLM 与 Embedding 配置

系统默认可在无 LLM 配置下使用规则解析、本地 SQL 查询和关键词检索完成主要流程。若配置 OpenAI-compatible API，可增强任务规划、回答生成和 embedding 检索能力。

```bash
export LLM_BASE_URL="https://api.example.com/v1"
export LLM_API_KEY="your-api-key"
export LLM_MODEL="your-chat-model"
export EMBEDDING_MODEL="your-embedding-model"
export KB_USE_EMBEDDINGS=true
```

对应 YAML：

```yaml
kb_use_embeddings: true
llm:
  base_url: "https://api.example.com/v1"
  api_key: "your-api-key"
  model: "your-chat-model"
  embedding_model: "your-embedding-model"
  timeout_seconds: 40
```

### 执行大模型文档抽取链路

在项目根目录的 `.env` 中配置 OpenAI-compatible 模型（该文件已被 Git 忽略）：

```dotenv
LLM_BASE_URL=https://api.example.com/v1
LLM_API_KEY=your-api-key
LLM_MODEL=your-chat-model
EMBEDDING_MODEL=
LLM_TIMEOUT_SECONDS=40
OCR_ENGINE=rapidocr
OCR_POLICY=financial_pages_and_low_text
OCR_DPI=220
OCR_MIN_PAGE_TEXT_CHARS=40
OCR_MIN_CONFIDENCE=0.5
OCR_MAX_PAGE_PIXELS=40000000
```

`OCR_MAX_PAGE_PIXELS` 限制单页栅格化后的总像素数，异常超大页面会以 `OCR_PAGE_TOO_LARGE` 明确失败，避免 worker 因渲染内存耗尽而失去响应。

程序启动时会自动读取当前项目目录的 `.env`；已经设置的系统环境变量优先，不会被 `.env` 覆盖。配置后可独立执行版本化的 P0-P8 提示词链路：

```powershell
smart-finqa-llm-extract `
  --input "正式数据\附件2：财务报告\reports-上交所\600080_20230428_FQ2V.pdf" `
  --schema "正式数据\附件3：数据库-表名及字段说明.xlsx" `
  --company "正式数据\附件1：中药上市公司基本信息（截至到2025年12月22日）.xlsx" `
  --output-dir "outputs\llm-extraction"
```

也可使用 `python -m smart_finqa.llm_extraction_cli` 调用同一入口。输入为 DOCX 时，程序会先执行 OOXML 安全检查，再通过 LibreOffice 确定性渲染为 PDF，并在审计包中记录原件、衍生 PDF 和渲染器哈希；没有 LibreOffice 时明确返回 `DOCX_RENDERER_UNAVAILABLE`。

该命令当前只生成可重放的 JSON 审计包，模型候选固定为 `NEEDS_REVIEW`，不会直接写入 `financial_fact` 或授予 `VALIDATED`。真实入库仍需接入现有 staging、人工复核和原子提升事务。完整提示词和协议见 [docs/LLM_DOCUMENT_EXTRACTION_PROMPTS.md](docs/LLM_DOCUMENT_EXTRACTION_PROMPTS.md)。

---

## 核心流程

### 任务一：财报结构化入库

任务一以附件2财务报告 PDF 为输入，依据附件3数据库字段说明，完成财务数据结构化抽取与入库。

处理流程：

1. 逐页解析 PDF；默认对扫描页、文本不足页、检测到财务报表标题或含大尺寸栅格图像的混合页运行本地 OCR，同时保留文本层、物理页码、坐标、置信度和页面图像哈希。
2. 根据文件名、标题和正文识别公司简称、股票代码和报告期。
3. 定位利润表、资产负债表、现金流量表和核心财务指标区域。
4. 根据字段别名和规则映射抽取财务指标。
5. 标准化数值单位、百分比、括号负数、缺失值和异常值。
6. 将候选写入 `financial_fact` 并生成校验报告，默认状态为 `NEEDS_REVIEW`。
7. 人工核对来源页、行列、表头单位和报表口径后确认或拒绝候选。
8. 仅将 `VALIDATED` 事实投影到兼容宽表。

当前步骤 3 至 5 仍是原型抽取器，尚未完成复杂表格、跨页表格和合并/母公司列结构恢复，因此不能绕过步骤 7。

### 任务二：Text-to-SQL 多轮问答

任务二读取附件4问题，将自然语言问题解析为结构化查询意图。

系统识别：

- 公司简称和股票代码。
- 报告期，例如年度、季度、近三年。
- 财务指标，例如营业收入、净利润、资产负债率、研发费用。
- 分析类型，例如单指标查询、趋势分析、Top-N 排名、对比分析。
- 图表需求，例如折线图、柱状图、饼图。

解析完成后，系统生成参数化 SQL。Task 2 和 Task 3 使用同一 AST 安全边界，只允许已注册表和字段、单条只读查询及绑定参数；业务指标还必须匹配已确认事实。答案会携带事实键、PDF 文件、页码和表格行列来源。

### 任务三：RAG 综合分析问答

任务三读取附件6综合问题，结合结构化财务数据和附件5研报数据生成回答。

处理流程：

1. 使用 Planner 将问题拆解为 SQL、retrieval、reason 等子任务。
2. SQL 子任务查询结构化财务数据库。
3. Retrieval 子任务按公司、行业和发布日期过滤，并混合检索逐页研报文本。
4. Reason 子任务综合 SQL 结果、研报引用和上下文生成回答。
5. 如有图表需求，生成图片并在答案 JSON 中引用。

当前 RAG 为进程内轻量级实现：默认关键词召回，可选 embedding 增强并确定性重排；引用包含文件、页码和原文片段。没有可定位到页码的证据时明确回答“证据不足”。

---

## 输出文件

运行完成后会生成或更新：

| 文件或目录 | 说明 |
|---|---|
| `outputs/finance.db` | SQLite 结构化财报数据库。 |
| `outputs/smart_finqa.log` | 运行日志。 |
| `outputs/run_log.json` | 入库、查询、校验、引用和图表记录。 |
| `outputs/ingestion_state.json` | 增量入库状态。 |
| `outputs/validation_report.json` | 事实状态、勾稽异常和未覆盖校验。 |
| `result_2.xlsx` | 任务二答案文件。 |
| `result_3.xlsx` | 任务三答案文件。 |
| `result/*.jpg` | 问题回答中生成的图表图片。 |

`run_log.json` 与同次生成的 `validation_report.json` 使用相同 `run_id`。入库日志全量保留异常、待复核和无效行事件，仅对成功解析事件按 `ingestion_log_limit` 采样，并在 `ingestion_event_stats` 中按状态记录总数、保留数和丢弃数。`ingestion_summary` 分开记录候选事实写入数、投影种子行数、含已验证指标的投影行数及已投影指标单元格数；投影种子行不代表存在可用于问数的财务指标。

当存在已验证事实、但没有任何记录具备完整勾稽输入时，校验报告状态为 `VALIDATION_UNAVAILABLE`，不得解释为校验通过。

任务二结果列：

| 列名 | 说明 |
|---|---|
| `编号` | 问题编号。 |
| `问题` | 原始问题 JSON。 |
| `SQL 查询语句` | 本题执行的 SQL。 |
| `图形格式` | 图表类型。 |
| `回答` | JSON 格式答案。 |

任务三结果列：

| 列名 | 说明 |
|---|---|
| `编号` | 问题编号。 |
| `问题` | 原始问题 JSON。 |
| `SQL 查询语法` | 本题执行的 SQL。 |
| `回答` | JSON 格式答案，可能包含 references 和 image。 |

---

## 结果校验

提交或交付前执行以下检查：

```bash
ruff check .
ruff format --check .
pytest
```

评测器使用版本化 manifest 和 JSON/JSONL 预测文件：

```python
from smart_finqa.evaluation import evaluate_files, write_evaluation_report

report = evaluate_files("golden/manifest.json", "run/predictions.jsonl")
write_evaluation_report(report, "run/evaluation.json")
```

仓库不包含真实财报金标，当前无法给出可信的 95%/98% 准确率。`tests/fixtures/evaluation/` 只验证评测器契约。交付前还应检查：

- `result_2.xlsx` 是否存在，列名是否为 `编号`、`问题`、`SQL 查询语句`、`图形格式`、`回答`。
- `result_3.xlsx` 是否存在，列名是否为 `编号`、`问题`、`SQL 查询语法`、`回答`。
- `回答` 字段是否为合法 JSON。
- 如果答案包含 `image`，对应 `result/*.jpg` 是否存在。
- `outputs/run_log.json` 是否记录 task2/task3 行数、SQL、安全校验和异常信息。

---

## 安全与可靠性设计

- SQL 查询通过 AST 限制为单条只读 `SELECT`，拒绝子查询、CTE、通配符、未知函数、锁定读和未绑定值。
- 表名和字段名基于 schema 白名单；schema 字段标识符和 MySQL 类型在输入边界受控。
- 查询输入关系只接受 `VALIDATED + consolidated` 事实，返回后再次检查唯一来源和数值一致性。
- 行数限制会主动探测截断并拒绝不完整结果。
- 查询缓存可降低重复 SQL 执行开销。
- 增量入库通过文件签名跳过未变化 PDF，避免重复处理。
- 运行日志记录 SQL、参数、审计状态、事实来源、图表和检索引用；失败运行也会落盘。
- 无 LLM 配置时使用本地规则和检索逻辑，避免强依赖外部 API。

---

## 常见问题

### 数据库不存在

如果直接运行 `task2` 或 `task3` 报数据库不存在，请先执行入库：

```bash
smart-finqa --mode ingest --full-data true
```

### MySQL 无法连接

检查 MySQL 服务、数据库名称、账号密码和权限。若只是本地复现，建议切换 SQLite：

```bash
export DB_BACKEND=sqlite
export SQLITE_DB_PATH=outputs/finance.db
smart-finqa --mode all --full-data true
```

### PDF 解析速度较慢

数据量较大时，PDF 解析和 OCR 会占用较多时间。可通过日志查看进度：

```bash
tail -f outputs/smart_finqa.log
```

### 需要重新入库

如果需要从零开始处理数据，可删除数据库和增量状态后重新运行：

```bash
rm outputs/ingestion_state.json
rm outputs/finance.db
smart-finqa --mode all --full-data true
```

---

## 项目说明

本项目定位为 Smart FinQA 财报智能问答系统，README 用于说明系统能力、运行方式、数据输入输出和结果复现流程。如需适配特定交付场景，请按目标要求确认文件命名、附件数量和匿名性检查。

---

## 开源协议

本项目采用 MIT License，详情请见 [LICENSE](LICENSE)。
