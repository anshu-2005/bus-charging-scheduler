# Bus Charging Scheduler — Architecture

This document designs a **scalable scheduling framework** for the Bus Charging Scheduler assignment (Python + Streamlit, single process, in-memory). It is intentionally **data-first** so that:

- **Changing a weight** is editing **one obvious value** in one scenario file.
- **Adding a new rule** is adding a new rule module/config and registering it (no engine rewrite).
- **Growing the world** (more buses/stations/routes/chargers/operators) is primarily **data changes**, not structural rewrites.

---

## Goals & non-goals

### Goals
- Produce a valid schedule for each scenario:
  - a per-bus timeline (drives, arrivals, waiting, charging, final arrival)
  - a per-station charger timeline (ordered charging sessions)
- Provide tunable soft-objective weights:
  - **individual** fairness (avoid extreme waits)
  - **operator** fairness / smoothness
  - **overall** efficiency / throughput
- Make future changes easy via configuration:
  - more stations, multiple chargers, multiple routes, priority buses
  - electricity pricing, driver shifts, maintenance windows, station outages

### Non-goals (explicitly out of scope for v1)
- Persistent DB, authentication, maps/animations, distributed execution
- Real-time traffic variability or stochastic simulation (unless modeled as data)

---

## Hard constraints analysis (must always hold)

These are modeled as **hard rules**. Any candidate schedule that violates them is invalid.

### Physics / energy constraints
- **Battery range**: full charge gives **240 km** max range.
- **Charging action**: always charges **to full**, and takes **exactly 25 minutes**.
- **Range feasibility**: between consecutive charges (or start → first charge, last charge → arrival), a bus cannot traverse \(>240\) km.

### Route / movement constraints
- **Fixed route order**: a bus visits stations in route order; **no backtracking**.
- **Directionality**: buses travel both directions; they share stations A/B/C/D.
- **Endpoints** (Bengaluru, Kochi): assume slow chargers, so **each bus departs with full charge**; endpoints are not “scheduled chargers” in v1.

### Resource constraints
- **Charger capacity**: each station has **1 charger** in the provided scenarios; generalized as \(N\) chargers per station.
- **No overlap**: one charger can serve **only one bus at a time**.

### Determinism constraints (simulation)
- **Speed consistency**: all buses have the same constant speed; travel time is distance / speed.

---

## Soft constraints analysis (optimization objectives)

Soft constraints drive “better” schedules when multiple valid schedules exist.

### Objective 1 — Individual bus fairness
Minimize extreme waiting or total delay for any single bus. Typical penalty terms:
- waiting time before charging
- total trip duration vs “ideal” (no-wait) duration
- number of charging stops (optional tie-breaker)

**Rationale**: avoid schedules where most buses are fine but one bus is severely delayed.

### Objective 2 — Operator fleet smoothness
Encourage fairness and predictability across operators’ fleets. Examples:
- minimize variance of delays within an operator
- cap how many buses from an operator get “starved” in station queues

**Rationale**: operators experience the schedule as a fleet outcome, not just per-bus.

### Objective 3 — Overall network efficiency
Minimize aggregate inefficiency:
- total waiting time across all buses
- makespan (when the last bus arrives)

**Rationale**: the system should run efficiently as a whole.

### Tunable weights (must not be hardcoded)
Each scenario provides weights for the three objectives:
- `weights.individual`
- `weights.operator`
- `weights.overall`

The scheduler computes:
\[
Score = w_i \cdot J_{individual} + w_o \cdot J_{operator} + w_g \cdot J_{overall}
\]

All weights and objective parameters live in scenario data. The engine only consumes them.

---

## Complete architecture design (high level)

### Key idea: data-driven scheduling pipeline
**Inputs**: a scenario file fully describing:
- world topology (routes, segments, stations)
- resources (chargers per station)
- fleet (buses, operators, departures, direction)
- constants (battery range, charge duration, speed)
- enabled rule set and objective weights

**Outputs**: a `ScheduleResult` containing:
- per-bus event timelines
- per-station charger timelines
- violations (if any) and score breakdown

### Components (single-process, modular)
- **Scenario loader**: validates schema, normalizes times, builds in-memory model.
- **Core domain model**: Route/Station/Charger/Bus + schedule state.
- **Scheduler framework**: orchestrates planning + dispatch + simulation + scoring.
- **Rule engine**:
  - hard rules: feasibility checks (reject/repair)
  - soft rules: objective scoring (weighted)
  - dispatch rules: queue priority signals
- **Simulation engine**: computes timelines from decisions (drive/arrive/wait/charge).
- **UI (Streamlit)**: scenario picker, input view, per-bus timetable, per-station order view.

### Approach choice (fit + scalability)
Use an **event-driven simulation** with **pluggable planning and dispatch**:
- **Planning** decides which stations each bus will charge at (feasible sequences under 240 km).
- **Dispatch** decides who charges next at each station when multiple buses are waiting.

This cleanly accommodates future requirements by adding rules/attributes without restructuring the engine.

---

## Scheduler framework design

### Decision points
Decisions occur when:
- a bus arrives at a candidate charging station
- a station charger becomes available and multiple buses are queued
- a bus can choose to skip charging (if it can still reach its next planned charge/endpoint)

### Core scheduling phases

#### Phase A — Feasible plan generation (per bus)
Generate feasible charge-stop sequences using the route graph:
- nodes: origin, stations, destination
- feasible edge if distance \( \le \) battery range
- candidate paths from origin → destination through charge nodes

For the given base problem this is small; for larger worlds it remains a bounded search with pruning.

#### Phase B — Global dispatch simulation
Simulate time forward:
- buses depart at their departure times
- travel deterministically by segment times
- on arrival at a station in their plan:
  - if a charger is free: start charging
  - else: queue at the station
- when a charger frees: pick next bus by a **dispatch priority function** built from dispatch rules

#### Phase C — Scoring and optional improvement
Compute objective terms and weighted score. Optionally improve by:
- reselecting among candidate plans for some buses (local search)
- adjusting dispatch tie-breakers

The framework supports swapping in a more advanced optimizer later without changing the rule interfaces.

---

## Rule engine design

### Rule categories

#### Hard rules (must always pass)
Checked at plan-generation time and/or during simulation:
- Range feasibility (240 km between charges)
- Charger capacity (no overlap on a single charger)
- No backtracking (route order respected)
- Fixed charge duration (25 minutes) / charge-to-full model
- Future: maintenance windows, outages, driver constraints

#### Soft rules (objective terms)
Compute penalties/bonuses; combined by scenario weights:
- individual total wait / max wait
- operator fairness (e.g., variance of delay)
- overall total wait / makespan
- future: electricity cost, priority adherence, shift compliance slack

#### Dispatch rules (queue priority signals)
At a station with contention, compute per-bus priority signals that can incorporate:
- urgency (risk of downstream infeasibility)
- bus priority class
- operator fairness adjustments
- lateness relative to ideal timeline

### Rule registration & configuration
- Rules are referenced by stable string IDs in scenario JSON.
- Each rule receives free-form `params`.
- Adding a new rule requires only:
  - new module implementing the rule interface
  - single registry entry mapping `id -> rule class`
  - scenario JSON enabling/configuring it

---

## Data model design

### Core entities

#### World
- `Station(station_id, chargers, calendars)`
- `Charger(charger_id, station_id, capability_tags?, calendars)`
- `Route(route_id, stops[], segments_km[], scheduled_charge_stations[])`
- `Segment(from_stop, to_stop, distance_km, travel_time_min)`

#### Fleet
- `Bus(bus_id, operator_id, direction, route_id, departure_time, attributes{})`
- `Operator(operator_id, name)`

#### Schedule output
- `BusTimeline(bus_id, events[], kpis{})`
- `StationTimeline(station_id, chargers[{charger_id, sessions[]}])`
- `ChargingSession(bus_id, start, end, wait_before_start_min)`

### Time model
- Parse scenario-local `HH:MM` into absolute timestamps anchored by `service_date`.
- Normalize internally to integer minutes since scenario start to simplify scheduling math.

### Why this model survives future changes
- More stations/routes/chargers/operators: additive data.
- Priority buses/pricing/shifts/outages: new attributes + new rule modules; engine unchanged.

---

## JSON scenario schema

### Recommended scenario document shape

```json
{
  "scenario_id": "scenario_1_even_spacing",
  "label": "Scenario 1 — Even spacing",
  "service_date": "2026-06-02",
  "constants": {
    "battery_range_km": 240,
    "charge_duration_min": 25,
    "cruise_speed_kmph": 60
  },
  "operators": [
    { "operator_id": "kpn", "name": "KPN" },
    { "operator_id": "freshbus", "name": "Freshbus" },
    { "operator_id": "flixbus", "name": "Flixbus" }
  ],
  "stations": [
    { "station_id": "A", "name": "A", "chargers": { "count": 1 } },
    { "station_id": "B", "name": "B", "chargers": { "count": 1 } },
    { "station_id": "C", "name": "C", "chargers": { "count": 1 } },
    { "station_id": "D", "name": "D", "chargers": { "count": 1 } }
  ],
  "routes": [
    {
      "route_id": "blr_kochi_main",
      "name": "Bengaluru ↔ Kochi",
      "stops": ["Bengaluru", "A", "B", "C", "D", "Kochi"],
      "segments_km": [100, 120, 100, 120, 100],
      "scheduled_charge_stations": ["A", "B", "C", "D"]
    }
  ],
  "buses": [
    {
      "bus_id": "bus-BK-01",
      "operator_id": "kpn",
      "direction": "BENGALURU_TO_KOCHI",
      "route_id": "blr_kochi_main",
      "departure_time": "19:00",
      "attributes": { "priority": 0 }
    }
  ],
  "weights": { "individual": 1.0, "operator": 1.0, "overall": 1.0 },
  "rules": {
    "hard": [
      { "id": "charger_capacity", "enabled": true },
      { "id": "range_feasible", "enabled": true },
      { "id": "no_backtracking", "enabled": true },
      { "id": "fixed_charge_duration", "enabled": true }
    ],
    "soft": [
      { "id": "individual_total_wait", "enabled": true, "weight_key": "individual" },
      { "id": "operator_delay_variance", "enabled": true, "weight_key": "operator" },
      { "id": "overall_total_time", "enabled": true, "weight_key": "overall" }
    ],
    "dispatch": [
      { "id": "default_priority", "enabled": true }
    ]
  }
}
```

### Formal JSON Schema (Draft 2020-12)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.local/bus-charging-scheduler/scenario.schema.json",
  "type": "object",
  "required": ["scenario_id", "constants", "operators", "stations", "routes", "buses", "weights", "rules"],
  "properties": {
    "scenario_id": { "type": "string", "minLength": 1 },
    "label": { "type": "string" },
    "service_date": { "type": "string", "description": "ISO date (YYYY-MM-DD) used to anchor HH:MM times." },
    "constants": {
      "type": "object",
      "required": ["battery_range_km", "charge_duration_min", "cruise_speed_kmph"],
      "properties": {
        "battery_range_km": { "type": "number", "exclusiveMinimum": 0 },
        "charge_duration_min": { "type": "integer", "minimum": 1 },
        "cruise_speed_kmph": { "type": "number", "exclusiveMinimum": 0 }
      },
      "additionalProperties": true
    },
    "operators": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["operator_id"],
        "properties": {
          "operator_id": { "type": "string", "minLength": 1 },
          "name": { "type": "string" }
        },
        "additionalProperties": true
      }
    },
    "stations": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["station_id", "chargers"],
        "properties": {
          "station_id": { "type": "string", "minLength": 1 },
          "name": { "type": "string" },
          "chargers": {
            "type": "object",
            "required": ["count"],
            "properties": {
              "count": { "type": "integer", "minimum": 0 }
            },
            "additionalProperties": true
          },
          "outages": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["start", "end"],
              "properties": {
                "start": { "type": "string" },
                "end": { "type": "string" },
                "reason": { "type": "string" }
              },
              "additionalProperties": true
            }
          }
        },
        "additionalProperties": true
      }
    },
    "routes": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["route_id", "stops", "segments_km"],
        "properties": {
          "route_id": { "type": "string", "minLength": 1 },
          "name": { "type": "string" },
          "stops": { "type": "array", "minItems": 2, "items": { "type": "string" } },
          "segments_km": { "type": "array", "minItems": 1, "items": { "type": "number", "exclusiveMinimum": 0 } },
          "scheduled_charge_stations": { "type": "array", "items": { "type": "string" } }
        },
        "additionalProperties": true
      }
    },
    "buses": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["bus_id", "operator_id", "direction", "route_id", "departure_time"],
        "properties": {
          "bus_id": { "type": "string", "minLength": 1 },
          "operator_id": { "type": "string", "minLength": 1 },
          "direction": { "type": "string", "enum": ["BENGALURU_TO_KOCHI", "KOCHI_TO_BENGALURU"] },
          "route_id": { "type": "string", "minLength": 1 },
          "departure_time": { "type": "string", "description": "HH:MM local time" },
          "attributes": { "type": "object", "additionalProperties": true }
        },
        "additionalProperties": true
      }
    },
    "weights": {
      "type": "object",
      "required": ["individual", "operator", "overall"],
      "properties": {
        "individual": { "type": "number", "minimum": 0 },
        "operator": { "type": "number", "minimum": 0 },
        "overall": { "type": "number", "minimum": 0 }
      },
      "additionalProperties": true
    },
    "rules": {
      "type": "object",
      "required": ["hard", "soft", "dispatch"],
      "properties": {
        "hard": { "$ref": "#/$defs/ruleList" },
        "soft": { "$ref": "#/$defs/ruleList" },
        "dispatch": { "$ref": "#/$defs/ruleList" }
      },
      "additionalProperties": true
    }
  },
  "$defs": {
    "ruleList": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "enabled"],
        "properties": {
          "id": { "type": "string", "minLength": 1 },
          "enabled": { "type": "boolean" },
          "weight_key": { "type": "string" },
          "params": { "type": "object", "additionalProperties": true }
        },
        "additionalProperties": true
      }
    }
  },
  "additionalProperties": true
}
```

---

## Folder structure (proposed)

```text
Bus-Charging-Scheduler/
  ARCHITECTURE.md
  README.md
  requirements.txt
  app/
    streamlit_app.py
    ui/
      scenario_picker.py
      views/
        scenario_input_view.py
        bus_timetable_view.py
        station_timeline_view.py
  scheduler/
    engine/
      scheduler.py
      dispatch.py
      simulation.py
      scoring.py
    model/
      scenario.py
      world.py
      fleet.py
      timeline.py
    planning/
      plan_generator.py
    rules/
      registry.py
      hard/
      soft/
      dispatch/
    io/
      scenario_loader.py
      scenario_schema.json
  scenarios/
    scenario_1_even_spacing.json
    scenario_2_bunched_start.json
    scenario_3_asymmetric_load.json
    scenario_4_operator_heavy.json
    scenario_5_worst_case_convergence.json
  tests/
    test_scenarios_validate.py
    test_hard_rules.py
```

---

## Scalability strategy

- **Data scalability**: routes/stations/chargers are declarative; scenario validation keeps growth safe.
- **Algorithm scalability**: start with greedy + local search; later add optional CP-SAT/MILP backend behind the same interfaces.
- **Rule scalability**: rules are plugins configured by data; engine stays stable as rules grow.
- **Explainability**: store decision traces and score breakdowns so schedules are “defensible”.

---

## Assumptions

- Endpoints provide full charge at departure; endpoint charging isn’t scheduled in v1.
- Charging always fills to full in exactly 25 minutes (per spec).
- Constant speed, no traffic variability, deterministic travel times.

---

## How to change a weight (via data)

Edit a single value in scenario JSON:

```text
"weights": { "individual": 1.0, "operator": 2.0, "overall": 1.0 }
```

---

## How to add a new rule (via plugin + data)

- Add a new rule module and one registry entry.
- Enable it in scenario JSON:

```text
rules:
  hard:
    - id: "station_outage"
      enabled: true
      params: { station_id: "B", start: "20:00", end: "21:00" }
```

---

## Future changes anticipated (and handled without engine rewrites)

- **More stations / different segment distances**: update `routes[].stops` + `segments_km`.
- **Multiple chargers**: update `stations[].chargers.count`.
- **Multiple routes sharing stations**: add routes; buses point to `route_id`; stations are shared resources.
- **Priority buses**: set `buses[].attributes.priority`; dispatch rules incorporate.
- **Electricity pricing**: add pricing curve data; add a cost soft rule.
- **Driver shifts**: model duty windows; add hard/soft rules.
- **Maintenance windows / outages**: add `stations[].outages` (and later charger-level outages); add hard rule.

