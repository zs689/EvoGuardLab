# 测试夹具的来源与许可

## `injecagent_sample.jsonl`

含 **12 条** InjecAgent 样本正文，是 `tests/test_external_attacks.py` 的确定性回归夹具。
`FIX_SHA` 固定其 sha256，改动即触发失败（防止静默换语料后沿用旧数字）。

- 上游：UIUC Kang Lab / **InjecAgent**，**MIT** 许可；由
  `scripts/import_external_attacks.py` 按逐文件 sha256 校验后取得。
- 完整语料**不随本仓库分发**：`data/external/` 被 `.gitignore` 排除。
- 这 12 条是仓库内唯一随代码分发的语料正文，属**测试夹具**；
  保留 MIT 许可与来源标注，不转授、不声明为自有数据。
- 引用：Qiusi Zhan, Zhixiang Liang, Zifan Ying, Daniel Kang,
  *InjecAgent: Benchmarking Indirect Prompt Injections in Tool-Integrated
  Large Language Model Agents*, ACL 2024.

`experiments/semantic_injection_v1/frozen_split.json` 与 `semantic_injection_v2/`
的分组键里含 InjecAgent 的 `user_instruction` / `attacker_instruction` 原文，
同属 MIT 语料的引用，按同一许可与来源约束处理。

## 其余夹具

`tests/fixtures/` 下其它文件均为本项目自造的假数据：假 AWS key
（`AKIAIOSFODNN7EXAMPLE`，AWS 文档示例值）、假 OpenAI/GitHub token、
`127.0.0.1` 丢弃端口、模拟轨迹。**不含任何真实凭据、真实客户数据或真实业务流量**。

## 中文语料

`data/external/chinese/` 是本项目自建构造集（120 条间接注入 + 成对干净对照）
与许可清楚的公开中文越狱/有害请求语料，同样不随仓库分发；
来源与许可见 `docs/attack-datasets.md` 与各目录 `manifest.json`。
