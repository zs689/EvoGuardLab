# 最小镜像：核心库零第三方依赖，只需要标准库 + 项目代码
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY ai-service/ ./ai-service/
COPY evoguard_cli.py ./
# 不装任何 pip 包：dependencies = [] 是硬约束
ENV PYTHONPATH=/app/ai-service:/app/ai-service/evoguard \
    PYTHONUNBUFFERED=1

EXPOSE 8080
# app.py 从 argv 取端口；默认 8080
CMD ["python", "ai-service/api/app.py", "--port", "8080"]
