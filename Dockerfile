FROM python:3.11-slim

WORKDIR /app

# 系统依赖（matplotlib 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目
COPY backend/ backend/
COPY scripts/ scripts/
COPY frontend/ frontend/

EXPOSE 8766 8765

# 启动脚本：同时起后端 API + 前端静态服务
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

ENTRYPOINT ["./docker-entrypoint.sh"]