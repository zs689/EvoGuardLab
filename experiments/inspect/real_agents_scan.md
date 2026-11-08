# 真实框架实测报告：9 个 Agent 框架的静态检查

> 检索与下载：2026-11-08。安全边界：**全程只读文本，未执行、未 import、
> 未 pip install 任何被检代码**；解压走 `safe_extract.py`（拒绝路径穿越、
> 只保留文本后缀）；下载内容用完已删除，清点记录见文末。
> 机器可读数据：`real_agents_scan.json`。

## 一、一句话结论

覆盖了大厂（OpenAI ×2、Google、Microsoft、Hugging Face）、主流框架
（LangChain/LangGraph）和小厂（CrewAI、PydanticAI、Agno）共 **9 个框架、
76 个 Agent 定义**，但**可靠线索只有 2 条**。

这不是「没查出问题」，而是本次实测最重要的发现：
**第一版报出的 298 条里绝大多数是噪音，把它们消掉比多报几条有价值得多。**
教学示例里没写 timeout，不等于框架运行时没有 timeout。

## 二、覆盖范围

| 框架 | 厂商 / 类型 | 来源形式 | 定义数 | 提到 prompt | 提到 tools | 线索 |
|---|---|---|---|---|---|---|
| OpenAI Agents SDK | OpenAI · 大厂 | 整包 examples（按子目录拆） | 15 | 14 | 13 | 0 |
| OpenAI Swarm | OpenAI · 大厂 | 整包 examples | 6 | 6 | 1 | 0 |
| Google ADK | Google · 大厂 | 整包 contributing/samples | 19 | 19 | 3 | 0 |
| LangGraph | LangChain · 主流框架 | 整包 libs/cli/examples（按文件拆） | 1 | 0 | 1 | 0 |
| smolagents | Hugging Face · 大厂 | 整包 examples | 2 | 1 | 2 | 0 |
| Agno | Agno · 小厂 | 整包 cookbook | 20 | 20 | 9 | **2** |
| Microsoft AutoGen | Microsoft · 大厂 | 官方示例单文件（6 个） | 4 | 4 | 0 | 0 |
| PydanticAI | Pydantic · 小厂 | 官方示例单文件（8 个） | 7 | 5 | 6 | 0 |
| CrewAI | CrewAI · 小厂 | README + 库源码（2 个） | 2 | 1 | 2 | 0 |

**为什么有的框架只取了少量文件**：GitHub archive 整包走代理时会被截断
（crewAI 约 244MB、pydantic-ai 约 70MB、autogen 也损坏），
服务端不支持断点续传。改用 `ghfast` 的 raw 单文件通道，从公开目录页挑
官方示例文件构造迷你仓库。这在报告里如实标注，**不把「只扫了几个文件」
伪装成「扫了整个框架」**。

**为什么按子目录/按文件拆**：一个仓库里通常有多个 Agent。
全仓合并成一个定义时工具会堆到几十个，`tool_too_many` 必报——
那是抽样方式的问题，不是被检对象的问题。

**置信度全部 `low`**：定义是从源码里启发式提取的，prompt 边界和工具清单
都可能不全。这类结论只能当线索，不能当事实。

## 三、误报治理：298 条 → 2 条（本次实测的核心结果）

### 修复前

| 规则 | 命中情况 |
|---|---|
| `no_timeout` | 几乎所有框架 100% 命中 |
| `no_retry_policy` | 几乎所有框架 100% 命中 |
| `no_max_iterations` | 几乎所有框架 100% 命中 |
| `no_output_schema` | 几乎所有框架 100% 命中 |
| `prompt_missing` / `prompt_no_refusal_boundary` | 部分框架 100% 命中 |

合计 **298 条「线索」**。按项目里已经踩过的教训：
**一条规则如果命中率接近 100%，它就是噪音，不是发现。**

### 根因

这些都是**「缺失型」规则**——它们判断「某个东西不存在」。
但从源码猜出来的定义里，「没在文本里读到 timeout」不等于「运行时没有 timeout」：
配置可能在框架默认值、环境变量、外部 config 文件、部署平台里。
教学示例为了简洁本来就不写这些。拿它去判「缺配置」，判断的是提取器的局限。

### 修复

给静态规则加 `needs_high` 标记：13 条缺失型规则只在
`confidence=high`（用户显式提供的定义文件）上生效；
低置信度的仓库扫描一律跳过。低置信度下仍生效的只有 3 类**「存在型」证据**：
读到了 PII、读到了硬编码密钥、读到了命令执行类工具——这些是「看见了」，
不是「没看见」。

修复后：**76 个定义、2 条线索、0 条高命中噪音规则**。
并补了回归测试：显式定义缺配置仍会报，低置信度对象不报，
但 `unsafe_shell_tool` 这类存在型证据在低置信度下依然成立。

### 剩下的 2 条

都是 **Agno 的 `unsafe_shell_tool`（error）**：
示例里提供了执行任意命令/代码的工具，却没有沙箱说明。
这是「读到了工具名」得出的结论，属于可保留的线索；
但示例是教学代码，是否真有沙箱要看运行时环境，**需要人工复核**。

## 四、顺带补上的提取器适配（泛用性的实质）

第一版提取器只认「大写常量赋值三引号 prompt」和 `@tool` 装饰器，
扫 AutoGen / PydanticAI 时一个定义都拿不到。按各框架真实写法补齐并加了回归测试：

| 框架写法 | 例子 | 修复前 |
|---|---|---|
| AutoGen | `AssistantAgent(system_message="...")` | 0 个 prompt |
| Google ADK | `LlmAgent(instruction="...")` | 0 个 prompt |
| CrewAI | `Agent(role=..., goal=..., backstory=...)` | 0 个 prompt |
| PydanticAI | `@agent.tool` / `@agent.tool_plain` | 0 个工具 |
| 通用 | `system_prompt=( "a" "b" )` 多行拼接 | prompt 里混进源码引号 |

修复后：AutoGen 4/4、PydanticAI 5/7、Google ADK 19/19 提到 prompt，
PydanticAI 6/7 提到工具（连 docstring 一起抓，避免 `tool_desc_missing` 刷屏）。

## 五、这份报告不能说明什么

1. **「0 条线索」不等于「框架没问题」**：只跑了 20 条静态规则，
   而且低置信度下大部分规则被有意跳过。静态检查看不到运行时行为。
2. **示例 ≠ 生产配置**：教学示例缺审批/预算是简化，不是框架缺陷。
3. **扫的是示例，不是框架库源码**：把库代码当 Agent 定义会导致工具堆积、
   结论失真，所以按子目录/文件拆。
4. **LangGraph 只提取到 1 个定义**：它的顶层 `examples/` 只有 4 个通用脚本
   （无字面量 Agent 定义），改扫 `libs/cli/examples` 后仍只有 1 个有效对象。
   这是官方示例的形态，不是检查器崩溃。
5. **三个框架只取了少量代表性文件**（见第二节），覆盖率有限。
6. **Replit、Air Canada 这类闭源 Agent 拿不到定义**，无法对它们跑静态检查；
   对它们的分析在 `agent_failure_cases.md`，依据公开报道。

真正能查出运行时问题的是轨迹检查（`--trace`，25 条规则）。
两层要一起用。

## 六、安全与清理

- 下载通道：`ghfast.top` 代理 GitHub archive / raw；`data.jsdelivr.com` 目录接口。
  全程 `curl` 只写文件，不执行。
- 解压：`scripts/safe_extract.py` 拒绝 `..` 路径穿越、跳过非文本与超大文件。
- 执行：被检代码**从未运行**（`tests/test_static.py` 里有一条专门的回归测试：
  源码里放一行会写文件的语句，验证加载器没有执行它）。
- 清理：`scripts/cleanup_sandbox.py` 删除整个 `_sandbox`，
  并清点可执行文件后缀与体积，删除后校验目录不存在。
  **清点结果：见下方「清理记录」**（由清理脚本输出，不在这里预先编造）。

---

### 清理记录

```
待清理：11934 个文件，403.9 MB
可执行文件：0 个
下载物全程只读文本，未执行、未安装、未 import。
[√] 已删除 _sandbox（11934 个文件 / 403.9 MB），路径不存在
```

复核：`ls -d _sandbox` 返回 `No such file or directory`；
`experiments/inspect/` 下的报告与 JSON 数据均保留。
**结论：没有引入可执行文件，没有任何下载代码被运行，下载物已全部删除。**
