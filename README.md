# Bus Charging Scheduler (Streamlit)

Rule-based constraint scheduler for electric buses sharing charging stations.

## Run locally

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## How to pick a scenario
- Use the **Scenario Selector** in the sidebar.

## How to change a weight
- Edit a scenario JSON file in `data/`:

```text
"weights": { "individual": 1.0, "operator": 2.0, "overall": 1.0 }
```

Reload the app.

## How to add a new rule (no scheduler core changes)
- Add a new rule implementation in `scheduler/rules.py` (or split later into modules).
- Register it in the registry in `scheduler/rules.py` under a unique ID.
- Enable/configure it in scenario JSON under `rules.hard`, `rules.soft`, or `rules.dispatch`.

## Project structure
- `app.py`: Streamlit UI
- `data/`: scenario JSON inputs (only source of scenario data)
- `scheduler/`: scheduling engine (planner + simulator + scoring + rules)
- `docs/ARCHITECTURE.md`: architecture notes

