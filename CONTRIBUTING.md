# Contributing (local only)

本仓库目前**只在本地工作**：不创建远程仓库、不推送、不发布制品、不对外发消息。
这份文件是给未来的本地贡献者/接手 AI 看的，不是公开招募。

## 三条硬规矩

1. **保留离线零依赖 demo**。运行时依赖是 `[]`，装不上 torch 的 Windows 机器
   也必须能跑出同一批数字。想引入第三方库，先证明它不影响这条边界；
   企业依赖走可选安装或独立部署入口。
2. **六字段契约不许动**：`detected / decision / attempted / executed / effect / reason`。
   「检测建议 ≠ 实际拦截」是这个项目的立身之本，任何报告不得把 `detected`
   当成 `executed` 来写。
3. **文档里的承诺要跟代码对齐**。写了「已实现」就得能指出可复验的命令或产物。
   `docs/api-contract-v1.json` 有 `tests/test_api_contract.py` 守着，改动路由
   必须同步契约。

## 改代码前先做什么

```bash
python scripts/review_checks.py --self-test    # 先用已知坏样本证明检查会红
python scripts/review_checks.py --gate          # 逐项客观门槛（EG-023）
python -m unittest discover -s tests -q        # 全量回归
python scripts/check_docker_context.py         # 构建上下文
```

按 `docs/review-remediation/README.md` 的台账看当前还有哪些缺口，别重复修。
审查的交接规则要求：**先用验收复现差距，再修改，保留修前/修后证据**。
`docs/review-remediation/before/` 与 `after/` 就是这么来的。

## 改完之后

- 新增行为要有测试；负结果也要有测试（例如「清理后链仍可验」「手工删行仍被抓」）。
- 改了指标名，同步 `deploy/alerts.yml`，否则告警永远不响且 yaml 语法还「正常」。
- 重跑实验写到**独立输出目录**，记录环境、提交、配置；不要覆盖冻结产物。
- 提交信息写清「修了什么、怎么验的、有什么没做」。不要写「应该没问题」。

## 提交检查清单

- [ ] `python scripts/review_checks.py --self-test` 全过（检查工具没退化）
- [ ] `python scripts/review_checks.py --gate` 全绿，且 **0 skip**（skip 默认算失败）
- [ ] `python -m unittest discover -s tests -q` 全绿，并把总数/skip 记进报告
- [ ] 干净 clone 再跑一次（不要拿 editable 安装冒充制品测试）
- [ ] `git status` 干净，没有本地数据库/语料/凭据被带进去
- [ ] 文档数字与实际命令输出一致
- [ ] 知道 `evoguard demo` 会**重写** `experiments/defense_demo_v2/demo.json`（重新生成证据，
      属正常）；只是想看结果不想改仓库时，跑完 `git checkout -- experiments/` 或用
      `--out` 指到临时目录
