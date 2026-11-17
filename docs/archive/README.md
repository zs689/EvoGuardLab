# 归档：外部审查原始证据

从工作区根目录清理掉的大体积临时目录，**关键证据已抽出来存在这里**。

| 来源 | 保留了什么 | 原体积 |
|---|---|---|
| `_enterprise_review_20261112/` | `independent_measurements.json`（独立测量）、`tests_*.log`（两次测试日志）、`demo.json/md`、`wheel_smoke.json` | 138 MB（其余是源码拷贝与轮子构建缓存） |
| `_nemo_doc.txt` | `nemo-review-2026-11-11.txt`（NeMo 对标评审结论原文） | 24 KB |
| `_review_work_20261111/` | 未保留 —— 只是生成上面那份 txt 的 NeMo 源码分块 | 30 MB |
| `_plan_work*` | 未保留 —— 生成 docx 的渲染中间件，成品 docx 仍在根目录 | 3.2 MB |

删除时间：2026-11-17。删除原因：都是**可重建的中间产物**，
结论已沉淀到 `docs/review-remediation/`。
