from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from .models import Bus, BusTimeline, ChargingSession, Direction, EventType, Scenario, StationTimeline


@dataclass(frozen=True)
class RuleContext:
    scenario: Scenario


class HardRule(Protocol):
    rule_id: str

    def validate(self, scenario: Scenario) -> List[str]:
        ...


class SoftRule(Protocol):
    rule_id: str

    def score(
        self,
        scenario: Scenario,
        bus_timelines: Mapping[str, BusTimeline],
        station_timelines: Mapping[str, StationTimeline],
    ) -> float:
        ...


class DispatchRule(Protocol):
    rule_id: str

    def priority_key(
        self,
        scenario: Scenario,
        station_id: str,
        bus: Bus,
        bus_timeline: BusTimeline,
        bus_timelines: Mapping[str, BusTimeline],
        arrival_t_min: int,
        now_t_min: int,
        queue_waiting: int,
    ) -> Tuple[float, float, str]:
        """
        Return a key used for heap ordering (smaller sorts first).
        Must be deterministic.
        """
        ...


HARD_RULES: Dict[str, Callable[[Mapping[str, Any]], HardRule]] = {}
SOFT_RULES: Dict[str, Callable[[Mapping[str, Any]], SoftRule]] = {}
DISPATCH_RULES: Dict[str, Callable[[Mapping[str, Any]], DispatchRule]] = {}


def register_hard_rule(rule_id: str) -> Callable[[Callable[[Mapping[str, Any]], HardRule]], Callable[[Mapping[str, Any]], HardRule]]:
    def decorator(factory: Callable[[Mapping[str, Any]], HardRule]) -> Callable[[Mapping[str, Any]], HardRule]:
        HARD_RULES[rule_id] = factory
        return factory

    return decorator


def register_soft_rule(rule_id: str) -> Callable[[Callable[[Mapping[str, Any]], SoftRule]], Callable[[Mapping[str, Any]], SoftRule]]:
    def decorator(factory: Callable[[Mapping[str, Any]], SoftRule]) -> Callable[[Mapping[str, Any]], SoftRule]:
        SOFT_RULES[rule_id] = factory
        return factory

    return decorator


def register_dispatch_rule(
    rule_id: str,
) -> Callable[[Callable[[Mapping[str, Any]], DispatchRule]], Callable[[Mapping[str, Any]], DispatchRule]]:
    def decorator(factory: Callable[[Mapping[str, Any]], DispatchRule]) -> Callable[[Mapping[str, Any]], DispatchRule]:
        DISPATCH_RULES[rule_id] = factory
        return factory

    return decorator


def build_hard_rules(scenario: Scenario) -> List[HardRule]:
    out: List[HardRule] = []
    for rc in scenario.rules.hard:
        if not rc.enabled:
            continue
        if rc.rule_id not in HARD_RULES:
            raise KeyError(f"Unknown hard rule id: {rc.rule_id}")
        out.append(HARD_RULES[rc.rule_id](rc.params))
    return out


def build_soft_rules(scenario: Scenario) -> List[Tuple[SoftRule, Optional[str]]]:
    out: List[Tuple[SoftRule, Optional[str]]] = []
    for rc in scenario.rules.soft:
        if not rc.enabled:
            continue
        if rc.rule_id not in SOFT_RULES:
            raise KeyError(f"Unknown soft rule id: {rc.rule_id}")
        out.append((SOFT_RULES[rc.rule_id](rc.params), rc.weight_key))
    return out


def build_dispatch_rules(scenario: Scenario) -> List[DispatchRule]:
    out: List[DispatchRule] = []
    for rc in scenario.rules.dispatch:
        if not rc.enabled:
            continue
        if rc.rule_id not in DISPATCH_RULES:
            raise KeyError(f"Unknown dispatch rule id: {rc.rule_id}")
        out.append(DISPATCH_RULES[rc.rule_id](rc.params))
    return out


# -----------------------
# Built-in hard rules
# -----------------------


@register_hard_rule("fixed_charge_duration")
def _fixed_charge_duration(_: Mapping[str, Any]) -> HardRule:
    class FixedChargeDuration:
        rule_id = "fixed_charge_duration"

        def validate(self, scenario: Scenario) -> List[str]:
            if scenario.constants.charge_duration_min <= 0:
                return ["charge_duration_min must be > 0"]
            return []

    return FixedChargeDuration()


@register_hard_rule("charger_capacity")
def _charger_capacity(_: Mapping[str, Any]) -> HardRule:
    class ChargerCapacity:
        rule_id = "charger_capacity"

        def validate(self, scenario: Scenario) -> List[str]:
            errs: List[str] = []
            for st in scenario.stations:
                if st.chargers.count < 0:
                    errs.append(f"Station {st.station_id}: chargers.count must be >= 0")
            return errs

    return ChargerCapacity()


@register_hard_rule("no_backtracking")
def _no_backtracking(_: Mapping[str, Any]) -> HardRule:
    class NoBacktracking:
        rule_id = "no_backtracking"

        def validate(self, scenario: Scenario) -> List[str]:
            # Structural constraint: routes must define a linear order; planner/simulator enforce.
            # This rule remains as a schema/consistency check.
            errs: List[str] = []
            for r in scenario.routes:
                if len(set(r.stops)) != len(r.stops):
                    errs.append(f"Route {r.route_id} contains duplicate stops which can cause backtracking.")
            return errs

    return NoBacktracking()


@register_hard_rule("range_feasible")
def _range_feasible(_: Mapping[str, Any]) -> HardRule:
    class RangeFeasible:
        rule_id = "range_feasible"

        def validate(self, scenario: Scenario) -> List[str]:
            if scenario.constants.battery_range_km <= 0:
                return ["battery_range_km must be > 0"]
            return []

    return RangeFeasible()


# -----------------------
# Built-in soft rules
# -----------------------


@register_soft_rule("individual_total_wait")
def _individual_total_wait(_: Mapping[str, Any]) -> SoftRule:
    class IndividualTotalWait:
        rule_id = "individual_total_wait"

        def score(self, scenario: Scenario, bus_timelines: Mapping[str, BusTimeline], station_timelines: Mapping[str, StationTimeline]) -> float:
            return float(sum(bt.total_wait_min for bt in bus_timelines.values()))

    return IndividualTotalWait()


@register_soft_rule("operator_delay_variance")
def _operator_delay_variance(_: Mapping[str, Any]) -> SoftRule:
    class OperatorDelayVariance:
        rule_id = "operator_delay_variance"

        def score(self, scenario: Scenario, bus_timelines: Mapping[str, BusTimeline], station_timelines: Mapping[str, StationTimeline]) -> float:
            # Variance of (actual - ideal) arrival, aggregated by operator.
            by_op: Dict[str, List[float]] = {}
            for bt in bus_timelines.values():
                if bt.arrival_destination_t_min is None or bt.ideal_arrival_t_min is None:
                    continue
                delay = float(bt.arrival_destination_t_min - bt.ideal_arrival_t_min)
                by_op.setdefault(bt.operator_id, []).append(delay)
            total_var = 0.0
            for op, vals in by_op.items():
                if len(vals) <= 1:
                    continue
                mean = sum(vals) / float(len(vals))
                var = sum((v - mean) ** 2 for v in vals) / float(len(vals))
                total_var += var
            return total_var

    return OperatorDelayVariance()


@register_soft_rule("overall_total_time")
def _overall_total_time(_: Mapping[str, Any]) -> SoftRule:
    class OverallTotalTime:
        rule_id = "overall_total_time"

        def score(self, scenario: Scenario, bus_timelines: Mapping[str, BusTimeline], station_timelines: Mapping[str, StationTimeline]) -> float:
            total = 0.0
            for bt in bus_timelines.values():
                if bt.arrival_destination_t_min is None:
                    continue
                # Total time since departure (t=0 anchored per bus departure).
                # We store t_min since scenario start, so subtract first depart event.
                depart_events = [e for e in bt.events if e.event_type == EventType.DEPART_ORIGIN]
                if depart_events:
                    depart_t = depart_events[0].t_min
                else:
                    depart_t = 0
                total += float(bt.arrival_destination_t_min - depart_t)
            return total

    return OverallTotalTime()


# -----------------------
# Built-in dispatch rules
# -----------------------


@register_dispatch_rule("default_priority")
def _default_priority(_: Mapping[str, Any]) -> DispatchRule:
    class DefaultPriority:
        rule_id = "default_priority"

        def priority_key(
            self,
            scenario: Scenario,
            station_id: str,
            bus: Bus,
            bus_timeline: BusTimeline,
            bus_timelines: Mapping[str, BusTimeline],
            arrival_t_min: int,
            now_t_min: int,
            queue_waiting: int,
        ) -> Tuple[float, float, str]:
            # Smaller key -> higher priority.
            # Use negative priority so that higher priority number means earlier service.
            #
            # IMPORTANT: weights must affect the actual schedule, not just post-hoc scoring.
            # We fold scenario weights into dispatching:
            # - individual weight: serve buses that have accumulated more waiting so far
            # - operator weight: avoid starving an operator (serve buses from operators with more accumulated wait)
            # - overall weight: keep the system moving (earlier arrivals are favored)
            w = scenario.weights

            # Dynamic, weight-aware dispatching.
            # - Individual: prefer buses that have waited longer (so far + currently waiting in this queue)
            # - Operator: avoid starving an operator by tracking cumulative wait across its fleet so far
            current_queue_wait = max(0, int(now_t_min) - int(arrival_t_min))

            operator_total_wait = 0.0
            for bt in bus_timelines.values():
                if bt.operator_id == bus.operator_id:
                    operator_total_wait += float(bt.total_wait_min)

            individual_term = float(bus_timeline.total_wait_min + current_queue_wait)
            operator_term = float(operator_total_wait + current_queue_wait)
            fairness_term = float(w.individual) * individual_term + float(w.operator) * operator_term

            # Higher fairness_term => should go earlier => negate it for min-heap ordering.
            # Overall efficiency: prefer earlier arrivals and shorter current waits (stability).
            return (-float(bus.priority()), -fairness_term, float(arrival_t_min), bus.bus_id)

    return DefaultPriority()

