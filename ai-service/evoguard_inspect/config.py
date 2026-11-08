# -*- coding: utf-8 -*-
"""检查器配置：让规则适配不同 Agent，而不是只适配我自己那三个。

为什么必须可配置：规则里一旦写死「settle 是写操作」「金额字段叫 total」，
它就只对 EvoGuard 自己的 sim 有效，换个 Agent 全是误报。
配置把「业务名词」抽出来，规则只处理「模式」。

配置文件格式（JSON，放哪都行，用 --config 指定）：
{
  "write_tools": ["order.settle", "refund.create"],   // 显式写操作
  "write_hints": ["settle", "delete", "refund"],      // 关键词兜底
  "read_only_tools": ["kb.search"],                   // 明确只读，避免误判
  "allowed_tools": [],                                 // 非空则校验「有没有调用不存在的工具」
  "expect_keys": ["total"],                            // 答案必须包含的字段
  "max_repeat": 3,                                     // 同参数重复多少次算滥用
  "token_budget": 20000,                                // 单条轨迹 token 上限
  "slow_step_ms": 30000,                                // 单步耗时告警阈值
  "check_pii": true                                     // 是否检查输出里的个人信息
}
"""
import json
import os

DEFAULTS = {
    "write_tools": [],
    "write_hints": ["settle", "write", "delete", "update", "create", "refund",
                    "submit", "send", "pay", "transfer", "delete", "drop"],
    "read_only_tools": [],
    "allowed_tools": [],
    "expect_keys": [],
    "max_repeat": 3,
    "token_budget": 20000,
    "slow_step_ms": 30000,
    "check_pii": True,
}


class Config(object):
    __slots__ = tuple(DEFAULTS.keys())

    def __init__(self, **kw):
        for k, v in DEFAULTS.items():
            setattr(self, k, kw.get(k, v))

    def is_write(self, tool):
        t = (tool or "").lower()
        if tool in self.write_tools:
            return True
        if tool in self.read_only_tools:
            return False
        return any(h in t for h in self.write_hints)

    def to_dict(self):
        return {k: getattr(self, k) for k in DEFAULTS}


DEFAULT = Config()


def load(path=None):
    if not path:
        return DEFAULT
    if not os.path.exists(path):
        raise ValueError("配置文件不存在：%s" % path)
    with open(path, encoding="utf-8") as f:
        kw = json.load(f)
    unknown = set(kw) - set(DEFAULTS)
    if unknown:
        # 拼错的配置项必须报出来，静默忽略会让人以为规则生效了
        raise ValueError("配置里有不认识的键：%s" % ", ".join(sorted(unknown)))
    return Config(**kw)
