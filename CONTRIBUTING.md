# 开发与贡献规范

## 环境准备

项目支持 Python 3.10 及以上版本。开发环境使用仓库内虚拟环境，依赖以 `pyproject.toml` 为准。

PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

MySQL 开发需额外安装：

```bash
python -m pip install -e ".[mysql,dev]"
```

## 日常工作流

1. 从最新 `main` 创建短生命周期分支，建议使用 `feat/`、`fix/`、`refactor/`、`docs/` 前缀。
2. 先用测试或最小复现确认当前行为，再修改实现。
3. 保持改动聚焦；共享业务规则只保留一个实现和一个事实来源。
4. 更新受影响的测试、配置样例和用户文档。
5. 提交前运行完整质量检查并审阅 diff。

提交信息使用简洁的 Conventional Commits 格式，例如：

```text
feat: add annual report metric mapping
fix: reject unknown pipeline modes
docs: document MySQL development setup
```

## 代码规范

- Ruff 是统一的 lint 和格式化工具，行宽为 120；不要手工维护另一套格式规则。
- 类型标注覆盖公开函数、跨模块数据结构和非显然返回值。
- 函数保持单一职责和浅层控制流；编排与业务计算分离。
- 注释解释财务口径、输入约束和设计取舍，不复述代码。
- 日志使用结构化上下文，不记录 API Key、数据库密码或完整敏感原文。
- 允许受控降级时，必须限定异常类型并记录原因；显式选择的外部服务不得静默替换。

格式化和检查：

```bash
ruff format .
ruff check .
pytest
```

需要查看覆盖率时运行：

```bash
pytest --cov=smart_finqa --cov-report=term-missing
```

## 测试要求

- 纯解析、计算和规划逻辑使用快速单元测试。
- 缺陷修复必须包含能在修复前失败的回归用例。
- 数据库改动至少覆盖 SQLite；涉及方言差异时增加 MySQL 集成测试。
- LLM 功能用协议级 fake 验证请求与响应，并为真实服务保留可选 smoke test。
- 输出契约改动需校验 Excel 列名、JSON 可解析性、图表路径和运行日志。
- 测试不能依赖开发者本机路径、真实密钥、网络或未纳入测试夹具的数据。

## 配置与数据安全

- 本地默认使用 SQLite；只有显式设置 `DB_BACKEND=mysql` 才连接 MySQL。
- `.env.example` 只列出变量名，不放真实值；`.env*` 已被 Git 忽略。
- YAML 中 `password` 或 `api_key` 为空时，分别读取 `MYSQL_PASSWORD`、`LLM_API_KEY`。
- 原始 PDF、Excel 数据、`outputs/`、`result/` 和 `result_*.xlsx` 均属于本地资产，不得提交。

## Pull Request 检查清单

- 变更范围和动机清晰，没有顺带重写无关模块。
- 新行为有测试，现有测试、Ruff lint 和格式检查全部通过。
- 没有静默降级、重复规则、硬编码时间基准或敏感信息。
- 用户可见行为、配置或输出契约变化已更新文档。
- 已注明未执行的集成验证和剩余风险。
