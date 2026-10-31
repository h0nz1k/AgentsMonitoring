"""Prometheus text exposition (format 0.0.4) for ``GET /metrics``.

Built from the same dict as ``/api/state`` so the numbers always match the dashboard. Quota series
come from :func:`quota.series` (provider tables first; agent gauges only for windows no table has).
"""
from __future__ import annotations

import math

from . import quota

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def escape(value) -> str:
    """Label value escaping per the exposition format: backslash, double quote, newline."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**kv) -> str:
    return "{" + ",".join(f'{k}="{escape(v)}"' for k, v in kv.items()) + "}"


def _num(v) -> str:
    v = float(v)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return str(int(v)) if v.is_integer() else repr(v)


def render(data: dict) -> str:
    out: list[str] = []

    def family(name, help_text, samples):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
        out.extend(f"{name}{labels} {_num(value)}" for labels, value in samples)

    items = quota.series(data.get("agent_tables") or [], data.get("agents") or [])
    family("agentsmon_quota_used_percent", "Used share of a provider quota window (percent).",
           [(_labels(source=s["source"], label=s["label"]), s["used"]) for s in items])
    family("agentsmon_quota_reset_timestamp_seconds", "Unix time when the quota window resets.",
           [(_labels(source=s["source"], label=s["label"]), s["reset_at"]) for s in items if s["reset_at"]])
    family("agentsmon_quota_forecast_exhaust_timestamp_seconds",
           "Projected Unix time when the quota window reaches 100 % at the current burn rate.",
           [(_labels(source=f["source"], label=f["label"]), f["exhaust_at"])
            for f in data.get("forecasts") or [] if f.get("exhaust_at")])
    up = {}
    for a in data.get("agents") or []:
        key = _labels(agent=a.get("name") or "", kind=a.get("kind") or "", vendor=a.get("vendor") or "")
        up.setdefault(key, 1 if a.get("alive") else 0)     # a duplicate series would break scrapes
    family("agentsmon_agent_up", "1 when the monitored agent is running.", list(up.items()))
    family("agentsmon_plugin_errors", "Number of plugins currently reporting an error.",
           [("", len(data.get("plugin_errors") or []))])
    return "\n".join(out) + "\n"
