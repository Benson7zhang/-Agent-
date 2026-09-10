# AGENTS.md

## 项目目标

本仓库维护财报解析、结构化入库、财务问答与结果导出流水线。变更应优先保证财务口径正确、结果可追溯、失败可见，以及 SQLite/MySQL 行为一致。

## 开始工作前

1. 阅读 `docs/ARCHITECTURE.md`，确认变更所属模块和已有风险。
2. 阅读 `CONTRIBUTING.md`，使用仓库约定的环境和检查命令。
3. 检查工作区状态；不得覆盖无关的未提交改动。

## 单一事实来源

- 依赖、Python 版本和工具配置：`pyproject.toml`。
- 用户使用方法：`README.md`。
- 开发流程和提交要求：`CONTRIBUTING.md`。
- 架构边界、技术债和演进顺序：`docs/ARCHITECTURE.md`。
- 业务指标定义：`smart_finqa/task2.py` 中的 `TASK2_METRICS`。

不要在脚本、README 和依赖文件中维护相互冲突的版本或默认值。

## 架构边界

- `config.py` 只负责配置读取与校验，不连接外部系统。
- `ingestion.py` 和 `extraction.py` 负责输入解析，不写数据库。
- `database.py` 封装数据库差异，不承载问答业务规则。
- `planner.py`、`task2.py`、`sql_planner.py` 负责意图和查询计划，不执行 I/O。
- `pipeline.py` 只做流程协调；新增复杂逻辑应进入职责明确的模块。
- `quality.py` 负责答案表达和质量规则；图表与导出逻辑后续从 `pipeline.py` 独立时保持接口可测试。

## 强制约束

- 禁止把密钥、口令、真实财报、生成数据库和结果文件提交到仓库。
- 用户显式选择 MySQL 或外部 LLM 后，连接/调用失败必须可见；不得静默切换后端或伪造成功结果。
- 外部输入在边界处校验；无效配置、未知运行模式和不安全 SQL 必须明确失败。
- SQL 值使用参数绑定；动态表名和字段名只能来自经过校验的 schema 白名单。
- 不新增硬编码年份、公司或财务指标分支；时间基准应来自问题、报告或显式配置。
- 修复缺陷时先补回归测试，再修改实现；不得靠宽泛 `except Exception` 隐藏错误。

## 完成标准

```bash
ruff check .
ruff format --check .
pytest
```

涉及 MySQL、LLM、真实 PDF 或 Excel 输出契约时，除单元测试外还需记录对应集成验证；无法运行时必须在交付说明中明确指出。
