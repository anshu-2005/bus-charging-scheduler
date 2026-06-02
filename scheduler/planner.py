from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence, Tuple

from .models import Bus, Direction, Route, Scenario


@dataclass(frozen=True)
class ChargingPlan:
    bus_id: str
    route_id: str
    direction: Direction
    charge_stops: Tuple[str, ...]  # station_ids in travel order (subset of scheduled stations)


def generate_feasible_plans(
    scenario: Scenario,
    bus: Bus,
    max_plans: int = 32,
) -> List[ChargingPlan]:
    """
    Generate feasible charging plans for a bus purely from scenario data.

    A plan is a sequence of scheduled charge stations such that:
    - origin -> first charge <= battery_range
    - between charges <= battery_range
    - last charge -> destination <= battery_range
    """
    route = scenario.route_by_id()[bus.route_id]
    rng = float(scenario.constants.battery_range_km)

    if bus.direction == Direction.BENGALURU_TO_KOCHI:
        origin = route.stops[0]
        dest = route.stops[-1]
        stations = list(route.scheduled_charge_stations)
    else:
        origin = route.stops[-1]
        dest = route.stops[0]
        stations = list(reversed(route.scheduled_charge_stations))

    # Build an ordered list of candidate stops including endpoints.
    ordered = [origin] + stations + [dest]

    # Distance matrix along the route direction.
    def dist(a: str, b: str) -> float:
        if bus.direction == Direction.BENGALURU_TO_KOCHI:
            return route.distance_km_between_stops(a, b)
        # reverse direction: distance between b->a forward
        return route.distance_km_between_stops(b, a)

    plans: List[ChargingPlan] = []

    def dfs(current: str, idx_start: int, chosen: List[str]) -> None:
        if len(plans) >= max_plans:
            return
        # Try to go directly to destination.
        if dist(current, dest) <= rng:
            plans.append(
                ChargingPlan(
                    bus_id=bus.bus_id,
                    route_id=bus.route_id,
                    direction=bus.direction,
                    charge_stops=tuple(chosen),
                )
            )
            return

        for idx in range(idx_start, len(stations)):
            st = stations[idx]
            if dist(current, st) <= rng:
                chosen.append(st)
                dfs(st, idx + 1, chosen)
                chosen.pop()

    dfs(origin, 0, [])

    # Prefer fewer charges, then earlier/lexicographic stability.
    plans.sort(key=lambda p: (len(p.charge_stops), p.charge_stops))
    return plans


def choose_initial_plan(
    scenario: Scenario,
    bus: Bus,
    candidate_plans: Sequence[ChargingPlan],
) -> ChargingPlan:
    """
    Initial plan selection heuristic (fast, data-driven).
    Prefers:
    - minimal number of charges (hard requirement implies at least 2 for base route)
    - mid-route stations B/C slightly preferred to avoid funneling at endpoints (tunable later)
    """
    if not candidate_plans:
        raise ValueError(f"No feasible plans for bus {bus.bus_id}")

    def score(plan: ChargingPlan) -> Tuple[int, int, Tuple[str, ...]]:
        mid_bonus = 0
        for s in plan.charge_stops:
            if s in ("B", "C"):
                mid_bonus -= 1
        return (len(plan.charge_stops), mid_bonus, plan.charge_stops)

    return sorted(candidate_plans, key=score)[0]

