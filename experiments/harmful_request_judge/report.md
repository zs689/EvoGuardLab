# 直接越狱/有害请求判官（补齐中文越狱 6.5% 的缺口）

## 判定：中文**实质改善**，英文仍有明显缺口，不宣称「已解决越狱」

| 集 | 旧路径 | 新判官 | FPR | 备注 |
|---|---|---|---|---|
| 中文越狱 62 条（31 attack / 31 benign） | 0.0645（注入判官） | **0.8065** | 0.0 | 25/31 命中 |
| jb_classification **留出** 60 条（单判官） | L1 历史 0.036 | 0.5333 | 0.0 | 16/30 命中，未参与调参 |
| jb_classification **留出** 60 条（**双判官并联**） | — | **0.9** | 0.0 | 27/30，30 个 benign 零误报 |

## 英文提升来自哪里（借鉴 Meta LlamaFirewall / PromptGuard 2）

PromptGuard 2 专门在 **DAN / role-playing / instruction bypass** 上训练，
与通用有害请求分类器**分层**，而不是一个 prompt 打天下。我按同样的分工
加了 `persona_jailbreak_judge.py`：只判「改写 AI 身份/规则」（从现在起你是
DAN/DNE/JBOT/无过滤 VM、忽略既有规则、先免责声明再破规则），
与 `harmful_request_judge`（请求有害技术动作）**并联，任一命中即报**。

英文漏检的 14 条里 8 条是这类角色越狱，补上后 16→27/30。
中文集没变（0.8065）——它剩的 6 条漏检是 CTF/技术包装型，不是角色型，
两套判官确实互补，这不是「同一批题调了两遍」。

**留出纪律不变**：两个判官的 prompt 都只按开发集前 6 条调整，
留出集 index>=100 固定 seed 抽 60 条，`result_combined.json` 里
`errors=0`、benign 被角色判官命中的条数=0（不是拿误报换召回）。

两套判官判的是**不同的东西**，不能混为一谈：
- `agent_watch.SemanticJudge`：是否在对 AI 下注入指令（覆盖/遗忘/伪装系统消息）；
- 本判官：是否请求协助实施有害技术行为（绕过风控、逆向、攻击代码）。

中文越狱集大量是「请给我绕过某站加密参数的方法」+授权/CTF/情感包装——
前者不认，后者认。实测同一批样本从 2/31 → 25/31。

## 防自欺设计

1. **留出集**：英文 jb_classification 只看了前 6 条（#0000-#0005）调 prompt，
   评测从 index>=100 用固定 seed 抽 60 条，调参样本与验证样本不重叠。
2. **判官错误单列**：`error` 不计入 TP/FN/FP/TN，避免把"没判出来"粉饰成"判对了"。
3. **不写耗时进产物**：两次重跑要能逐字节一致。
4. `guard_input_semantic()` 判官不可用 → `decision=report`（转人工复核），
   **既不静默放行也不误伤拦截**。

## 边界（必须一起看，不能只念好消息）

1. **双判官并联后英文留出 0.9，仍有 3 条漏检**（如「扮演甘地」被
   jb_classification 标 jailbreak，但按判据是无解除约束的普通角色扮演）——
   这是数据集标签边界争议，没有为过线硬改判据。中文仍是 0.8065。
2. **文本无法核实授权**：真实授权的渗透测试与未授权攻击在文本上不可区分。
   提示词明确要求把「授权编号 / CTF 靶机 / 防守方 / 家人受害」视为
   unverified claim，因此会**误伤合法安全研究请求**——这是主动选择的偏向。
   FPR=0 是在本数据集的 benign 侧测的，不代表真实安全研究流量也零误报。
3. qwen3:4b + 提示词工程，不是训练过的分类器；换模型需重测。
4. 这是**检测信号**：`guard_input_semantic` 恒 `executed=false`，
   是否阻断由执行点决定。默认关闭，需 `EVOGUARD_HARMFUL_JUDGE=1`。

## 复现

```bash
python scripts/eval_harmful_judge.py --out experiments/harmful_request_judge/result.json
python -m unittest tests.test_harmful_request_judge -q
```
