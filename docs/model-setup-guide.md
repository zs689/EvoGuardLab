# EvoGuardLab 模型接入指南（key 填在哪里）

一句话：**EvoGuardLab 模型无关，key 放在「系统环境变量」，不在代码里。** 换模型只改 `EVOGUARD_PROVIDER` 一行。

## 1. 我能接哪些模型？

运行命令看完整列表（含每个服务实时检测到 key 填没填）：

```bash
python scripts/setup_model.py
```

| 服务 | `EVOGUARD_PROVIDER` | key 环境变量 | 默认模型 |
|---|---|---|---|
| OpenAI | `openai` | `OPENAI_API_KEY` | gpt-4o-mini |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | deepseek-chat |
| 通义千问（云） | `qwen` | `DASHSCOPE_API_KEY` | qwen-plus |
| 智谱 GLM | `glm` | `ZHIPU_API_KEY` | glm-4-flash |
| Kimi 月之暗面 | `kimi` | `MOONSHOT_API_KEY` | moonshot-v1-8k |
| Groq | `groq` | `GROQ_API_KEY` | llama-3.3-70b-versatile |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` | openai/gpt-4o-mini |
| 硅基流动 | `siliconflow` | `SILICONFLOW_API_KEY` | Qwen/Qwen2.5-7B-Instruct |
| MiniMax | `minimax` | `MINIMAX_API_KEY` | MiniMax-Text-01 |
| 百川 | `baichuan` | `BAICHUAN_API_KEY` | Baichuan4 |
| 火山方舟 | `volcengine` | `ARK_API_KEY` | doubao-seed-1-6 |
| Gemini | `gemini` | `GEMINI_API_KEY` | gemini-2.0-flash |
| Mistral | `mistral` | `MISTRAL_API_KEY` | mistral-small-latest |
| Together | `together` | `TOGETHER_API_KEY` | … |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | claude-3-5-haiku-latest |
| 本地 LM Studio/vLLM/llama.cpp | `local_openai` | 无需 | 自动识别 |
| 本地 Ollama | `ollama` | 无需 | 自动识别 |

**任何未列出的服务**：只要兼容 OpenAI `/v1/chat/completions`，用 `backend=openai_compat` + 自填 `EVOGUARD_MODEL_BASE` / `EVOGUARD_MODEL_MODEL` 即可。所以覆盖面是"几乎所有"。

## 2. key 到底填在哪（你问的重点）

**优先级**：系统环境变量 > 项目 `.env`。项目启动时会自动读取根目录 `.env`（零依赖加载器），但**不会覆盖**已设的系统环境变量——所以你系统里填的 key 永远优先。

### Windows（当前环境）—— 系统环境变量，二选一

**方式 A：PowerShell 命令（推荐，永久生效）**

```powershell
# 1) 选服务（例：DeepSeek）
[Environment]::SetEnvironmentVariable('EVOGUARD_PROVIDER','deepseek','User')
# 2) 填 key（换成你自己的真实密钥）
[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY','sk-你的真实key','User')
# 3) 重开终端，验证：
python scripts/setup_model.py --check
```

**方式 B：图形界面**
`设置 → 系统 → 关于 → 高级系统设置 → 环境变量 → 用户变量 → 新建`
- 变量名 `DEEPSEEK_API_KEY`，变量值你的密钥
- 再新建一个 `EVOGUARD_PROVIDER` = `deepseek`
- 确定后重开终端

### 也可以写进项目 `.env`（已被 gitignore，不会泄露到仓库）

编辑 `E:/agent/EvoGuardLab/.env`：
```
EVOGUARD_PROVIDER=deepseek
```
key 仍建议放系统环境变量（`.env` 里只写服务名）。

## 3. 当前状态

项目 `.env` 已设为 `EVOGUARD_PROVIDER=deepseek`。你只需：
1. 设置系统环境变量 `DEEPSEEK_API_KEY`
2. 重开终端
3. `python scripts/setup_model.py --check` 看到 key `[已设置 ✅]` 即可

## 4. 安全说明

- key **不会**写进日志、错误响应或 `describe()` 输出（有回归测试守护）。
- 未指定 key 环境变量时，本地无鉴权端点（Ollama/LM Studio）照常工作；云端预设若缺 key 会明确报 `E_LLM_AUTH`，不会静默吃 401。
- 本机 Ollama 程序与 qwen3:4b 模型已卸载/删除（2026-11-20），备份在 `E:\agent\_backup_ollama_20261120\`。适配器代码保留，将来连远端 Ollama 只需 `EVOGUARD_PROVIDER=ollama` + 改 base_url。
