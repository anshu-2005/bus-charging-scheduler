from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


class Direction(str, Enum):
    BENGALURU_TO_KOCHI = "BENGALURU_TO_KOCHI"
    KOCHI_TO_BENGALURU = "KOCHI_TO_BENGALURU"


@dataclass(frozen=True)
class Constants:
    battery_range_km: float
    charge_duration_min: int
    cruise_speed_kmph: float


@dataclass(frozen=True)
class Operator:
    operator_id: str
    name: str


@dataclass(frozen=True)
class Chargers:
    count: int = 1


@dataclass(frozen=True)
class Station:
    station_id: str
    name: str
    chargers: Chargers
    outages: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class Route:
    route_id: str
    name: str
    stops: Tuple[str, ...]
    segments_km: Tuple[float, ...]
    scheduled_charge_stations: Tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.stops) < 2:
            raise ValueError("Route must contain at least 2 stops.")
        if len(self.segments_km) != len(self.stops) - 1:
            raise ValueError("segments_km must have len(stops) - 1 entries.")

    def cumulative_distances_km(self) -> Dict[str, float]:
        d = 0.0
        out: Dict[str, float] = {self.stops[0]: 0.0}
        for idx, seg in enumerate(self.segments_km):
            d += float(seg)
            out[self.stops[idx + 1]] = d
        return out

    def stop_index(self, stop_id: str) -> int:
        return self.stops.index(stop_id)

    def distance_km_between_stops(self, from_stop: str, to_stop: str) -> float:
        idx_from = self.stop_index(from_stop)
        idx_to = self.stop_index(to_stop)
        if idx_to <= idx_from:
            raise ValueError("Route distance query must be forward along route.")
        return float(sum(self.segments_km[idx_from:idx_to]))

    def travel_time_min(self, distance_km: float) -> int:
        # speed in km/h => minutes = (km / kmph) * 60
        return int(round((distance_km / float(self._speed_kmph_placeholder())) * 60))

    def _speed_kmph_placeholder(self) -> float:
        # Actual speed lives in Scenario.constants; simulator will compute travel times.
        return 60.0


@dataclass(frozen=True)
class Bus:
    bus_id: str
    operator_id: str
    direction: Direction
    route_id: str
    departure_time: str  # HH:MM in scenario local time
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def priority(self) -> int:
        try:
            return int(self.attributes.get("priority", 0))
        except (TypeError, ValueError):
            return 0


@dataclass(frozen=True)
class Weights:
    individual: float
    operator: float
    overall: float


@dataclass(frozen=True)
class RuleConfig:
    rule_id: str
    enabled: bool
    weight_key: Optional[str] = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioRules:
    hard: Tuple[RuleConfig, ...]
    soft: Tuple[RuleConfig, ...]
    dispatch: Tuple[RuleConfig, ...]


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    label: str
    service_date: date
    constants: Constants
    operators: Tuple[Operator, ...]
    stations: Tuple[Station, ...]
    routes: Tuple[Route, ...]
    buses: Tuple[Bus, ...]
    weights: Weights
    rules: ScenarioRules

    def operator_by_id(self) -> Dict[str, Operator]:
        return {o.operator_id: o for o in self.operators}

    def station_by_id(self) -> Dict[str, Station]:
        return {s.station_id: s for s in self.stations}

    def route_by_id(self) -> Dict[str, Route]:
        return {r.route_id: r for r in self.routes}


class EventType(str, Enum):
    DEPART_ORIGIN = "DEPART_ORIGIN"
    ARRIVE_STOP = "ARRIVE_STOP"
    QUEUE_START = "QUEUE_START"
    CHARGE_START = "CHARGE_START"
    CHARGE_END = "CHARGE_END"
    ARRIVE_DESTINATION = "ARRIVE_DESTINATION"


@dataclass(frozen=True)
class TimelineEvent:
    t_min: int
    bus_id: str
    event_type: EventType
    location_id: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ChargingSession:
    station_id: str
    charger_index: int
    bus_id: str
    arrival_t_min: int
    start_t_min: int
    end_t_min: int

    @property
    def wait_before_start_min(self) -> int:
        return max(0, self.start_t_min - self.arrival_t_min)


@dataclass
class BusTimeline:
    bus_id: str
    operator_id: str
    direction: Direction
    route_id: str
    events: List[TimelineEvent] = field(default_factory=list)
    charge_stops: List[str] = field(default_factory=list)
    total_wait_min: int = 0
    total_charge_min: int = 0
    arrival_destination_t_min: Optional[int] = None
    ideal_arrival_t_min: Optional[int] = None


@dataclass
class StationTimeline:
    station_id: str
    chargers_count: int
    sessions: List[ChargingSession] = field(default_factory=list)


@dataclass
class ScoreBreakdown:
    individual: float
    operator: float
    overall: float
    total: float
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScheduleResult:
    scenario_id: str
    bus_timelines: Dict[str, BusTimeline]
    station_timelines: Dict[str, StationTimeline]
    score: ScoreBreakdown
    violations: List[str] = field(default_factory=list)
    decision_trace: List[str] = field(default_factory=list)


def parse_hhmm_to_time(hhmm: str) -> time:
    parts = hhmm.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {hhmm!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    return time(hour=hour, minute=minute)


def datetime_from_service_date(service_date: date, hhmm: str) -> datetime:
    t = parse_hhmm_to_time(hhmm)
    return datetime.combine(service_date, t)


def minutes_since(start: datetime, current: datetime) -> int:
    delta = current - start
    return int(round(delta.total_seconds() / 60.0))


def add_minutes(dt: datetime, minutes: int) -> datetime:
    return dt + timedelta(minutes=int(minutes))

