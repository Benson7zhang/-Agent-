# 财务事实复核流程

## 状态含义

- `NEEDS_REVIEW`：自动抽取候选。不能进入问数、筛选、排名或计算。
- `VALIDATED`：人工核对了公司、报告期、合并口径、数值、单位、页码和行列后确认。
- `REJECTED`：确认候选错误，并记录结构化问题原因。

自动抽取器不会直接生成 `VALIDATED`。`page_no`、`table_name`、`row_label`、`column_label`
任一缺失、为空或仅包含空白时，服务端都会拒绝确认。

## 待复核清单

以 SQLite 为例：

```sql
SELECT fact_key, stock_code, period, metric, raw_value, normalized_value,
       source_unit, target_unit, source_file, page_no, table_name,
       row_label, column_label, confidence, validation_issues
FROM financial_fact
WHERE validation_status = 'NEEDS_REVIEW'
ORDER BY stock_code, period, metric;
```

复核人必须回到 `source_file + page_no`，确认实际表名、行标签、当前期列、表头单位和合并范围。当前兼容抽取器无法可靠给出 `column_label`，因此其候选不应批量自动通过。

## 状态更新接口

```python
from smart_finqa.facts import ValidationIssue, ValidationStatus

# db 是已按项目 schema 初始化的 FinanceDatabase。
db.review_financial_fact(fact_key, ValidationStatus.VALIDATED)

db.review_financial_fact(
    fact_key,
    ValidationStatus.REJECTED,
    validation_issues=(ValidationIssue(code="wrong_period_column", message="候选值来自上期列", field="column_label"),),
)
```

`FinanceDatabase.review_financial_fact()` 会检查完整证据坐标、当前权威来源和同一业务键冲突；它不持有 Web
文档存储信息，因此离线调用前仍须独立核对 PDF 内容哈希和真实总页数。正式人工复核应使用 Web 接口。

Web 确认操作会在写入状态前检查完整证据坐标、页码是否落在关联 PDF 的真实总页数内，并核对
`financial_source.content_sha256`、`document.sha256` 与存储 PDF 当前字节哈希。内容不一致时返回
`source_content_mismatch`，坐标不完整时返回 `incomplete_fact_evidence`，事实、投影和审核事件均保持不变。
审核事件、事实状态、兼容宽表和文档状态在同一事务中更新。拒绝已确认事实时，对应投影值会被清除。

Web 复核通过 `POST /api/v1/facts/{fact_key}/decisions` 提交，并强制携带 `expected_version`。每次决定写入 append-only `fact_review_event`；过期版本返回 `409`，不会采用最后写入者覆盖。`CORRECT` 会创建新的替代事实、拒绝原事实并更新投影，原事实的数值和来源字段不会被原地修改。当前本地版本没有认证，操作者只能记录为“未认证的本地操作标识”，不能将其视为可信身份。

## 问数门槛

Task 2/3 启动前至少需要一条 `VALIDATED` 事实。每条生成 SQL 还会把 `metric + VALIDATED + consolidated` 作为绑定参数连接到事实表；返回结果之后再次检查唯一权威事实和数值一致性。任何冲突、缺失来源或勾稽异常都会显式失败。
