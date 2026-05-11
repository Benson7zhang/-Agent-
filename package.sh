#!/bin/bash
set -euo pipefail

PROJECT_NAME="$(basename "$PWD")"
STAMP="$(date +%Y%m%d_%H%M%S)"
ZIP_NAME="${PROJECT_NAME}_submit_${STAMP}.zip"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR/$PROJECT_NAME/outputs" "$TMP_DIR/$PROJECT_NAME/result"

rsync -a ./ "$TMP_DIR/$PROJECT_NAME/" \
  --exclude '.git/' \
  --exclude '.DS_Store' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'outputs/*' \
  --exclude 'result/*' \
  --exclude 'result_*.xlsx' \
  --exclude '数据/' \
  --exclude '样例数据/' \
  --exclude '测试数据/' \
  --exclude '全量数据/' \
  --exclude '*.db' \
  --exclude '*.sqlite' \
  --exclude '*.sqlite3' \
  --exclude '*.log' \
  --exclude '*.pdf' \
  --exclude '*.xlsx' \
  --exclude '*.xls' \
  --exclude '.skill' \
  --exclude '.skills/' \
  --exclude '.codex/' \
  --exclude '.agents/'

mkdir -p "$TMP_DIR/$PROJECT_NAME/outputs" "$TMP_DIR/$PROJECT_NAME/result"
(
  cd "$TMP_DIR"
  zip -qr "$OLDPWD/$ZIP_NAME" "$PROJECT_NAME"
)

echo "Created $ZIP_NAME"
