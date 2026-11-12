# -*- coding: utf-8 -*-
"""可观测性：结构化日志 sink、Prometheus 指标、span 追踪、流量采样。

真实现：多 sink 日志管道、Prometheus 文本格式渲染、span 时长记录、按比例采样。
预留接口：`LogSink` / `MetricsExporter` / `TraceExporter` —— 企业接
Loki / Prometheus / OTel Collector 时只需实现一个方法，不用改业务代码。

**不采正文**：采样器只允许白名单元数据字段，避免把用户内容带进观测系统。
"""
import json
import os
import threading
import time

# ---------------------------------------------------------------- 日志


class LogSink:
    def emit(self, record):                # pragma: no cover - 接口
        raise NotImplementedError


class NullSink(LogSink):
    def emit(self, record):
        return None


class StderrSink(LogSink):
    def __init__(self, stream=None):
        self.stream = stream

    def emit(self, record):
        import sys
        (self.stream or sys.stderr).write(
            json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


class JsonlFileSink(LogSink):
    """按大小轮转的 JSONL 落盘；`keep` 份历史。线程安全。"""

    def __init__(self, path, max_bytes=8 << 20, keep=3):
        self.path = path
        self.max_bytes = max_bytes
        self.keep = max(1, keep)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    def _rotate(self):
        if not os.path.exists(self.path):
            return
        if os.path.getsize(self.path) < self.max_bytes:
            return
        for i in range(self.keep, 0, -1):
            src = "%s.%d" % (self.path, i - 1) if i > 1 else self.path
            dst = "%s.%d" % (self.path, i)
            if os.path.exists(src):
                os.replace(src, dst)

    def emit(self, record):
        line = json.dumps(record, sort_keys=True, ensure_ascii=False)
        with self._lock:
            self._rotate()
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")


class MemorySink(LogSink):
    def __init__(self, limit=200):
        self.limit = limit
        self.records = []

    def emit(self, record):
        self.records.append(record)
        if len(self.records) > self.limit:
            del self.records[:len(self.records) - self.limit]


class LogPipeline:
    def __init__(self, sinks=None, clock=None):
        self.sinks = list(sinks or [])
        self._clock = clock or (lambda: time.time())
        self._lock = threading.Lock()

    def add(self, sink):
        with self._lock:
            self.sinks.append(sink)
        return sink

    def remove(self, sink):
        with self._lock:
            if sink in self.sinks:
                self.sinks.remove(sink)

    def emit(self, record, tenant_id=None):
        row = dict(record)
        row.setdefault("ts", self._clock())
        if tenant_id is not None:
            row["tenant_id"] = tenant_id
        with self._lock:
            sinks = list(self.sinks)
        for s in sinks:
            try:
                s.emit(row)
            except Exception:              # 观测失败不能拖垮业务
                continue
        return row


# ---------------------------------------------------------------- 指标

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _label_key(labels):
    if not labels:
        return ""
    return ",".join('%s="%s"' % (k, str(v).replace("\\", "\\\\")
                                  .replace('"', '\\"').replace("\n", "\\n"))
                    for k, v in sorted(labels.items()))


def _series(name, labels):
    key = _label_key(labels)
    return name if not key else "%s{%s}" % (name, key)


class Metrics:
    """最小指标注册表：counter / gauge / histogram + Prometheus 文本输出。"""

    def __init__(self):
        self._counters = {}
        self._gauges = {}
        self._hist = {}
        self._help = {}
        self._lock = threading.Lock()

    def describe(self, name, text, kind="counter"):
        with self._lock:
            self._help[name] = (text, kind)

    def inc(self, name, labels=None, value=1):
        k = _series(name, labels)
        with self._lock:
            self._counters[k] = self._counters.get(k, 0) + value
        return self._counters[k]

    def set_gauge(self, name, value, labels=None):
        k = _series(name, labels)
        with self._lock:
            self._gauges[k] = value
        return value

    def observe(self, name, value, labels=None):
        k = _series(name, labels)
        with self._lock:
            h = self._hist.setdefault(k, {"count": 0, "sum": 0.0,
                                          "buckets": {b: 0 for b in _BUCKETS}})
            h["count"] += 1
            h["sum"] += value
            for b in _BUCKETS:
                if value <= b:
                    h["buckets"][b] += 1
        return value

    def snapshot(self):
        with self._lock:
            return {"counter": dict(self._counters),
                    "gauge": dict(self._gauges),
                    "histogram": {k: {"count": v["count"], "sum": v["sum"]}
                                  for k, v in self._hist.items()}}

    def render_prometheus(self):
        """输出 Prometheus 文本格式；同名字多个 label 组合各一行。"""
        out = []
        with self._lock:
            groups = {}
            for k, v in self._counters.items():
                base = k.split("{")[0]
                groups.setdefault(base, []).append((k, "counter", v))
            for k, v in self._gauges.items():
                base = k.split("{")[0]
                groups.setdefault(base, []).append((k, "gauge", v))
            for k, h in self._hist.items():
                base = k.split("{")[0]
                groups.setdefault(base, []).append((k, "histogram", h))
            for base in sorted(groups):
                text, kind = self._help.get(base, ("", groups[base][0][1]))
                if text:
                    out.append("# HELP %s %s" % (base, text))
                out.append("# TYPE %s %s" % (base, kind))
                for series, kind2, val in groups[base]:
                    if kind2 == "histogram":
                        label = series[len(base):]  # "{..}" 或空
                        inner = label[1:-1] if label else ""
                        for b in _BUCKETS:
                            if inner:
                                out.append('%s_bucket{%s,le="%s"} %d'
                                           % (base, inner, b,
                                              val["buckets"][b]))
                            else:
                                out.append('%s_bucket{le="%s"} %d'
                                           % (base, b, val["buckets"][b]))
                        if inner:
                            out.append('%s_bucket{%s,le="+Inf"} %d'
                                       % (base, inner, val["count"]))
                        else:
                            out.append('%s_bucket{le="+Inf"} %d'
                                       % (base, val["count"]))
                        out.append("%s_sum%s %s" % (base, label, val["sum"]))
                        out.append("%s_count%s %d" % (base, label, val["count"]))
                    else:
                        out.append("%s %s" % (series, val))
        return "\n".join(out) + "\n"

    def flush(self, exporter):
        """预留：交给企业侧 exporter（Prometheus pushgateway / OTel）。"""
        exporter.export(self.snapshot())


class MetricsExporter:
    def export(self, snapshot):            # pragma: no cover - 接口
        raise NotImplementedError


# ---------------------------------------------------------------- 追踪


class Span:
    def __init__(self, tracer, name, attrs):
        self.tracer = tracer
        self.name = name
        self.attrs = dict(attrs)
        self.start = None
        self.duration_ms = None

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.duration_ms = round((time.perf_counter() - self.start) * 1000, 3)
        if exc_type is not None:
            self.attrs["error"] = exc_type.__name__
        self.tracer._append({"name": self.name,
                             "duration_ms": self.duration_ms,
                             "attrs": self.attrs})
        return False


class Tracer:
    def __init__(self, max_records=1000):
        self.max_records = max_records
        self.records = []
        self._lock = threading.Lock()

    def span(self, name, **attrs):
        return Span(self, name, attrs)

    def record(self, name, duration_ms, **attrs):
        """直接记一条已完成的操作（HTTP 请求等），不需要 context manager。"""
        rec = {"name": name, "duration_ms": duration_ms, "attrs": dict(attrs)}
        self._append(rec)
        return rec

    def _append(self, rec):
        with self._lock:
            self.records.append(rec)
            if len(self.records) > self.max_records:
                del self.records[:len(self.records) - self.max_records]

    def export(self):
        with self._lock:
            return list(self.records)

    def flush(self, exporter):
        exporter.export(self.export())

    def clear(self):
        with self._lock:
            self.records.clear()


class TraceExporter:
    def export(self, spans):               # pragma: no cover - 接口
        raise NotImplementedError


# ---------------------------------------------------------------- 流量采样

_SAMPLE_ALLOWED = {"path", "method", "status", "duration_ms", "tenant_id",
                   "decision", "tool", "error_type"}


class TrafficSampler:
    """按比例采样**元数据**（白名单字段），用于真实流量评估的接入点。

    本项目没有真实业务流量；企业把 `sink` 接到自己的数据管道即可。
    正文/参数一律不采，避免把用户内容带进离线分析。
    """

    def __init__(self, rate=0.05, sink=None, max_records=5000):
        self.rate = max(0.0, min(1.0, float(rate)))
        self.sink = sink
        self.max_records = max_records
        self.samples = []
        self._n = 0
        self._lock = threading.Lock()

    def maybe_sample(self, record):
        with self._lock:
            self._n += 1
            take = (self.rate >= 1.0) or (
                self.rate > 0 and (self._n % max(1, int(round(1 / self.rate))) == 0))
            if not take:
                return None
            clean = {k: v for k, v in record.items() if k in _SAMPLE_ALLOWED}
            self.samples.append(clean)
            if len(self.samples) > self.max_records:
                del self.samples[:len(self.samples) - self.max_records]
        if self.sink is not None:
            self.sink.emit(clean)
        return clean
