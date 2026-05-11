#!/bin/bash
# 一键运行脚本 - 自动处理所有配置

echo "================================"
echo "  财报智能问答Agent 系统 - 一键运行"
echo "================================"
echo ""

# 检查依赖
echo "1. 检查依赖..."
pip3 install -q openpyxl pypdf pandas matplotlib pyyaml psutil mysql-connector-python pytest 2>/dev/null
echo "   ✓ 依赖已安装"
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
echo "   1) 完整流程 (入库+任务2+任务3) - 推荐"
echo "   2) 仅入库"
echo "   3) 仅任务2"
echo "   4) 仅任务3"
echo ""
read -p "请选择 [1-4，默认1]: " choice
choice=${choice:-1}

case $choice in
    1) MODE="all" ;;
    2) MODE="ingest" ;;
    3) MODE="task2" ;;
    4) MODE="task3" ;;
    *) MODE="all" ;;
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
python3 run_pipeline.py \
    --mode $MODE \
    --full-data $FULL_DATA \
    --workers 4 \
    --log-level INFO

echo ""
echo "================================"
echo "  运行完成！"
echo "================================"
echo ""
echo "输出文件:"
echo "  - 数据库: outputs/finance.db"
echo "  - 任务2: result_2.xlsx"
echo "  - 任务3: result_3.xlsx"
echo "  - 图表: result/*.jpg"
echo "  - 日志: outputs/smart_finqa.log"
echo ""
