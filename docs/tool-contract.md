# 结构化工具契约授权（§5「先改权限结构 再补特征」）

代码：`ai-service/evoguard/tool_contract.py`；演示：`scripts/tool_contract_demo.py`
（产物 `experiments/tool_contract_v1/`）；验收：`tests/test_tool_contract.py`（12 项）。

## 立场

**授权由契约决定，正则只作附加信号，不承担全部授权决策。**

`security.scan()` 的命中只写进 `regex_signals`，不影响 `decision`。
域名、资源 ID、可写路径、最大金额、方法、副作用类型都是结构化字段，
由冻结契约校验；未知身份在 `enforce` 下拒绝；输入文本、模型或经验都不能自行扩权。

## 契约字段

| 字段 | 含义 |
|---|---|
| `identities` | 允许调用该工具的身份 |
| `allowed_domains` | 允许外联的域名（空 = 不许外联） |
| `allowed_resource_pattern` | 资源 ID 格式（订单号 / 文档号） |
| `writable_paths` | 允许写入的路径 |
| `max_amount` | 金额上限 |
| `methods` | 允许的方法（read / write …） |
| `side_effect` | 副作用类型（none / read / write / external / arbitrary） |
| `requires_approval` | 是否必须带 `approval_request_id` |

每次 `authorize()` 逐项产出 `checks`（`field / rule / value / ok / detail`），
失败项给出 `reasons`。`enforce` 下失败 → `deny`；`audit` 下 → `report`（不拦但留原因）；
`off` 下**一项都不查**，直接 `allow` 并置 `detection=False`，`note` 写明
「allow 只表示没拦，不代表调用合规」—— 对照档必须是真关掉，否则四档对比不成立。
未登记的模式名按 `audit` 处理，不能静默变成放行。

金额检查要求**有限非负数**：`NaN`、`±inf`、负数一律不合规。
原因很直白——`float('-inf') <= 10000` 为真，只比上限会被负无穷绕过去。

## 两个必须成立的反例

1. **正则命中但契约允许**：`order.get_order` 参数里写 `rm -rf /`，
   正则会命中 `dangerous_shell_rm`，但身份、资源 ID、方法、金额全合规 → `allow`。
   命中只记进 `regex_signals`，`regex_only=True`。
2. **正则零命中但越界**：`http.send` 带一个普通回环 URL，正则什么都没抓到，
   但契约没登记身份、没允许域名 → `deny`（`structural_only=True`）。
   「没认出攻击」不等于「有权执行」。

同一组合约下加不加危险字样，`decision` 必须一样（测试守着）。

## 与现有安全层的关系

- 独立模块，**不改** `security.guard_tool_args` 的默认行为，历史回归与冻结数字可比。
- `security` 的特征检测仍是**额外信号**，不承担授权；真正的兜底是权限最小化、
  写操作审批与执行隔离（后两者见 `approval.py` 与推进计划 S04/S05）。
- 冻结策略白名单来自手写声明，不从已执行轨迹反推（Q02）。

## 边界

- 这是本地演示契约，不是生产级策略引擎：没有多租户、动态策略下发、身份签名。
- 未接进 `runtime.Ctx.call` 的默认路径；要全面启用需重跑实验并重算历史数字。
- 正则检测可被改写绕过；本模块保证的是「绕得过特征也绕不过结构化授权」，
  不是「能识别任意攻击」。

## 复核命令

```bash
cd <repo-root>
python scripts/tool_contract_demo.py
python -m unittest tests.test_tool_contract -q
```
