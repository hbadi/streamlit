# Live Revit — Streamlit demos

Side-car web apps for **live Revit interaction** via the
[`revit`](https://github.com/hbadi/revit) Python package's IPC layer.
Browser-based dashboards that talk to a running Revit instance — no
export, no sync, no cloud.

What makes these demos different from Speckle / Forge / BIM 360 :
real-time round-trips between the browser and the live Revit model.
Click in the browser → instant transaction in Revit. Modify in Revit →
the dashboard data updates on the next interaction.

## Demos

### `ex_dashboard.py` — BIM Explorer

KPI strip (levels, rooms, sheets, views, schedules) + interactive
charts (click a category bar → isolate in Revit) + rooms treemap with
drill-down to per-room cards + schedule browser with row-click isolate.

### `ex_health_check.py` — Model Health Check

Ideate-style audit dashboard : Project Info, Warnings / File Size
ratio banner, 9 health KPI tiles (turn red over threshold), severity
donut, top warning descriptions bar, **live 3D viewer** (GLB-on-disk
+ local-read pattern to avoid IPC frame-size issues).

### `ex_schedules_isolate.py` — Schedule grid

Schedule rows → isolate corresponding elements in the active Revit
view (with ghost 10% overlay on others). Checkbox column → native
Revit selection. Click + check are two independent interactions on
the same AG Grid widget.

## Setup

```bash
pip install -r requirements.txt
```

You also need the `revit` package (private at the moment — see the
[main project](https://github.com/hbadi/revit) for status). Revit must
be running locally with the RevitShell IPC server active.

## Run

**Recommended — multi-demo launcher** (single URL, sidebar navigation
between all 3 demos, shared IPC connection) :

```bash
streamlit run home.py
```

Opens `http://localhost:8501` with a sidebar menu to switch between
demos.

**Single demo** :

```bash
streamlit run ex_dashboard.py
streamlit run ex_health_check.py
streamlit run ex_schedules_isolate.py
```

## Architecture

```
Browser  ──HTTP──▶  Streamlit (Python)  ──IPC ZMQ──▶  Revit.exe
                       │
                       └─ st.cache_resource : 1 IPC connection per session
                       └─ st.cache_data ttl=120s : avoid re-batching
                          heavy snapshots on every interaction
```

Each `@revit.batch` decorator marks a function that runs server-side
inside Revit's embedded Python — a single round-trip to query the
live model and return the result. Streamlit reruns the script on each
interaction ; the cache prevents IPC saturation.

## Notable patterns

- **`st.cache_resource`** for the IPC connection (1 per session, not
  per rerun).
- **`st.cache_data(ttl=120)`** for heavy snapshots (full BIM scan).
- **`_safe_call()`** wrapper around every `@revit.batch` so IPC drops
  don't crash the app — sets a `_ipc_error` flag, shows a banner,
  offers Reconnect button.
- **`_grid_key` rotation** on `AgGrid` to clear selections when the
  user clicks Restore view (prevents accidental re-isolate on next
  rerun from leftover selection state).
- **GLB-on-disk + local read** for 3D viewer (vs base64-inline via IPC
  which corrupts the ZMQ channel above ~30 MB).

## License

MIT.
