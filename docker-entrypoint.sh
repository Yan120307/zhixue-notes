#!/bin/sh
set -e

echo "=== 智学笔记 Docker 启动 ==="

# 启动后端 API（8766）
echo "[1/2] 启动后端 API..."
python backend/server.py --port 8766 --host 0.0.0.0 &
BACKEND_PID=$!

# 等后端就绪
sleep 2

# 启动前端静态服务（8765）
echo "[2/2] 启动前端..."
python -m http.server 8765 --bind 0.0.0.0 --directory frontend &
FRONTEND_PID=$!

echo ""
echo "=== 启动完成 ==="
echo "  平台地址: http://localhost:8765"
echo "  后端 API: http://localhost:8766"
echo ""

# 任一进程退出则整体退出
wait -n
kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true