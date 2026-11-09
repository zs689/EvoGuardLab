# 审批状态机与真实副作用闭环：5 个验收情形

> 由 `scripts/real_settlement_demo.py` 生成；每个情形用独立临时库。
> 通过 5 / 5

## 结果

| 情形 | 结果 | 说明 |
|---|---|---|
| 首次写入没有审批 | 通过 | 没有审批 → 写操作暂停，零落库副作用，状态可定位到 request + 检查点 |
| 审批前后参数发生变化 | 通过 | 参数变化 → 摘要不匹配，原审批不能复用，且未被偷偷消费 |
| 提交成功但响应丢失 | 通过 | 提交成功但响应丢失 → 同一幂等键重试，仍只有一笔结算 |
| 拒绝与过期 | 通过 | 拒绝与过期都留下原因，任务不会自动继续，也没有写操作被执行 |
| 暂停后进程重启 | 通过 | 关掉 Store 再重开，检查点与批准状态都在，且不重复写入 |

## 逐情形证据

### 首次写入没有审批

```json
{
  "request_id": "appr_39a8887dfe21",
  "checkpoint_id": "ckpt_18211db5519b",
  "status": "pending",
  "ledger_rows": 0,
  "tool_executed": false
}
```

### 审批前后参数发生变化

```json
{
  "deny_reason": "参数摘要不匹配，原审批不能复用",
  "original_digest": "efa6125c94eaf25f833d5413578bf6a757b55d5ffd4e9516bbabd87b2b662adc",
  "changed_digest": "5f8802fb8acb4ceab5d14a32753837ac89e3015cbc69e0d6851c5642a86348ae",
  "request_status": "approved",
  "consumed_at": null
}
```

### 提交成功但响应丢失

```json
{
  "http_source": "http://127.0.0.1:60031/order/A1001",
  "rules_path": "tests\\fixtures\\settlement_rules.json",
  "rule_version": "rules-v1",
  "compute": {
    "subtotal": 600.0,
    "threshold": 500.0,
    "rate": 0.1,
    "total": 540.0,
    "rule_version": "rules-v1"
  },
  "idempotency_key": "91a597b9641048e41349c89a0854a49b7e9e44096d26a08bcc24cc85a43fbc2b",
  "committed_before_loss": true,
  "retry_created_new_row": false,
  "ledger_rows_for_order": 1,
  "ledger_row": {
    "idempotency_key": "91a597b9641048e41349c89a0854a49b7e9e44096d26a08bcc24cc85a43fbc2b",
    "order_id": "A1001",
    "total": 540.0,
    "rule_version": "rules-v1"
  }
}
```

### 拒绝与过期

```json
{
  "rejected_status": "rejected",
  "rejected_reason": "人工拒绝：金额需要复核",
  "expired_status": "expired",
  "expired_reason": "审批已过期",
  "tool_executed": false
}
```

### 暂停后进程重启

```json
{
  "checkpoint_state": {
    "cache": {
      "step": 4
    }
  },
  "approved_count": 1,
  "resume_note": "恢复只还原检查点与审批状态，不自动继续写操作",
  "first_settle_created": true,
  "second_settle_created": false,
  "ledger_rows_for_order": 1
}
```

## 边界（不粉饰）

- 订单来自 127.0.0.1 回环测试服务，规则文件在项目沙箱内，结算只写专用 SQLite 表；不碰外部账号、真实支付、任意 shell 执行。
- `sim` 保留作快速回归，本演示是另一条真实副作用链路。
- 默认 `runtime.Ctx` 不挂 `approval_gate`，历史实验行为与冻结数字不变；真实任务显式挂上后才启用可恢复审批。
- 这不是生产级审批工作流：没有身份认证、多租户、审计访问与限流。
