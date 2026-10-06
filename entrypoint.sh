#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] 等待 PostgreSQL 就绪 (${POSTGRES_HOST:-db}:${POSTGRES_PORT:-5432}) ..."
until python -c "
import os, sys, psycopg2
try:
    psycopg2.connect(
        host=os.getenv('POSTGRES_HOST', 'db'),
        port=int(os.getenv('POSTGRES_PORT', '5432')),
        dbname=os.getenv('POSTGRES_DB', 'labflow'),
        user=os.getenv('POSTGRES_USER', 'lab'),
        password=os.getenv('POSTGRES_PASSWORD', 'labpass'),
        connect_timeout=2,
    ).close()
except Exception:
    sys.exit(1)
" 2>/dev/null; do
  sleep 1
done
echo "[entrypoint] 数据库可连接，执行初始化 ..."
python -m app.bootstrap

echo "[entrypoint] 启动 Web 服务 ..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT:-8000}"
