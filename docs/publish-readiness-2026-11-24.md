# GitHub 发布前自查（2026-11-24）

- 敏感信息：**未发现真实凭据**（28 处命中全为测试夹具，逐条列明）
- git 历史：无真实密钥；仅 1 个故意的假 key 测试夹具
- 已清理：16 个调试日志移出跟踪、1 处本地路径注释

```json
{
  "version": 1,
  "checked_at": "2026-11-24T12:0x",
  "purpose": "GitHub 公开发布前自查",
  "secret_scan": {
    "result": "未发现真实凭据",
    "scanned": "927 个被跟踪文件 + git 历史（--diff-filter=A 全量）",
    "hits_reviewed": 28,
    "all_false_positive": true,
    "details": [
      {
        "pattern": "AKIA…",
        "where": "docs/attack-datasets.md 等 7 处",
        "verdict": "AWS 官方示例假密钥 AKIAIOSFODNN7EXAMPLE"
      },
      {
        "pattern": "ghp_…",
        "where": "scripts/expanded_vectors_v1.py:137",
        "verdict": "假 GitHub token ghp_1234…"
      },
      {
        "pattern": "token/secret 赋值",
        "where": "scorecard.py:137 / drill.py:25 / loadtest.py:41 / review_checks.py 等",
        "verdict": "自造测试 token（probe-/drill-/loadtest-/review-probe-）与 Hunter2Hunter2"
      },
      {
        "pattern": "PRIVATE KEY",
        "where": "tests/test_openai_proxy.py 等 5 处",
        "verdict": "截断的假 PEM（带 ...），不可解析"
      },
      {
        "pattern": "sk-…",
        "where": "tests/fixtures/agent_secret.json",
        "verdict": "故意的测试夹具：模拟泄漏假 key 的 agent，key=abcdefghijklmnopqrstuvwxyz123456"
      }
    ]
  },
  "history_check": {
    "sensitive_files_ever_added": [
      "tests/fixtures/agent_secret.json（假）"
    ],
    "real_credentials_found": false
  },
  "cleaned": [
    "把 16 个运行期调试日志从 git 跟踪移出（git rm --cached，本地文件保留）",
    ".gitignore 加 *.log，并 !experiments/archive/** 保留 2 个历史证据日志",
    "ai-service/evoguard/evidence_gate/__init__.py:4 注释里的本地路径 e:/agent/... 改为相对描述"
  ],
  "open_questions_for_owner": [
    "docs/review-remediation/ 与 *_progress.md 含内部审计过程（27 项发现、自我下调分数）。公开与否是产品决策，我未改动。",
    "tests/fixtures/agent_secret.json 文件名可能触发 GitHub secret scanning 提示（内容为假），如介意可改名。",
    "test_openai_proxy.py 的截断 PEM 可能触发提示（不可解析），如介意可改成字符串拼接。"
  ],
  "recommend_before_publish": [
    "在 GitHub 仓库设置里开启 Secret scanning + Push protection（防止以后误推）",
    "确认 LICENSE（MIT）与已有依赖许可兼容",
    "确认 README 里的 demo 命令在干净环境可跑（无本地模型时应有明确降级提示）"
  ]
}
```
