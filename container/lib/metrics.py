# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# lib/metrics.py — instrument caching for OpenTelemetry counters and histograms
from typing import Any, Optional

_meter = None
try:
    from opentelemetry import metrics
    _meter = metrics.get_meter("opencode")
except ImportError:
    pass

_counters: dict[str, Any] = {}
_histograms: dict[str, Any] = {}

def record_metric(name: str, value: float, attributes: Optional[dict[str, str]] = None) -> None:
    if _meter is None:
        return
    try:
        if name not in _counters:
            _counters[name] = _meter.create_counter(name)
        _counters[name].add(value, attributes or {})
    except Exception:
        pass

def record_histogram(name: str, value: float, unit: str, attributes: Optional[dict[str, str]] = None) -> None:
    if _meter is None:
        return
    try:
        if name not in _histograms:
            _histograms[name] = _meter.create_histogram(name, unit=unit)
        _histograms[name].record(value, attributes or {})
    except Exception:
        pass
