from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from jsonschema import Draft202012Validator

from .models import (
    Bus,
    Chargers,
    Constants,
    Direction,
    Operator,
    RuleConfig,
    Route,
    Scenario,
    ScenarioRules,
    Station,
    Weights,
)
from .planner import ChargingPlan, choose_initial_plan, generate_feasible_plans
from .rules import build_dispatch_rules, build_hard_rules
from .scoring import compute_score
from .simulator import simulate


SCENARIO_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["scenario_id", "constants", "operators", "stations", "routes", "buses", "weights", "rules"],
    "properties": {
        "scenario_id": {"type": "string", "minLength": 1},
        "label": {"type": "string"},
        "service_date": {"type": "string"},
        "constants": {
            "type": "object",
            "required": ["battery_range_km", "charge_duration_min", "cruise_speed_kmph"],
            "properties": {
                "battery_range_km": {"type": "number", "exclusiveMinimum": 0},
                "charge_duration_min": {"type": "integer", "minimum": 1},
                "cruise_speed_kmph": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        "operators": {"type": "array", "minItems": 1},
        "stations": {"type": "array", "minItems": 1},
        "routes": {"type": "array", "minItems": 1},
        "buses": {"type": "array", "minItems": 1},
        "weights": {
            "type": "object",
            "required": ["individual", "operator", "overall"],
            "properties": {
                "individual": {"type": "number", "minimum": 0},
                "operator": {"type": "number", "minimum": 0},
                "overall": {"type": "number", "minimum": 0},
            },
        },
        "rules": {"type": "object"},
    },
}


def _parse_date(value: Optional[str]) -> date:
    if not value:
        # Default to today's date to keep UI stable if omitted.
        return date.today()
    parts = value.split("-")
    if len(parts) != 3:
        raise ValueError(f"Invalid service_date: {value!r}")
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


def load_scenario_json(path: str | Path) -> Tuple[Scenario, Dict[str, Any]]:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))

    validator = Draft202012Validator(SCENARIO_SCHEMA)
    errors = sorted(validator.iter_errors(raw), key=lambda e: e.path)
    if errors:
        msg = "\n".join([f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors])
        raise ValueError(f"Scenario JSON failed validation:\n{msg}")

    constants = Constants(
        battery_range_km=float(raw["constants"]["battery_range_km"]),
        charge_duration_min=int(raw["constants"]["charge_duration_min"]),
        cruise_speed_kmph=float(raw["constants"]["cruise_speed_kmph"]),
    )

    operators = tuple(Operator(operator_id=o["operator_id"], name=o.get("name", o["operator_id"])) for o in raw["operators"])

    stations = []
    for s in raw["stations"]:
        chargers = Chargers(count=int(s["chargers"]["count"]))
        stations.append(
            Station(
                station_id=s["station_id"],
                name=s.get("name", s["station_id"]),
                chargers=chargers,
                outages=tuple(s.get("outages", [])),
            )
        )
    stations_t = tuple(stations)

    routes = []
    for r in raw["routes"]:
        routes.append(
            Route(
                route_id=r["route_id"],
                name=r.get("name", r["route_id"]),
                stops=tuple(r["stops"]),
                segments_km=tuple(float(x) for x in r["segments_km"]),
                scheduled_charge_stations=tuple(r.get("scheduled_charge_stations", [])),
            )
        )
    routes_t = tuple(routes)

    buses = []
    for b in raw["buses"]:
        buses.append(
            Bus(
                bus_id=b["bus_id"],
                operator_id=b["operator_id"],
                direction=Direction(b["direction"]),
                route_id=b["route_id"],
                departure_time=b["departure_time"],
                attributes=b.get("attributes", {}),
            )
        )
    buses_t = tuple(buses)

    weights = Weights(
        individual=float(raw["weights"]["individual"]),
        operator=float(raw["weights"]["operator"]),
        overall=float(raw["weights"]["overall"]),
    )

    def _rc(x: Mapping[str, Any]) -> RuleConfig:
        return RuleConfig(
            rule_id=str(x["id"]),
            enabled=bool(x.get("enabled", True)),
            weight_key=x.get("weight_key"),
            params=x.get("params", {}),
        )

    rules_raw = raw["rules"]
    rules = ScenarioRules(
        hard=tuple(_rc(x) for x in rules_raw.get("hard", [])),
        soft=tuple(_rc(x) for x in rules_raw.get("soft", [])),
        dispatch=tuple(_rc(x) for x in rules_raw.get("dispatch", [])),
    )

    scenario = Scenario(
        scenario_id=raw["scenario_id"],
        label=raw.get("label", raw["scenario_id"]),
        service_date=_parse_date(raw.get("service_date")),
        constants=constants,
        operators=operators,
        stations=stations_t,
        routes=routes_t,
        buses=buses_t,
        weights=weights,
        rules=rules,
    )

    return scenario, raw


@dataclass
class EngineConfig:
    max_plan_candidates_per_bus: int = 32
    local_search_iterations: int = 40


class SchedulerEngine:
    def __init__(self, config: Optional[EngineConfig] = None):
        self.config = config or EngineConfig()

    def build_plans(self, scenario: Scenario) -> Tuple[Dict[str, ChargingPlan], Dict[str, List[ChargingPlan]]]:
        candidates: Dict[str, List[ChargingPlan]] = {}
        chosen: Dict[str, ChargingPlan] = {}
        for bus in scenario.buses:
            cands = generate_feasible_plans(
                scenario,
                bus,
                max_plans=self.config.max_plan_candidates_per_bus,
            )
            candidates[bus.bus_id] = cands
            chosen[bus.bus_id] = choose_initial_plan(scenario, bus, cands)
        return chosen, candidates

    def schedule(self, scenario: Scenario) -> "ScheduleResult":
        from .models import ScheduleResult  # avoid circular import

        hard_rules = build_hard_rules(scenario)
        violations: List[str] = []
        for hr in hard_rules:
            violations.extend(hr.validate(scenario))
        if violations:
            return ScheduleResult(
                scenario_id=scenario.scenario_id,
                bus_timelines={},
                station_timelines={},
                score=compute_score(scenario, {}, {}),
                violations=violations,
                decision_trace=[],
            )

        dispatch_rules = build_dispatch_rules(scenario)
        plans, candidates = self.build_plans(scenario)

        decision_trace: List[str] = []
        bus_tl, st_tl, sim_violations = simulate(scenario, plans, dispatch_rules, decision_trace=decision_trace)
        violations.extend(sim_violations)
        best_score = compute_score(scenario, bus_tl, st_tl)
        # If the initial schedule has any hard violations, treat it as invalid so that any
        # violation-free alternative will replace it during local search.
        initial_total = best_score.total if not violations else float("inf")
        best = (initial_total, plans, bus_tl, st_tl, best_score, list(decision_trace), list(violations))

        # Lightweight hill-climb: try alternate plan for one bus at a time.
        # Keeps it production-safe (bounded) and provides visibly different schedules across scenarios.
        rng_iter = 0
        for _ in range(int(self.config.local_search_iterations)):
            rng_iter += 1
            bus = scenario.buses[rng_iter % len(scenario.buses)]
            bus_id = bus.bus_id
            alts = candidates.get(bus_id, [])
            if len(alts) <= 1:
                continue
            # Try the next alternative plan (cyclic).
            alt = alts[(rng_iter // len(scenario.buses)) % len(alts)]
            if alt.charge_stops == plans[bus_id].charge_stops:
                continue
            new_plans = dict(plans)
            new_plans[bus_id] = alt
            dt: List[str] = []
            bt2, st2, viol2 = simulate(scenario, new_plans, dispatch_rules, decision_trace=dt)
            score2 = compute_score(scenario, bt2, st2)
            if viol2:
                continue
            if score2.total < best[0]:
                plans = new_plans
                best = (score2.total, new_plans, bt2, st2, score2, dt, list(viol2))

        _, best_plans, best_bt, best_st, best_score, best_trace, best_viol = best

        return ScheduleResult(
            scenario_id=scenario.scenario_id,
            bus_timelines=best_bt,
            station_timelines=best_st,
            score=best_score,
            violations=best_viol,
            decision_trace=best_trace,
        )

