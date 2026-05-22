"""home.py — Streamlit multi-app launcher.

Single port (8501), single URL, sidebar navigation between the 3
demos. Uses st.navigation (built-in since Streamlit 1.36) — replaces
the legacy `pages/` folder convention.

Run :
    streamlit run home.py

Then pick a demo in the left sidebar :
    📊 BIM Explorer      — KPIs + drill-down rooms + schedule isolate
    🩺 Model Health      — Ideate-style audit + live 3D viewer
    📋 Schedules Isolate — schedule grid : row click = isolate

All pages share :
- 1 IPC connection to Revit (st.cache_resource at process level)
- st.cache_data caches (efficient if you switch between pages)
- session_state (set_X in one page = visible in another ; use keys
  prefixed by page name if you need isolation)

Caveat : each demo also calls st.set_page_config() at its top — only
the FIRST config (this file's, on first page render) wins ; pages'
configs are ignored with a warning. Harmless but noisy in dev.
"""
import streamlit as st

st.set_page_config(
    page_title='Live Revit demos',
    page_icon='🏢',
    layout='wide',
    initial_sidebar_state='expanded',
)

pg = st.navigation({
    'Demo': [
        st.Page('ex_dashboard.py',         title='BIM Explorer', icon='📊'),
        st.Page('ex_health_check.py',      title='Model Health', icon='🩺'),
    ],
    'Tools': [
        st.Page('ex_schedules_isolate.py', title='Schedules Isolate',
                icon='📋'),
    ],
})
pg.run()
