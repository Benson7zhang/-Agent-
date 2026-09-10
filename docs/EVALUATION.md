# 评测数据契约

## 目标与边界

评测模块把“能运行”与“在真实财报上准确”分开。合成数据只验证解析、指标计算和失败明细是否工作，不能用于对外声明财报抽取或问数准确率。没有经过授权和标注的真实财报时，真实评测状态必须是 `unavailable`，指标值为 `null`。

当前契约版本是 `1.1`。不兼容修改必须提升版本，加载器会拒绝未知版本、未知字段、重复记录和不完整来源。

## Golden manifest

manifest 使用 JSON，负责声明数据集性质、可用性、注释文件和数值容差：

```json
{
  "schema_version": "1.1",
  "dataset_id": "annual-reports-golden-v1",
  "dataset_kind": "real",
  "availability": "available",
  "description": "Licensed and manually reviewed annual reports.",
  "annotation_files": ["facts-and-questions.jsonl"],
  "numeric_tolerance": {
    "absolute": "0.01",
    "relative": "0"
  },
  "provenance": {
    "annotation_version": "1.0.0",
    "review_status": "approved",
    "reviewers": ["reviewer-a", "reviewer-b"],
    "reviewed_at": "2026-09-06T12:00:00+08:00",
    "source_license": "internal-evaluation-only",
    "source_artifacts": [
      {"path": "reports/600000-2024.pdf", "sha256": "<64位SHA-256>"}
    ],
    "annotation_artifacts": [
      {"path": "facts-and-questions.jsonl", "sha256": "<64位SHA-256>"}
    ]
  }
}
```

- `dataset_kind` 只能是 `real` 或 `synthetic`。
- `availability` 只能是 `available` 或 `unavailable`。
- 精确小数以字符串保存，避免 JSON 浮点舍入改变金标。
- 注释路径必须相对 manifest，且不能逃逸 manifest 所在目录。
- 可用的真实数据集必须声明已批准的标注版本、至少两名不同审核人、带时区的审核时间和数据授权范围。
- 加载器会读取本地源文件与注释文件并核对 SHA-256；缺文件、缺哈希或哈希不一致均直接失败。
- 未获得真实数据时，使用空的 `annotation_files`、省略 `numeric_tolerance`，并填写 `unavailable_reason`。

## 注释记录

注释文件可使用 JSON 数组或逐行 JSON（JSONL）。事实记录的联合键为 `company_id + period + statement_scope + period_type + metric`，来源至少包含文件和从 1 开始的 PDF 页码：

```json
{"schema_version":"1.1","record_type":"financial_fact","record_id":"fact-001","company_id":"600000","period":"2024FY","statement_scope":"consolidated","period_type":"instant","metric":"total_assets","normalized_value":"1000.00","unit":"CNY_10K","currency":"CNY","source":{"source_file":"reports/600000-2024.pdf","page_no":8,"table_name":"合并资产负债表","row_label":"资产总计","column_label":"2024年12月31日"}}
```

问答记录显式声明答案类型，避免把证券代码等文本误当数值：

```json
{"schema_version":"1.1","record_type":"question_answer","record_id":"qa-001","question":"该公司 2024 年末资产总额是多少？","answer_type":"number","expected_answer":"1000.00","source":{"source_file":"reports/600000-2024.pdf","page_no":8}}
```

`source` 对事实是必填项，对问答是可选项。问答未标来源时不会进入来源定位准确率的分母。

## 预测文件

预测可使用 JSON 或 JSONL。JSONL 第一行必须是 `prediction_set`，用于阻止把一个数据集的预测误评到另一个数据集：

```json
{"schema_version":"1.1","record_type":"prediction_set","dataset_id":"annual-reports-golden-v1"}
{"schema_version":"1.1","record_type":"financial_fact_prediction","prediction_id":"pred-001","company_id":"600000","period":"2024FY","statement_scope":"consolidated","period_type":"instant","metric":"total_assets","normalized_value":"1000.00","unit":"CNY_10K","currency":"CNY","source":{"source_file":"reports/600000-2024.pdf","page_no":8,"table_name":"合并资产负债表","row_label":"资产总计","column_label":"2024年12月31日"}}
{"schema_version":"1.1","record_type":"question_answer_prediction","prediction_id":"pred-qa-001","question_id":"qa-001","answer":"1000.00","source":{"source_file":"reports/600000-2024.pdf","page_no":8}}
```

相同事实键或 `question_id` 出现多个预测时直接失败，不从多个候选中挑选最有利结果。

## 指标定义

| 指标 | 分子 | 分母 |
|---|---|---|
| `field_recall` | 联合键匹配到预测的事实数 | 金标事实数 |
| `cell_accuracy` | 联合键、标准值、单位和币种均正确的事实数 | 金标事实数 |
| `fact_precision` | 联合键、标准值、单位和币种均正确的事实数 | 预测事实数 |
| `question_accuracy` | 按声明类型和容差匹配的问答数 | 金标问答数 |
| `answer_precision` | 按声明类型和容差匹配的问答数 | 预测问答数 |
| `source_location_accuracy` | 来源文件、页码及金标中已填写的表名、行标签和列标签均匹配的记录数 | 带来源的事实和问答数 |

缺少相应金标或预测时，单项指标的 `status` 是 `unavailable`、`value` 是 `null`。错误值、缺失预测、来源错误和额外预测会写入 `failures`；额外事实或问答预测会降低相应精确率。

## Python 接口

```python
from smart_finqa.evaluation import evaluate_files, write_evaluation_report

report = evaluate_files("golden/manifest.json", "run/predictions.jsonl")
write_evaluation_report(report, "run/evaluation.json")
```

合成报告固定包含 `dataset_kind: synthetic`、`is_synthetic: true`、`provenance_verified: false` 和 `evidence_scope: synthetic_contract_only`。真实 manifest 声明为不可用时，报告整体 `status` 为 `unavailable`，不得用合成分数替代。只有源文件与注释文件哈希校验成功的真实数据集才会得到 `evidence_scope: verified_real_dataset`；审核人与授权信息仍属于 manifest 声明，不能替代外部审计。

仓库内 `tests/fixtures/evaluation/` 只包含虚构公司、数值和文件路径，用于回归测试评测器本身，不属于真实验收集。
