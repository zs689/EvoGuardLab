# 从 LlamaFirewall 学到的方法，以及融进本项目的部分

> 2026-11-10 · 来源：`E:/agent/learn/LlamaFirewall`
> 上游：`meta-llama/PurpleLlama` 的 LlamaFirewall，固定提交
> `f64eebd53003a87d9c2d2a3a712133534c214c8d`，目录内许可 **MIT**。

## 先说清楚读到的是什么

那份目录是**轻量源码学习版**（见 `下载说明.md:11-16`）：71 个上游文件、只保存源码/示例/测试/文档/许可证，
**没装依赖、没下模型权重、没跑过示例**。所以下面的结论是「读代码读出来的」，
不是「跑过上游验证了效果」。上游 PromptGuard 需要 HF 模型权重（torch/transformers）、
AlignmentCheck 示例需要 Together API 密钥和费用、CodeShield 需要 Semgrep —— 本机都没装，
按项目约定也不该为了学习去装。

## 它的三个方法

1. **按角色挂扫描器**（`src/llamafirewall/llamafirewall.py:90-96`、
   `config.py:13`）：`Configuration = Mapping[Role, Sequence[ScannerType]]`，
   USER / TOOL / ASSISTANT / MEMORY / SYSTEM 各自挂不同的扫描器，而不是所有文本过同一套规则。
2. **可插拔扫描器 + 轨迹回放**（`llamafirewall.py:29-43, 189-211`）：
   `register_scanner` / `create_scanner` 按名字懒加载；`scan_replay` 逐条消息带
   「此前轨迹」扫描，BLOCK 或人工复核立即停止；`scan_replay_build_trace` 只把 ALLOW 的消息加进轨迹。
3. **两类专门扫描器**：
   - `HiddenASCIIScanner`（`scanners/hidden_ascii_scanner.py:28-45`）查
     U+E0000..U+E007F 的不可见 tag 字符，并能解码出藏了什么；
   - `AlignmentCheckScanner`（`scanners/experimental/alignmentcheck_scanner.py:37-130`）
     不看「这句话像不像攻击」，而是拿**用户原始目标**对比**当前这一步动作**，
     用 few-shot 让模型输出 observation / thought / conclusion，命中转
     `HUMAN_IN_THE_LOOP_REQUIRED` 而不是直接 BLOCK。

## 融进来的三块（都改了代码，不是写个笔记）

### 1. 不可见字符：补上一个真实绕过

`ai-service/evoguard/security.py:340-460`。

本仓库原来靠 `_PRE` 前置子串做降噪（`security.py:358`），关键词被一个不可见字符拆开时，
前置检查失败、正则也匹配不到，**整个特征表等于被关掉**。改之前实测：

```
plain       -> ['injection_override']
tag-char    -> []          # 插 U+E004E
zero-width  -> []          # 插 U+200B
```

现在 `strip_invisible()` 给出两个视图：**剥离视图**（删掉隐藏字符，让 `Ig\u200Bnore` 重新连成
`Ignore`）和 **解码视图**（把 tag 字符还原成 ASCII，抓「整段指令都藏在 tag 里」），
`scan()` 两个视图都跑，并额外产出 `hidden_tag_chars` / `hidden_zero_width` /
`hidden_bidi_control` 三条可复核发现。改后五类绕过全部检出。

比上游多做两件事：
- 上游只查 tag chars，这里补了零宽字符和双向控制符（Trojan Source 那类）；
- 上游只「报一条发现」，这里**先归一化再检测** —— 只告警不归一化，绕过依旧存在。

性能：正则 C 层快速路径（`security.py:350-366`），无隐藏字符的文本不做 Python 逐字符循环。
10KB 实测 **0.37ms**，预算 1.5ms（`tests/test_security.py:86`）。

### 2. 按角色编排 + 轨迹回放 + 三档决策

新文件 `ai-service/evoguard/scan_pipeline.py`：
`Role` / `ScanDecision`（ALLOW / HUMAN_IN_THE_LOOP_REQUIRED / BLOCK）、
`Scanner` 基类与 `register_scanner`（:174）、`create_scanner`（:184）、
`ScanPipeline.scan`（:216）、`scan_replay`（:253）、`scan_replay_build_trace`（:262）、
`to_ci_action`（:271）把三档映射到本项目 CI 的 pass / review / block。

**刻意没照抄**：上游 `RegexScanner`（`regex_scanner.py:79-87`）命中邮箱、电话、信用卡号也直接
BLOCK。这里按 severity 分级：error → BLOCK，warn → 人工复核，info（如公开邮箱）→ ALLOW。
把 PII 命中当攻击命中会同时抬高「能力」和「误报」，本仓库 Q01 三真值口径不允许。

扫描器自身抛异常时不静默放行，转 `HUMAN_IN_THE_LOOP_REQUIRED` + `status=ERROR`。

### 3. 目标对齐审查（AlignmentCheck 的本地版）

`ai-service/evoguard/semantic_guard.py:173-215` 新增 `goal_alignment` prompt 与
`judge_alignment()`：输入「用户目标 + 历史 + 当前动作」，要求模型输出
observation / thought / reason 便于人工复核；命中 → 人工复核、不直接 BLOCK。

与上游的两处差异，都是刻意的：
- 用本地 Ollama，不依赖 Together API / 密钥 / 费用；
- 模型不可用或输出不可解析时，上游默认 `conclusion=True`（当作已沦陷），
  这里**记 N/A 但同样升级人工复核** —— 不猜结论，也不静默放行。

`scan_pipeline.py:134` 提供 `AlignmentScanner`，**默认不挂任何角色**（约 1 秒/次），
要显式配置 `ScanPipeline({Role.ASSISTANT: ["alignment"]})` 才启用。

## 上游的缺点（我认为不该照抄的）

1. **默认配置不含 HiddenASCII**：`llamafirewall.py:90-96` 的 `default_scanners` 里
   TOOL 是 `[CODE_SHIELD, PROMPT_GUARD]`、USER 是 `[PROMPT_GUARD]`，
   `HIDDEN_ASCII` 一次都没挂。也就是说「有扫描器，但默认不拦这类攻击」，
   除非用户自己写 Configuration。我们把它默认挂在 TOOL / MEMORY 上。
2. **RegexScanner 一命中就 BLOCK**，包括正常业务里的邮箱和手机号；
   而且和 PII / 注入混成一个 decision，没有 severity 分级，也没有落点概念。
3. **sync 与 async 行为不一致**：`scan()`（:108-167）跑完所有扫描器再取 BLOCK 优先，
   `scan_async()`（:169-187）遇到第一个 BLOCK 就直接返回；两者在「多个扫描器都命中」时
   给出的 reason / score 会不同。另外 sync 路径每条消息都 `asyncio.run(...)` 新建事件循环。
   本仓库统一成一条聚合逻辑，没有 async 分叉。

## 没融合的部分（以及为什么）

| 上游组件 | 不融合的原因 |
|---|---|
| PromptGuard 2（BERT 分类器） | 需要 HF 权重 + torch/transformers；本仓库已有 `deep_scan` 字符模型与 `semantic_guard` 本地语义判定，且第三方模型会让历史数字不可比 |
| CodeShield（Semgrep，8 语言） | 依赖外部静态分析引擎；本项目不生成代码，已有 20 条静态规则覆盖 Agent 定义文件 |
| PIICheck（实验性 LLM） | 已有 `pii_*` 正则且按 info/warn 分级；再挂一个 LLM 只增加延迟 |
| `from_usecase` 预设 | 预设只有 chatbot / coding_assistant 两种，与本仓库的四模式对照不是一回事 |

## 验证证据

- 绕过复现与修复：`tests/test_llamafirewall_merge.py:26-38`，5 类隐藏字符全部检出。
- 流水线：同文件 :84-190 共 23 项，含 replay 提前停止、被拦消息不入轨迹、
  扫描器异常转人工、默认配置不加载模型、info 不拦、warn 转 review。
- **历史指标不变**：重跑 `expanded_vectors_v1` / `eval_external_attacks` /
  `agentdojo_adapter` 的报告与明细逐字节一致；`jailbreak_eval_v1` 的召回/误报
  （0.041 / 0.036 / 0.004 / 0.000）逐项一致。
- 顺手修的可复现性问题：`eval_jailbreak_prompts.py` 原先把 p50/p95 耗时写进产物，
  两次重跑必然不同；现在耗时只在终端显示，`tests/test_reproducibility.py` 新增该链路的
  逐字节比对。
- 全量回归 **638 项全绿**（`python -m unittest discover -s tests -q`）。

## 边界

- 这套东西只提升**检测**，不提供执行隔离；隐藏字符被检出后是否真拦住，
  仍取决于调用方有没有采用 `guard_*` 的返回值（`security_effects` 那套契约不变）。
- 目标对齐审查默认关闭，且只用 4B 本地模型，不能宣称「已验证对齐检测有效」。
- 上游效果**没有在本机复现过**（没装依赖、没下模型），本文对上游的评价基于源码阅读。
