# EvoGuardLab 本地演示镜像（EG-003 / EG-016 的一部分）
#
# 边界先说清楚：这**不是**完整生产拓扑。它解决的是「构建时不夹带本地数据」
# 和「容器能跑起来」；真正的企业部署还需要 TLS 反代、集中身份源、
# 独立审计库、备份/恢复与滚动升级 runbook，见 docs/enterprise-readiness.md。
#
# 安全默认值：容器内绑 0.0.0.0，但**必须**置于 TLS 反代之后并设置
# AGENTLAB_API_TOKEN。裸暴露这个端口等于把无鉴权接口放到网络上。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTLAB_BIND=0.0.0.0 \
    AGENTLAB_DATA_ROOT=/app/data \
    AGENTLAB_DB=/app/data/evoguard.db

WORKDIR /app

# 显式 COPY 清单 —— 不用 `COPY . /app`。
# .dockerignore 是第二道防线；两层独立，任一层被改坏另一层还在。
COPY pyproject.toml README.md LICENSE evoguard_cli.py ./
COPY ai-service/evoguard ./ai-service/evoguard
COPY ai-service/api/app.py ./ai-service/api/app.py
COPY config ./config
COPY frontend ./frontend
COPY docs/taxonomy.md ./docs/taxonomy.md
COPY experiments/metrics.json ./experiments/metrics.json
COPY scripts/defense_demo.py ./scripts/defense_demo.py

# 零运行时依赖；editable 安装只为提供 `evoguard` CLI 入口。
RUN pip install --no-cache-dir -e . \
 && useradd --create-home --uid 10001 evoguard \
 && mkdir -p /app/data \
 && chown -R evoguard:evoguard /app/data

# 数据只写独立卷，镜像本身保持只读友好。
VOLUME ["/app/data"]
USER evoguard
EXPOSE 8770

# 走轻量存活探针 /api/live：不碰数据库、不进业务 SLI。
# 若部署时开了鉴权，探针请求会自动带上同一个 token。
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request as u; t=os.environ.get('AGENTLAB_API_TOKEN'); h={'Authorization':'Bearer '+t} if t else {}; r=u.urlopen(u.Request('http://127.0.0.1:8770/api/live',headers=h),timeout=2); sys.exit(0 if r.status==200 else 1)"

STOPSIGNAL SIGTERM
CMD ["python", "ai-service/api/app.py"]
