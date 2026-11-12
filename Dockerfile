# 开发 / 演示镜像，**不是生产部署基线**。
# 缺 TLS、反向代理、多租户隔离、集中式日志、持久化审计表与 schema 迁移、
# SLA / 容量 / 故障演练；见 docs/service-controls.md 与 docs/upgrade-report.md。
FROM python:3.11-slim

WORKDIR /app
COPY . /app

# 项目零运行时依赖；editable 安装只为提供 `evoguard` CLI 入口。
RUN pip install --no-cache-dir -e .

EXPOSE 8770
# 服务绑定 127.0.0.1；容器里如需外部访问，应由反向代理/端口映射负责，
# 不要在镜像里直接监听 0.0.0.0 并暴露无鉴权的接口。
CMD ["python", "ai-service/api/app.py"]
