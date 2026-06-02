from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .models import (
    Bus,
    BusTimeline,
    ChargingSession,
    Direction,
    EventType,
    Scenario,
    StationTimeline,
    TimelineEvent,
    datetime_from_service_date,
    minutes_since,
)
from .planner import ChargingPlan
from .rules import DispatchRule


@dataclass
class _BusState:
    bus: Bus
    plan: ChargingPlan
    route_stops: Tuple[str, ...]
    route_segments_km: Tuple[float, ...]
    stop_index: int
    distance_since_charge_km: float


@dataclass
class _StationState:
    station_id: str
    chargers_count: int
    free_heap: List[Tuple[int, int]] = field(default_factory=list)  # (free_t_min, charger_index)
    waiting: List[Tuple[int, str]] = field(default_factory=list)  # (arrival_t_min, bus_id)


def _travel_time_min(distance_km: float, speed_kmph: float) -> int:
    return int(round((float(distance_km) / float(speed_kmph)) * 60.0))


def simulate(
    scenario: Scenario,
    plans: Mapping[str, ChargingPlan],
    dispatch_rules: List[DispatchRule],
    decision_trace: Optional[List[str]] = None,
) -> Tuple[Dict[str, BusTimeline], Dict[str, StationTimeline], List[str]]:
    """
    Discrete-event simulation:
    - BUS_ARRIVE events advance buses along the route
    - CHARGE_END events release a charger and let the bus continue

    Heap-based charger allocation:
    - per-station heap of charger free times
    - per-station heap of waiting buses using dispatch priority keys
    """
    decision_trace = decision_trace if decision_trace is not None else []

    routes = scenario.route_by_id()
    station_defs = scenario.station_by_id()
    buses = {b.bus_id: b for b in scenario.buses}

    # Scenario time anchor: midnight of service_date for stable HH:MM formatting.
    scenario_start_dt = datetime.combine(scenario.service_date, datetime.min.time())

    # Build station state and timelines.
    station_states: Dict[str, _StationState] = {}
    station_timelines: Dict[str, StationTimeline] = {}
    for st in scenario.stations:
        ss = _StationState(station_id=st.station_id, chargers_count=int(st.chargers.count))
        for i in range(int(st.chargers.count)):
            heapq.heappush(ss.free_heap, (0, i))
        station_states[st.station_id] = ss
        station_timelines[st.station_id] = StationTimeline(
            station_id=st.station_id, chargers_count=int(st.chargers.count)
        )

    bus_timelines: Dict[str, BusTimeline] = {}
    violations: List[str] = []

    bus_states: Dict[str, _BusState] = {}
    for bus_id, plan in plans.items():
        bus = buses[bus_id]
        route = routes[bus.route_id]
        if bus.direction == Direction.BENGALURU_TO_KOCHI:
            stops = route.stops
            segments = route.segments_km
            start_index = 0
        else:
            stops = tuple(reversed(route.stops))
            segments = tuple(reversed(route.segments_km))
            start_index = 0

        bus_states[bus_id] = _BusState(
            bus=bus,
            plan=plan,
            route_stops=stops,
            route_segments_km=segments,
            stop_index=start_index,
            distance_since_charge_km=0.0,
        )
        bus_timelines[bus_id] = BusTimeline(
            bus_id=bus.bus_id,
            operator_id=bus.operator_id,
            direction=bus.direction,
            route_id=bus.route_id,
        )

    # Event queue: (t_min, seq, type, payload)
    event_q: List[Tuple[int, int, str, Dict[str, Any]]] = []
    seq = 0

    def push_event(t_min: int, etype: str, payload: Dict[str, Any]) -> None:
        nonlocal seq
        seq += 1
        heapq.heappush(event_q, (int(t_min), seq, etype, payload))

    # Initialize departures.
    for bus in scenario.buses:
        depart_dt = datetime_from_service_date(scenario.service_date, bus.departure_time)
        depart_t = minutes_since(scenario_start_dt, depart_dt)
        bt = bus_timelines[bus.bus_id]
        bt.events.append(
            TimelineEvent(
                t_min=depart_t,
                bus_id=bus.bus_id,
                event_type=EventType.DEPART_ORIGIN,
                location_id=bus_states[bus.bus_id].route_stops[0],
            )
        )
        # Schedule arrival at next stop.
        state = bus_states[bus.bus_id]
        if len(state.route_stops) < 2:
            violations.append(f"Bus {bus.bus_id}: route has insufficient stops")
            continue
        first_leg_km = float(state.route_segments_km[0])
        first_leg_min = _travel_time_min(first_leg_km, scenario.constants.cruise_speed_kmph)
        push_event(depart_t + first_leg_min, "BUS_ARRIVE", {"bus_id": bus.bus_id, "stop_index": 1})

        # Ideal arrival time (no waits, but includes charge durations at planned stops).
        ideal = depart_t
        dist_since = 0.0
        for i in range(1, len(state.route_stops)):
            ideal += _travel_time_min(float(state.route_segments_km[i - 1]), scenario.constants.cruise_speed_kmph)
            dist_since += float(state.route_segments_km[i - 1])
            stop_id = state.route_stops[i]
            if stop_id in state.plan.charge_stops:
                ideal += int(scenario.constants.charge_duration_min)
                dist_since = 0.0
        bt.ideal_arrival_t_min = ideal

    def _dispatch_priority_key(
        station_id: str,
        bus_id: str,
        arrival_t_min: int,
        now_t_min: int,
        queue_waiting: int,
    ) -> Tuple[float, float, str]:
        bus = buses[bus_id]
        bt = bus_timelines[bus_id]
        # Combine multiple dispatch rules by taking the minimum key lexicographically
        # after appending rule_id stability.
        keys: List[Tuple[float, float, str]] = []
        for dr in dispatch_rules:
            keys.append(dr.priority_key(scenario, station_id, bus, bt, bus_timelines, arrival_t_min, now_t_min, queue_waiting))
        if not keys:
            return (0.0, float(arrival_t_min), bus_id)
        return min(keys)

    def try_start_charging(station_id: str, now_t_min: int) -> None:
        ss = station_states[station_id]
        if ss.chargers_count <= 0:
            # No chargers => any bus that planned to charge here can never proceed.
            # Fail fast with a hard violation rather than silently deadlocking.
            violations.append(f"Station {station_id}: no chargers available (chargers.count=0)")
            # Drain the queue to avoid infinite waiting.
            ss.waiting.clear()
            return
        # Fill chargers while possible.
        while ss.free_heap and ss.waiting:
            free_t, charger_idx = heapq.heappop(ss.free_heap)
            if free_t > now_t_min:
                # Charger not free yet; put it back and stop.
                heapq.heappush(ss.free_heap, (free_t, charger_idx))
                return

            # Choose next bus by dynamically evaluating dispatch priority at decision time.
            best_idx = 0
            best_key: Optional[Tuple[float, float, str]] = None
            for i, (arrival_t, bus_id) in enumerate(ss.waiting):
                k = _dispatch_priority_key(station_id, bus_id, arrival_t, now_t_min, queue_waiting=len(ss.waiting))
                if best_key is None or k < best_key:
                    best_key = k
                    best_idx = i
            arrival_t, bus_id = ss.waiting.pop(best_idx)
            prio_key = best_key if best_key is not None else (0.0, float(arrival_t), bus_id)
            start_t = max(int(arrival_t), int(free_t), int(now_t_min))
            end_t = start_t + int(scenario.constants.charge_duration_min)

            # Record session & bus events.
            station_timelines[station_id].sessions.append(
                ChargingSession(
                    station_id=station_id,
                    charger_index=int(charger_idx),
                    bus_id=bus_id,
                    arrival_t_min=int(arrival_t),
                    start_t_min=int(start_t),
                    end_t_min=int(end_t),
                )
            )

            bt = bus_timelines[bus_id]
            if int(start_t) > int(arrival_t):
                bt.events.append(
                    TimelineEvent(
                        t_min=int(arrival_t),
                        bus_id=bus_id,
                        event_type=EventType.QUEUE_START,
                        location_id=station_id,
                    )
                )
            bt.events.append(
                TimelineEvent(
                    t_min=int(start_t),
                    bus_id=bus_id,
                    event_type=EventType.CHARGE_START,
                    location_id=station_id,
                    details={"charger_index": int(charger_idx)},
                )
            )
            bt.events.append(
                TimelineEvent(
                    t_min=int(end_t),
                    bus_id=bus_id,
                    event_type=EventType.CHARGE_END,
                    location_id=station_id,
                    details={"charger_index": int(charger_idx)},
                )
            )
            bt.total_wait_min += max(0, int(start_t) - int(arrival_t))
            bt.total_charge_min += int(scenario.constants.charge_duration_min)
            bt.charge_stops.append(station_id)

            decision_trace.append(
                f"t={start_t} station={station_id} charger={charger_idx} -> {bus_id} "
                f"(arrival={arrival_t}, key={prio_key})"
            )

            # Charger becomes free later.
            heapq.heappush(ss.free_heap, (int(end_t), int(charger_idx)))

            # After charging, schedule the bus to arrive at next stop.
            push_event(int(end_t), "BUS_CHARGED", {"bus_id": bus_id, "stop_id": station_id})

    while event_q:
        t_min, _, etype, payload = heapq.heappop(event_q)
        now = int(t_min)

        if etype == "BUS_ARRIVE":
            bus_id = str(payload["bus_id"])
            stop_index = int(payload["stop_index"])
            state = bus_states[bus_id]

            # Update bus position.
            state.stop_index = stop_index
            stop_id = state.route_stops[stop_index]

            # Range accounting: add last segment distance.
            seg_km = float(state.route_segments_km[stop_index - 1])
            state.distance_since_charge_km += seg_km
            if state.distance_since_charge_km - float(scenario.constants.battery_range_km) > 1e-6:
                violations.append(
                    f"Bus {bus_id}: exceeded range before {stop_id} "
                    f"(since last charge {state.distance_since_charge_km:.1f} km)"
                )

            bt = bus_timelines[bus_id]
            bt.events.append(
                TimelineEvent(
                    t_min=now,
                    bus_id=bus_id,
                    event_type=EventType.ARRIVE_STOP,
                    location_id=stop_id,
                )
            )

            # Destination reached.
            if stop_index == len(state.route_stops) - 1:
                bt.events.append(
                    TimelineEvent(
                        t_min=now,
                        bus_id=bus_id,
                        event_type=EventType.ARRIVE_DESTINATION,
                        location_id=stop_id,
                    )
                )
                bt.arrival_destination_t_min = now
                continue

            # Decide whether to charge here.
            if stop_id in state.plan.charge_stops:
                ss = station_states.get(stop_id)
                if ss is None:
                    violations.append(f"Bus {bus_id}: planned to charge at unknown station {stop_id}")
                    continue
                if ss.chargers_count <= 0:
                    violations.append(f"Bus {bus_id}: planned to charge at {stop_id} but station has 0 chargers")
                    # No valid continuation for this plan.
                    continue
                # Enqueue in station waiting list; priority is evaluated dynamically at dispatch time.
                ss.waiting.append((now, bus_id))
                try_start_charging(stop_id, now)
            else:
                # Skip charging, travel onward.
                next_seg_km = float(state.route_segments_km[stop_index])
                next_seg_min = _travel_time_min(next_seg_km, scenario.constants.cruise_speed_kmph)
                push_event(now + next_seg_min, "BUS_ARRIVE", {"bus_id": bus_id, "stop_index": stop_index + 1})

        elif etype == "BUS_CHARGED":
            bus_id = str(payload["bus_id"])
            state = bus_states[bus_id]
            # Reset range after charging.
            state.distance_since_charge_km = 0.0
            # Continue to next stop after current stop.
            stop_index = state.stop_index
            if stop_index >= len(state.route_stops) - 1:
                continue
            next_seg_km = float(state.route_segments_km[stop_index])
            next_seg_min = _travel_time_min(next_seg_km, scenario.constants.cruise_speed_kmph)
            push_event(now + next_seg_min, "BUS_ARRIVE", {"bus_id": bus_id, "stop_index": stop_index + 1})

            # When a bus finishes charging at a station, the charger frees at this time;
            # attempt to start the next waiting bus at that station.
            stop_id = state.route_stops[stop_index]
            if stop_id in station_states:
                try_start_charging(stop_id, now)

        else:
            violations.append(f"Unknown event type: {etype}")

    # Sort timeline events for display stability.
    for bt in bus_timelines.values():
        bt.events.sort(key=lambda e: (e.t_min, e.event_type.value, e.location_id))

    # Sort station sessions.
    for stl in station_timelines.values():
        stl.sessions.sort(key=lambda s: (s.start_t_min, s.end_t_min, s.charger_index, s.bus_id))

    return bus_timelines, station_timelines, violations

