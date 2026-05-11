# 财报智能问答Agent 系统

## 项目简介

财报智能问答Agent 系统面向上市公司财务分析场景，围绕财务报告 PDF、公司基础信息、结构化数据库、问题清单和研报数据，构建从数据解析、结构化入库、问题理解、工具调用到结果导出的端到端自动化问答流程。

系统不是单一问答脚本，而是一个面向财报任务的多工具 Agent：它会根据问题自动拆解任务，选择调用 Text-to-SQL、研报检索、规则推理、图表生成和结果导出等模块，最终生成标准化答案文件和运行日志。

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
| 结构化入库 | 抽取利润表、资产负债表、现金流量表和核心财务指标，写入 SQLite 或 MySQL。 |
| Text-to-SQL | 将公司、指标、时间、排名、趋势等槽位映射为 SQL 查询。 |
| 多轮上下文 | 支持连续追问，继承上一轮公司、报告期、指标等上下文信息。 |
| 轻量级 RAG | 对附件5研报 PDF 进行文本抽取、分块索引和 Top-K 检索，支持开放式问题归因。 |
| Agent 任务编排 | 将复杂问题拆解为 SQL 查询、检索、推理和图表生成等子任务。 |
| 图表生成 | 根据趋势、排名、结构占比等问题生成折线图、柱状图、饼图等图片。 |
| 结果导出 | 按要求生成 `result_2.xlsx`、`result_3.xlsx`，并记录 SQL、引用、图表和运行日志。 |
| 安全校验 | 对 SQL 执行进行 SELECT-only、表名白名单和字段白名单校验。 |

---

## 技术栈

- **语言与数据处理**：Python、Pandas、OpenPyXL
- **数据库**：SQLite、MySQL
- **文档解析**：pypdf、pdftotext、Tesseract OCR fallback
- **问答与 Agent**：Text-to-SQL、轻量级 RAG、任务规划、规则化推理
- **可视化**：Matplotlib
- **配置与日志**：YAML、环境变量、结构化运行日志
- **测试**：pytest

---

## 系统架构

```text
用户问题 / 附件问题清单
        │
        ▼
任务规划器 Planner
        │
        ├── SQL 子任务 ──► Text-to-SQL ──► SQLite/MySQL ──► 结构化财务结果
        │
        ├── 检索子任务 ─► 研报知识库 ─────► Top-K 引用片段
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
财报智能问答Agent/
├── 数据/
│   ├── 测试数据/                    # 测试数据目录
│   └── 全量数据/                    # 全量数据目录
├── 样例数据/                        # 样例数据目录
├── smart_finqa/                     # 核心程序模块
│   ├── config.py                    # 配置读取与环境变量管理
│   ├── core.py                      # 报告期解析、数值标准化、SQL 安全校验
│   ├── database.py                  # 数据库建表、入库、查询与缓存
│   ├── ingestion.py                 # PDF 文本解析与元信息识别
│   ├── extraction.py                # 财务指标抽取
│   ├── task2.py                     # 任务二槽位识别与查询意图构建
│   ├── sql_planner.py               # SQL 查询规划
│   ├── kb.py                        # 研报知识库与检索
│   ├── planner.py                   # 任务三子任务规划
│   ├── quality.py                   # 回答格式化与质量控制
│   └── pipeline.py                  # 主流程调度
├── tests/                           # 单元测试与回归测试
├── outputs/                         # 数据库、日志和运行记录
├── result/                          # 图表输出目录
├── run_pipeline.py                  # 命令行入口
├── run.sh                           # 一键运行脚本
├── config.example.yaml              # 配置示例
├── requirements.txt                 # Python 依赖
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

程序只写入 `outputs/`、`result/`、`result_2.xlsx` 和 `result_3.xlsx`，不会修改原始数据附件。

---

## 环境要求

- Python 3.10 及以上版本。
- macOS、Linux 或 Windows。
- 本地复现推荐使用 SQLite；多人共享或大规模数据可使用 MySQL。
- 可选安装 `pdftotext`、`pdftoppm`、`tesseract`，用于提升 PDF/OCR 解析覆盖率。

安装依赖：

```bash
pip3 install -r requirements.txt
```

主要依赖：

- `openpyxl`：读取和写入 Excel。
- `pypdf`：解析 PDF 文本。
- `pandas`：处理表格与结果导出。
- `matplotlib`：生成图表图片。
- `pyyaml`：读取 YAML 配置。
- `psutil`：监控运行资源。
- `mysql-connector-python`：MySQL 后端支持。
- `pytest`：测试运行。

---

## 快速运行

进入项目目录：

```bash
cd /Users/benson/Desktop/程序
```

推荐使用 SQLite 完整运行：

```bash
DB_BACKEND=sqlite SQLITE_DB_PATH=outputs/finance.db \
python3 run_pipeline.py --mode all --full-data true --workers 4 --config config.example.yaml
```

也可以使用一键脚本：

```bash
bash run.sh
```

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
python3 run_pipeline.py --mode all --full-data true

# 仅入库
python3 run_pipeline.py --mode ingest --full-data true

# 仅生成任务二结果
python3 run_pipeline.py --mode task2 --full-data true

# 仅生成任务三结果
python3 run_pipeline.py --mode task3 --full-data true
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

程序支持通过 YAML 文件或环境变量配置数据库、日志、缓存、知识库和 LLM 参数。命令行参数会覆盖配置文件中的运行模式、数据模式、并发数、日志级别和缓存开关。

### SQLite 配置

SQLite 适合本地复现和结果生成，无需启动数据库服务。

```bash
export DB_BACKEND=sqlite
export SQLITE_DB_PATH=outputs/finance.db
python3 run_pipeline.py --mode all --full-data true
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

python3 run_pipeline.py --mode all --full-data true
```

也可以在 `config.example.yaml` 中配置：

```yaml
database:
  backend: mysql
  host: 127.0.0.1
  port: 3306
  user: root
  password: "your-password"
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

---

## 核心流程

### 任务一：财报结构化入库

任务一以附件2财务报告 PDF 为输入，依据附件3数据库字段说明，完成财务数据结构化抽取与入库。

处理流程：

1. 解析 PDF 文本，必要时使用 `pdftotext` 或 OCR fallback。
2. 根据文件名、标题和正文识别公司简称、股票代码和报告期。
3. 定位利润表、资产负债表、现金流量表和核心财务指标区域。
4. 根据字段别名和规则映射抽取财务指标。
5. 标准化数值单位、百分比、括号负数、缺失值和异常值。
6. 写入 SQLite 或 MySQL，并记录增量状态和运行日志。

### 任务二：Text-to-SQL 多轮问答

任务二读取附件4问题，将自然语言问题解析为结构化查询意图。

系统识别：

- 公司简称和股票代码。
- 报告期，例如年度、季度、近三年。
- 财务指标，例如营业收入、净利润、资产负债率、研发费用。
- 分析类型，例如单指标查询、趋势分析、Top-N 排名、对比分析。
- 图表需求，例如折线图、柱状图、饼图。

解析完成后，系统生成 SQL，查询财务数据库，并输出答案、SQL 查询语句和图形格式。多轮问题会继承上下文，支持补充公司、补充报告期和继续追问。

### 任务三：RAG 综合分析问答

任务三读取附件6综合问题，结合结构化财务数据和附件5研报数据生成回答。

处理流程：

1. 使用 Planner 将问题拆解为 SQL、retrieval、reason 等子任务。
2. SQL 子任务查询结构化财务数据库。
3. Retrieval 子任务检索研报知识库中的相关文本片段。
4. Reason 子任务综合 SQL 结果、研报引用和上下文生成回答。
5. 如有图表需求，生成图片并在答案 JSON 中引用。

当前 RAG 为轻量级实现：默认使用关键词匹配召回，可选使用 embedding 相似度增强；未依赖独立向量数据库。

---

## 输出文件

运行完成后会生成或更新：

| 文件或目录 | 说明 |
|---|---|
| `outputs/finance.db` | SQLite 结构化财报数据库。 |
| `outputs/smart_finqa.log` | 运行日志。 |
| `outputs/run_log.json` | 入库、查询、校验、引用和图表记录。 |
| `outputs/ingestion_state.json` | 增量入库状态。 |
| `result_2.xlsx` | 任务二答案文件。 |
| `result_3.xlsx` | 任务三答案文件。 |
| `result/*.jpg` | 问题回答中生成的图表图片。 |

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

推荐在交付前执行以下检查：

```bash
python3 -m pytest tests/test_config_and_modes.py -v
python3 -m pytest tests/test_pipeline_run_modes.py -v
```

手工检查项：

- `result_2.xlsx` 是否存在，列名是否为 `编号`、`问题`、`SQL 查询语句`、`图形格式`、`回答`。
- `result_3.xlsx` 是否存在，列名是否为 `编号`、`问题`、`SQL 查询语法`、`回答`。
- `回答` 字段是否为合法 JSON。
- 如果答案包含 `image`，对应 `result/*.jpg` 是否存在。
- `outputs/run_log.json` 是否记录 task2/task3 行数、SQL、安全校验和异常信息。

---

## 安全与可靠性设计

- SQL 查询默认限制为 SELECT 查询，禁止危险关键字和多语句执行。
- 表名和字段名基于 schema 白名单校验，减少越权查询风险。
- 查询缓存可降低重复 SQL 执行开销。
- 增量入库通过文件签名跳过未变化 PDF，避免重复处理。
- 运行日志记录每题 SQL、返回行数、图表路径和检索引用，便于复盘。
- 无 LLM 配置时使用本地规则和检索逻辑，避免强依赖外部 API。

---

## 常见问题

### 数据库不存在

如果直接运行 `task2` 或 `task3` 报数据库不存在，请先执行入库：

```bash
python3 run_pipeline.py --mode ingest --full-data true
```

### MySQL 无法连接

检查 MySQL 服务、数据库名称、账号密码和权限。若只是本地复现，建议切换 SQLite：

```bash
export DB_BACKEND=sqlite
export SQLITE_DB_PATH=outputs/finance.db
python3 run_pipeline.py --mode all --full-data true
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
python3 run_pipeline.py --mode all --full-data true
```

---

## 简历描述建议

项目名称建议写为：

```text
财报智能问答Agent 系统
```

简历 Bullet 可写：

- 构建面向上市公司财报的智能问答Agent，集成财报 PDF 解析、结构化入库、Text-to-SQL、轻量级 RAG、图表生成和 Excel 结果导出能力。
- 设计任务规划流程，将复杂财务问题拆解为 SQL 查询、研报检索、规则推理和图表生成等子任务，实现多工具自动编排。
- 实现财务指标槽位识别和 SQL 查询规划，支持公司、报告期、指标、Top-N、趋势和多轮上下文问答。
- 构建研报知识库检索模块，对研报 PDF 进行文本抽取、分块索引和 Top-K 召回，为开放式归因问题提供引用依据。
- 支持 SQLite/MySQL 双后端、增量入库、查询缓存、运行日志和结果格式校验，提升系统可复现性和可维护性。

---

## 项目说明

本项目定位为财报智能问答Agent 系统，README 用于说明系统能力、运行方式、数据输入输出和结果复现流程。如需适配特定交付场景，请按目标要求确认文件命名、附件数量和匿名性检查。
