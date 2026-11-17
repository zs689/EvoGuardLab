# 最小镜像：核心库零第三方依赖，只需要标准库 + 项目代码
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml ./
COPY README.md* ./
COPY ai-service/ ./ai-service/
COPY evoguard_cli.py ./
# 企业模式启动校验要求 config/thresholds_v1.json 存在
COPY config/ ./config/
# app.py 启动要求 frontend 目录存在（看板 UI）
COPY frontend/ ./frontend/
# 不装任何 pip 包：dependencies = [] 是硬约束
ENV PYTHONPATH=/app/ai-service:/app/ai-service/evoguard \
    PYTHONUNBUFFERED=1

EXPOSE 8080
# app.py 从 argv 取端口；默认 8080
CMD ["python", "ai-service/api/app.py", "--port", "8080"]
