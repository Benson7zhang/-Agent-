#!/usr/bin/env bash
set -euo pipefail

# 一键运行脚本。依赖安装由开发环境初始化阶段完成。

echo "================================"
echo "  Smart FinQA 财报智能问答系统"
echo "================================"
echo ""

if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="python3"
fi

echo "1. 检查环境..."
if ! "$PYTHON" -c "import matplotlib, openpyxl, pandas, psutil, pypdf, smart_finqa, yaml" 2>/dev/null; then
    echo "   依赖未安装，请先执行: python -m pip install -r requirements.txt" >&2
    exit 1
fi
echo "   环境可用"
echo ""

# 自动选择数据目录
if [ -d "数据/测试数据" ]; then
    DATA_MODE="测试数据"
    FULL_DATA="true"
elif [ -d "数据/全量数据" ]; then
    DATA_MODE="全量数据"
    FULL_DATA="true"
else
    DATA_MODE="样例数据"
    FULL_DATA="false"
fi

echo "2. 数据模式: $DATA_MODE"
echo ""

# 询问运行模式
echo "3. 选择运行模式:"
echo "   1) 完整流程 (已有人工复核事实时使用)"
echo "   2) 仅入库并生成复核清单 - 首次运行推荐"
echo "   3) 仅任务2"
echo "   4) 仅任务3"
echo ""
read -r -p "请选择 [1-4，默认2]: " choice
choice=${choice:-2}

case $choice in
    1) MODE="all" ;;
    2) MODE="ingest" ;;
    3) MODE="task2" ;;
    4) MODE="task3" ;;
    *)
        echo "无效选择: $choice" >&2
        exit 2
        ;;
esac

echo ""
echo "4. 开始运行..."
echo "   - 模式: $MODE"
echo "   - 数据: $DATA_MODE"
echo "   - 日志: outputs/smart_finqa.log"
echo ""
echo "================================"
echo ""

# 运行
"$PYTHON" run_pipeline.py \
    --mode "$MODE" \
    --full-data "$FULL_DATA" \
    --workers 4 \
    --log-level INFO

echo ""
echo "================================"
echo "  运行完成！"
echo "================================"
echo ""
echo "请查看命令输出和 outputs/run_log.json 获取本次实际生成的文件。"
echo ""
