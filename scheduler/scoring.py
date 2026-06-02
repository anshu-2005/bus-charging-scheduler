from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

from .models import BusTimeline, Scenario, ScoreBreakdown, StationTimeline
from .rules import build_soft_rules


def compute_score(
    scenario: Scenario,
    bus_timelines: Mapping[str, BusTimeline],
    station_timelines: Mapping[str, StationTimeline],
) -> ScoreBreakdown:
    """
    Weighted soft-objective score.

    Notes:
    - The scenario provides the weights.
    - Each soft rule returns a raw numeric penalty (lower is better).
    """
    w = scenario.weights
    soft_rules = build_soft_rules(scenario)

    components: Dict[str, float] = {"individual": 0.0, "operator": 0.0, "overall": 0.0}
    per_rule: Dict[str, float] = {}

    for rule, weight_key in soft_rules:
        raw = float(rule.score(scenario, bus_timelines, station_timelines))
        per_rule[rule.rule_id] = raw
        if weight_key is None:
            # Default bucket: overall
            components["overall"] += raw
        else:
            if weight_key not in components:
                components["overall"] += raw
            else:
                components[weight_key] += raw

    total = (
        float(w.individual) * components["individual"]
        + float(w.operator) * components["operator"]
        + float(w.overall) * components["overall"]
    )

    return ScoreBreakdown(
        individual=components["individual"],
        operator=components["operator"],
        overall=components["overall"],
        total=total,
        meta={"per_rule": per_rule, "weights": {"individual": w.individual, "operator": w.operator, "overall": w.overall}},
    )

