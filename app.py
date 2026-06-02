from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from scheduler.engine import SchedulerEngine, load_scenario_json
from scheduler.models import EventType, ScheduleResult


DATA_DIR = Path(__file__).parent / "data"
DOCS_DIR = Path(__file__).parent / "docs"


def _scenario_files() -> List[Path]:
    files = sorted(DATA_DIR.glob("scenario_*.json"))
    return files


def _format_tmin_to_hhmm(t_min: int) -> str:
    # Scenario times are stored as minutes since service_date 00:00.
    h = (t_min // 60) % 24
    m = t_min % 60
    return f"{h:02d}:{m:02d}"


def _bus_timetable_df(result: ScheduleResult) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for bt in result.bus_timelines.values():
        depart = next((e.t_min for e in bt.events if e.event_type == EventType.DEPART_ORIGIN), None)
        arrive = bt.arrival_destination_t_min
        rows.append(
            {
                "bus_id": bt.bus_id,
                "operator": bt.operator_id,
                "direction": bt.direction.value,
                "charges": " → ".join(bt.charge_stops) if bt.charge_stops else "",
                "depart": _format_tmin_to_hhmm(depart) if depart is not None else "",
                "arrive": _format_tmin_to_hhmm(arrive) if arrive is not None else "",
                "total_wait_min": bt.total_wait_min,
                "total_charge_min": bt.total_charge_min,
                "ideal_arrive": _format_tmin_to_hhmm(bt.ideal_arrival_t_min) if bt.ideal_arrival_t_min is not None else "",
                "delay_vs_ideal_min": (arrive - bt.ideal_arrival_t_min) if (arrive is not None and bt.ideal_arrival_t_min is not None) else None,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["direction", "depart", "bus_id"])
    return df


def _station_queue_dfs(result: ScheduleResult) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for station_id, stl in result.station_timelines.items():
        rows: List[Dict[str, Any]] = []
        for s in stl.sessions:
            rows.append(
                {
                    "bus_id": s.bus_id,
                    "charger": s.charger_index,
                    "arrival": _format_tmin_to_hhmm(s.arrival_t_min),
                    "start": _format_tmin_to_hhmm(s.start_t_min),
                    "end": _format_tmin_to_hhmm(s.end_t_min),
                    "wait_min": s.wait_before_start_min,
                }
            )
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(["start", "charger", "bus_id"])
        out[station_id] = df
    return out


def _bus_event_log_df(result: ScheduleResult, bus_id: str) -> pd.DataFrame:
    bt = result.bus_timelines[bus_id]
    rows: List[Dict[str, Any]] = []
    for e in bt.events:
        rows.append(
            {
                "time": _format_tmin_to_hhmm(e.t_min),
                "event": e.event_type.value,
                "location": e.location_id,
                "details": json.dumps(dict(e.details)) if e.details else "",
            }
        )
    return pd.DataFrame(rows)


st.set_page_config(page_title="Bus Charging Scheduler", layout="wide")
st.title("Bus Charging Scheduler")

with st.sidebar:
    st.header("Scenario Selector")
    files = _scenario_files()
    if not files:
        st.error("No scenario files found in `data/`.")
        st.stop()

    labels = []
    raw_cache: Dict[str, Dict[str, Any]] = {}
    for f in files:
        try:
            _, raw = load_scenario_json(f)
            raw_cache[str(f)] = raw
            labels.append(raw.get("label", f.name))
        except Exception:
            labels.append(f.name)

    idx = st.selectbox("Scenario", options=list(range(len(files))), format_func=lambda i: labels[i])
    selected_file = files[int(idx)]

    st.divider()
    st.header("Scheduler Settings")
    local_iters = st.slider("Local search iterations", min_value=0, max_value=200, value=40, step=5)

scenario, raw = load_scenario_json(selected_file)
engine = SchedulerEngine()
engine.config.local_search_iterations = int(local_iters)

result = engine.schedule(scenario)

tab_scenario, tab_buses, tab_stations, tab_diag, tab_arch = st.tabs(
    ["Scenario View", "Bus Timetable", "Station Queues", "Diagnostics", "Architecture View"]
)

with tab_scenario:
    st.subheader("Scenario View")
    st.caption(f"Loaded from `{selected_file.name}`. The scheduler reads scenarios from JSON only.")
    st.json(raw, expanded=False)

with tab_buses:
    st.subheader("Bus Timetable")
    if result.violations:
        st.warning("This schedule has violations. Check Diagnostics.")
    df = _bus_timetable_df(result)
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Per-bus event log")
    bus_ids = sorted(result.bus_timelines.keys())
    if bus_ids:
        bus_id = st.selectbox("Bus", options=bus_ids)
        st.dataframe(_bus_event_log_df(result, bus_id), use_container_width=True, hide_index=True)

with tab_stations:
    st.subheader("Station Queues")
    station_dfs = _station_queue_dfs(result)
    cols = st.columns(2)
    for idx_s, (station_id, df) in enumerate(sorted(station_dfs.items(), key=lambda x: x[0])):
        with cols[idx_s % 2]:
            st.markdown(f"**Station {station_id}**")
            if df.empty:
                st.info("No charging sessions.")
            else:
                st.dataframe(df, use_container_width=True, hide_index=True)

with tab_diag:
    st.subheader("Diagnostics")

    st.markdown("**Score breakdown**")
    st.json(
        {
            "individual_component": result.score.individual,
            "operator_component": result.score.operator,
            "overall_component": result.score.overall,
            "total_weighted_score": result.score.total,
            "meta": result.score.meta,
        },
        expanded=False,
    )

    st.markdown("**Hard-constraint violations**")
    if not result.violations:
        st.success("No violations detected.")
    else:
        for v in result.violations:
            st.error(v)

    st.markdown("**Decision trace (charger allocations)**")
    if result.decision_trace:
        st.dataframe(pd.DataFrame({"trace": result.decision_trace}), use_container_width=True, hide_index=True)
    else:
        st.info("No decisions recorded.")

with tab_arch:
    st.subheader("Architecture View")
    arch_path = DOCS_DIR / "ARCHITECTURE.md"
    if arch_path.exists():
        st.markdown(arch_path.read_text(encoding="utf-8"))
    else:
        st.info("`docs/ARCHITECTURE.md` not found.")

