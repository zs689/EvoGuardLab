from langchain_core.messages import SystemMessage
from langchain_core.tools import tool

SYSTEM_PROMPT = """你是一个运维助手，可以执行命令来排查问题。
先看日志，再决定要不要重启服务。"""

@tool
def restart_service(name: str) -> str:
    """重启服务"""
    return "ok"

@tool
def run_shell(cmd: str) -> str:
    return "done"
