# Smart FinQA 架构与演进边界

## 系统定位

Smart FinQA 当前是“可信基础版本”的本地财报问答系统，包含批处理 CLI 和 Web 工作台，但不是已经完成真实准确率验收的通用智能问数产品。系统已建立事实状态、统一安全查询、持久会话、页级证据、审核事件和评测契约；现有正则抽取器仍只产生 `NEEDS_REVIEW` 候选，不能未经人工复核直接用于回答。

## 运行链路

```text
财报 PDF
  -> 逐页文本/OCR
  -> 候选事实（原型抽取器，NEEDS_REVIEW）
  -> financial_fact 长表 + validation_report.json
  -> 人工核对页码、行列、单位和口径
  -> VALIDATED 事实
  -> 仅由已验证事实生成的兼容宽表
  -> QuerySpec -> 参数化 SQL -> AST 校验 -> 行数限制 -> 执行审计
  -> 计算算子 -> 数值答案、图表和事实来源

独立 LLM 文档抽取
  -> PDF，或 DOCX 安全检查 + LibreOffice 确定性渲染
  -> 版本化 P0-P8 Prompt + 严格 JSON Schema
  -> 来源区域/候选 ID 白名单 + Decimal 复算 + 财务勾稽
  -> NEEDS_REVIEW 候选审计包（当前不写权威库）

研报 PDF
  -> 逐页切块 + 公司/行业/发布日期元数据
  -> 关键词与可选向量混合检索 + 确定性重排
  -> 文件、页码、原文片段引用
  -> 无页级证据时明确“证据不足”

Web/API
  -> 不透明 document_id / conversation_id / job_id
  -> 无状态 ApplicationService + 请求级 UnitOfWork
  -> 每个入库任务独立 staging SQLite
  -> lease token + 当前文档尝试校验后原子提升到权威库
  -> 持久作业、会话、查询快照和 append-only 审核事件
  -> AnswerResult / ChartSpec / Evidence DTO
  -> React 仅渲染后端口径，不在浏览器重算财务指标
```

## 模块职责

| 模块 | 职责 |
|---|---|
| `config.py` | 配置读取与校验，不连接外部系统 |
| `ingestion.py` | PDF/OCR 输入解析、逐页文本和报告元数据识别 |
| `extraction.py` | 原型指标抽取；输出不得被视为已验证事实 |
| `llm.py` | OpenAI-compatible 结构化模型客户端、能力声明和限定重试 |
| `llm_extraction_contracts.py`、`llm_prompts.py` | P0-P8/Repair DTO、运行时指标目录和版本化 Prompt 注册表 |
| `llm_extraction.py` | 阶段执行、Schema/身份/证据边界、Repair 防篡改和确定性单位换算 |
| `llm_document_pipeline.py`、`llm_extraction_cli.py` | PDF/DOCX 提示词编排、候选闭合校验和可重放审计包 |
| `document_conversion.py` | DOCX OOXML 安全检查、确定性 PDF 渲染和 hash 血缘 |
| `facts.py` | 财务事实、来源、口径和状态不变量 |
| `database.py` | SQLite/MySQL 差异、事实持久化、人工复核状态和宽表投影 |
| `task2.py` | 指标定义、公司解析、意图和查询规格 |
| `safe_query.py` | `QuerySpec`、`SessionState`、SQL AST 校验、行数限制和执行审计 |
| `sql_planner.py` | 参数化 SQL，以及 `VALIDATED` 事实输入约束 |
| `calculations.py` | 比率、期间比较、行业均值和多指标 Top-N 交集算子 |
| `kb.py` | 页级研报证据、元数据过滤、混合检索和可回验引用 |
| `evaluation.py` | 版本化金标契约、准确率指标和逐条失败明细 |
| `promotion.py` | 校验 job 私有 staging 数据并生成不可变提升计划 |
| `quality.py` | 答案表达和引用格式 |
| `pipeline.py` | 流程协调、导出和图表；不应继续增加独立业务规则 |
| `application.py` | CLI/API 可复用的可信问答服务及 HTTP Facade |
| `web_store.py` | Web 控制面迁移、作业、会话、查询快照和审核事件 |
| `api.py`、`api_models.py` | `/api/v1` 边界、文件安全、统一错误与 DTO |
| `runtime.py`、`worker.py` | 本地组合根、持久任务 worker 和财报入库 handler |
| `web/` | React 工作台；只消费版本化 API 契约 |

`dialog.py`、`query_engine.py` 和正则 SQL 校验已删除，避免会话、查询和安全规则出现第二实现。

## 核心不变量

1. 无法识别公司或报告期时必须进入复核或失败，禁止生成占位公司、代码或年份。
2. 所有财务候选都保存状态和来源；缺页码、PDF 总页数未知或页码超出真实范围的事实不得标记为 `VALIDATED`。
3. 问数 SQL 的业务指标必须匹配 `VALIDATED + consolidated` 事实；待复核或冲突事实不能参与筛选、排序、Top-N 或计算。
4. 用户值只能通过参数绑定进入 SQL；表名和字段名来自受校验 schema；仅允许单条只读 `SELECT`。
5. 超过行数上限、未知算子、零分母、无证据、连接失败和校验失败必须显式暴露。
6. 数值答案必须附事实键、来源文件、页码和表格行列信息；研报归因必须附页级原文片段。
7. 合成评测只验证评测器契约，不得作为真实财报准确率证据。
8. Web 请求和作业不得共享可变 Pipeline 或数据库连接；SQLite 每个工作单元使用独立连接。
9. 财务纠错创建替代事实，原事实只改变审核状态；审核决定必须写入不可变事件并检查版本。
10. API 不接受本机路径或任意 SQL；无认证部署只能监听回环地址。
11. Pipeline 只能写 job 私有 staging SQLite；事实、基础投影、产物、文档状态和任务终态必须在权威库的同一事务中提升。
12. 长任务必须持续续租；提升事务首尾都校验不可复用的 lease token 和文档当前尝试，旧 worker 失去租约后不得写入权威库。
13. 同一文档只能有一个当前入库尝试；失败或中断后只能从当前尝试创建一次原子 retry，禁止重试旧祖先任务。

## 已完成的结构性改造

- 建立 `financial_fact` 长表及 `VALIDATED / NEEDS_REVIEW / REJECTED` 状态。
- 增量状态升级到版本 3，并绑定抽取器版本；旧状态不会跳过事实层重建。
- 宽表改为已验证事实投影，SQL 额外内连接权威事实，防止未复核值影响结果集合。
- Task 2 和 Task 3 统一走 `compile -> AST validate -> SafeQueryExecutor`，SQLite/MySQL 使用各自参数占位符。
- 查询限制采用 `max_rows + 1` 探测；发生截断时拒绝回答，不把部分结果表述为完整结果。
- 会话统一为不可变 `SessionState`，支持话题切换和显式时间锚点；相对时间缺锚点时追问。
- 计算统一为注册算子，覆盖 `above_industry_mean`、`period_comparison`、`ratio` 和 `intersection_topn`。
- RAG 改为页级证据，支持公司、行业、发布日期过滤和引用回验。
- 建立真实/合成金标契约；无真实数据时指标为 `unavailable/null`。
- Web 控制面记录 PDF 总页数；每个任务先写独立 staging SQLite，再以自动续租、当前尝试和 lease token fencing 将结果原子提升到权威库。
- SQLite 迁移在 `BEGIN IMMEDIATE` 锁内检查和执行；MySQL 在同一连接取得命名锁后才执行基础 schema 和 Web 控制面迁移，API 与 worker 并发启动不会在锁外修改 schema。
- 落地 P0-P8 结构化 LLM 候选链路；拒绝/部分响应、Repair 篡改、候选遗漏、来源 hash 变化和跨阶段行列不一致均明确失败。
- DOCX 先经过受限 OOXML 检查和确定性 PDF 渲染，审计记录原件、衍生 PDF、渲染器和页数血缘；缺少渲染器时以 `DOCX_RENDERER_UNAVAILABLE` 失败。

## 当前明确风险

### P0：表格结构恢复仍未完成

当前抽取仍基于文本邻域和字段名推断，尚未可靠恢复跨页表格、当前/上期列、合并/母公司口径及表头单位。因此所有自动抽取均保持 `NEEDS_REVIEW`；代码不会将这些数值伪装成可信答案。下一阶段应让抽取候选直接携带原始 token、实际表头单位、行列标签和 scope，替换兼容抽取器。

新增 LLM 链路已经约束候选必须与 P1 表、scope、单位、页码、行列和当前期间表头一致。扫描页、低文本页、带财务报表标题或大尺寸栅格图像的混合页现在可由 RapidOCR 产生带物理页码、归一化 bbox、置信度、页面图像 hash 与引擎版本的 token；混合页同时保留原文本层，服务端会校验布局及最终事实来源不得篡改 OCR bbox。单页栅格化受像素上限保护，超限会明确失败。普通文本页仍没有可靠几何信息，OCR token 也不等于确定性的表格网格或跨页结构。该入口因此仍只生成审计包，不自动写 `financial_fact`；接入表格结构恢复、真实金标验收和 staging 提升事务前，不得把它描述为自动可信入库。

### P1：人工复核仍缺批量操作与可信身份

Web 工作台已提供单事实审核、PDF 页码定位、版本冲突提示和替代事实纠正，并复用 `FinanceDatabase.review_financial_fact()` 的原子状态变更与投影更新。当前仍缺批量复核和认证授权；本地未认证操作标识不能视为可信身份，多人协作必须在引入认证、权限和审计主体后再开放。

### P1：评测和集成证据不足

仓库没有获授权的真实财报/问题金标，因此不能声明 95% 或 98% 准确率。还需要 3 至 5 家公司、2 至 3 年、覆盖扫描件和跨页表格的数据集，并补真实 MySQL、LLM、PDF/OCR 和 Excel 输出集成验证。

### P2：编排模块仍偏大

`pipeline.py` 仍承载导出、绘图和部分抽取协调。后续拆分必须先固定现有事实、查询、证据和输出契约，不应与抽取算法重写同时进行。

## 变更决策原则

优先保证财务口径正确、来源可追溯和失败可见。只要输入事实、计算集合或引用证据不满足可信条件，就停止并报告原因；不通过增加关键词、默认年份或静默降级制造“成功”结果。
