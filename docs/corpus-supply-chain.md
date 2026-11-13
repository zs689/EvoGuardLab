# 外部语料供应链（EG-018）

外部攻击语料**不入库**（许可 + 体积），但「用了什么、从哪来、许可是什么、
有没有被改过」必须可核对。这份文档说明现状、校验方式，以及**还没锁住的部分**。

## 一、现在锁住了什么

`data/corpus_lock.json`（入库，不含语料本体）为每个语料源记录：

| 字段 | 含义 |
|---|---|
| `repo` | 上游仓库 |
| `commit` | 上游不可变版本 —— **目前是 `null`**（见 §三） |
| `pin_status` | `pinned` / `unpinned` |
| `license` / `license_url` | 逐资产许可 |
| `cases_sha256` / `cases_bytes` / `cases_lines` | **生成产物**的指纹 |
| `upstream_files` | manifest 里登记的上游文件数（每个都有 sha256/bytes） |

校验：

```bash
python scripts/verify_corpus.py           # 篡改类问题判失败；缺许可/未锁定只告警
python scripts/verify_corpus.py --strict  # 告警也算失败
python scripts/verify_corpus.py --write-lock   # 重新生成锁清单
```

- 语料目录整体不存在（干净 clone）→ 输出「未导入」，**不算失败**，
  依赖语料的测试按 `skipUnless` 跳过。
- `cases.jsonl` 被改一个字节、行数变化、manifest 损坏 → **失败**。
- 缺 `license` / 上游 commit 未锁定 → 告警；`--strict` 下失败。

## 二、逐资产许可现状（本轮实测）

```
agentdojo                 cases=True  lines=120   pin=unpinned  license=MIT
chinese                   cases=True  lines=413   pin=unpinned  license=None
chinese_jailbreak         cases=True  lines=62    pin=unpinned  license=None
injecagent                cases=True  lines=2108  pin=unpinned  license=None
jailbreak_classification  cases=False              pin=unpinned  license=None
```

- **4 个源缺 `license` / `license_url`**：需要回填上游许可证名称与链接。
  在补齐之前，这 4 个源只能用于本地研究，**不能对外分发**。
- `jailbreak_classification` 没有 `cases.jsonl`（只有 manifest），锁清单如实标
  `cases=False`。
- `label_sheet/` 是人工标注页，不是语料，已从语料源列表里排除。

## 三、没锁住的部分（不假装锁了）

`data/external/*/manifest.json` 里的 `git_sha` 写的是 **`cache`** 占位符，
不是上游 commit。也就是说：**上游仓库将来改了内容，我们无法证明当时用的是
哪一版**。`pin_status` 因此全部是 `unpinned`，`--strict` 会失败。

要真正锁定，需要回填每个源的不可变 commit：

```jsonc
// data/external/<name>/manifest.json
{
  "repo": "ethz-spylab/agentdojo",
  "commit": "9f3c...（40 位 SHA，不是分支名）",   // 新增
  "license": "MIT",
  "license_url": "https://...",
  "files": [ { "path": "...", "sha256": "...", "bytes": 1234 } ]
}
```

回填后重新 `--write-lock`，`pin_status` 变 `pinned`，`--strict` 才会通过。
**本轮没有回填**：需要逐个访问上游确认 commit，属于未完成项。

## 四、CI 里的位置

`review_checks.py` 目前把语料校验作为**可选**项（干净 clone 没有语料），
不放进 `--gate` 的硬门槛。要把它变成硬门槛，前提是：
1. 4 个源补齐 license；
2. 全部源回填不可变 commit；
3. 语料在 CI 里通过缓存或制品注入。

这三条都没做，所以**不把「有校验脚本」写成「供应链已锁定」**。

## 五、与 EG-017 的关系

v3 预注册要求 `test_unseen_schema` 用独立分布。当前语料无法证明来源版本，
所以 v3 明确写了「仍是确定性合成，不是外部真实分布」—— 这两处口径是一致的：
**没有锁定的上游，就不能声称跨域泛化。**
