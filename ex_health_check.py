"""ex_health_check.py — Live Revit Model Health Check dashboard.

Inspiration : Ideate BIM Manager / Revit Model Health Check (Power BI).
Audit qualité du modèle Revit ouvert :

    - Project Information : file, version, file size, path
    - KPIs santé (rouges si dépassent un seuil) :
        * Views Not on Sheet
        * Model Groups  (CategoryWrapper.count())
        * Avg Annotation Issues / Sheet
        * CAD Imports
        * In-Place Families  (family.in_place)
        * Warnings (doc.warnings.Count())
        * Warnings / File Size (warnings.Count / file_size_mb)
        * Model Lines
        * Detail Groups
    - Severity donut (Warning / Error / DocumentCorruption)
    - Top Warnings Descriptions (bar chart)

Pré-requis :
    pip install streamlit plotly
    revit (avec Revit qui tourne + IPC actif)

Lancer :
    streamlit run examples/streamlit/ex_health_check.py
"""
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

import revit
from revit.remote._client import IpcError


# ────────────────────────────────────────────────────────────────────
# Config + connexion + state
# ────────────────────────────────────────────────────────────────────
st.set_page_config(page_title='Model Health Check',
                   page_icon='🩺', layout='wide',
                   initial_sidebar_state='expanded')


@st.cache_resource
def _connect():
    return revit.connect(name='bim-health-check', ghost=True)


def _safe_connect():
    try:
        _connect()
        return True, None
    except (IpcError, RuntimeError, ConnectionError, OSError) as e:
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


st.session_state.setdefault('_ipc_error', None)
st.session_state.setdefault('_ipc_poisoned', False)


# ────────────────────────────────────────────────────────────────────
# Health thresholds (red KPI when exceeded) — Ideate-style defaults
# ────────────────────────────────────────────────────────────────────
THRESHOLDS = {
    'views_not_on_sheet':   500,
    'model_groups':         1000,
    'avg_annot_per_sheet':  5,
    'cad_imports':          0,    # any CAD import = warning
    'in_place_families':    5,
    'warnings':             200,
    'warnings_per_mb':      1.0,  # < 1 = green
    'model_lines':          50,
    'detail_groups':        500,
}


# ────────────────────────────────────────────────────────────────────
# Server-side fetcher — full snapshot in 1 IPC roundtrip
# ────────────────────────────────────────────────────────────────────
@revit.batch
def _fetch_health_raw():
    """One-shot scan : KPIs + warnings detail. Uses fast `cat.count()`
    + `family.in_place` + `view.is_placed` + `doc.warnings`."""
    import revit as _r

    doc = _r.activeDoc
    cats = list(doc.categories)
    by_name = {c.name: c for c in cats}

    def _cat_count(name):
        c = by_name.get(name)
        return c.count() if c else 0

    # Views placement : single FEC pass to build placed-view-ids set,
    # then count "not placed" via membership test (vs view.is_placed
    # per-view which would iterate viewports N times).
    import Autodesk.Revit.DB as DB
    placed_ids = {
        vp.ViewId.Value if hasattr(vp.ViewId, 'Value')
        else vp.ViewId.IntegerValue
        for vp in DB.FilteredElementCollector(doc.unwrap()).OfClass(DB.Viewport)
    }
    n_views = len(doc.views)
    n_views_unplaced = sum(1 for v in doc.views if int(v.id) not in placed_ids)

    # In-place families : iterate doc.families
    n_in_place = sum(1 for f in doc.families if f.in_place)

    # CAD Imports (OST_ImportObject category)
    cad_count = _cat_count('Imports in Families') + _cat_count('CAD Links') \
        + (by_name['Imports'].count() if 'Imports' in by_name else 0)

    # Warnings : single fetch, build per-severity + per-description maps
    warnings = doc.warnings.ToList()
    warnings_data = [
        {'severity': w.severity, 'description': w.description,
         'n_elements': len(w.element_ids)}
        for w in warnings
    ]

    n_warnings = len(warnings)
    fs_mb = doc.file_size_mb
    warnings_per_mb = round(n_warnings / fs_mb, 2) if fs_mb > 0 else 0.0

    # Annotation issues per sheet — approximation : warnings affecting
    # elements visible on a sheet, divided by sheet count. Cheap stand-in
    # for the Ideate-precise metric. We default to 0.0 (full impl would
    # require per-sheet warning attribution).
    n_sheets = len(doc.sheets)
    avg_annot = 0.0

    return {
        'project':            doc.title,
        'path':               doc.path,
        'file_size_mb':       fs_mb,
        'revit_version':      _r.info.version,
        'revit_version_name': _r.info.version_name,
        'username':           _r.info.username,
        'kpis': {
            'views_not_on_sheet':  n_views_unplaced,
            'n_views':             n_views,
            'model_groups':        _cat_count('Model Groups'),
            'detail_groups':       _cat_count('Detail Groups'),
            'model_lines':         _cat_count('Lines'),
            'cad_imports':         cad_count,
            'in_place_families':   n_in_place,
            'warnings':            n_warnings,
            'warnings_per_mb':     warnings_per_mb,
            'avg_annot_per_sheet': avg_annot,
            'n_sheets':            n_sheets,
        },
        'warnings':           warnings_data,
    }


@st.cache_data(ttl=120)
def fetch_health():
    try:
        return _fetch_health_raw()
    except (IpcError, RuntimeError, ConnectionError, OSError) as e:
        st.session_state['_ipc_error'] = f"{type(e).__name__}: {e}"
        return None


# ────────────────────────────────────────────────────────────────────
# 3D view rendering — revit.html3d(view) returns a self-contained
# HTML string (Three.js + GLB base64-inlined). Streamlit renders it
# via components.html() in an iframe. Cached 5 min (GLB export is
# the heavy part).
# ────────────────────────────────────────────────────────────────────
@revit.batch
def _list_3d_views_raw():
    """Only true 3D views renderable to GLB. Filters out :
    - templates (is_template=True) — not renderable
    - views whose name starts with '<' (placeholder views Revit creates)
    """
    import revit as _r
    return [v.name for v in _r.activeDoc.views
            if v.is_3d
            and not v.is_template
            and not (v.name or '').startswith('<')]


@st.cache_data(ttl=300)
def list_3d_views():
    try:
        return _list_3d_views_raw()
    except (IpcError, RuntimeError, ConnectionError, OSError) as e:
        st.session_state['_ipc_error'] = f"{type(e).__name__}: {e}"
        return []


@revit.batch
def _export_glb_for_view(view_name):
    """Server-side : export view → GLB file on disk, return PATH only
    (small ~200 chars). The HTML+base64-inline (~46 MB for a building
    view) is NOT transferred via IPC — that's what poisoned the ZMQ
    channel before (symptom: 'invalid frame length: <huge>').

    Assumes Streamlit + Revit run on the SAME MACHINE — true for our
    IPC live-model architecture anyway."""
    import revit as _r
    from revit._show3d import _to_glb
    view = _r.activeDoc.views[view_name]
    glb_path, is_tmp = _to_glb(view, None)
    return {'path': glb_path, 'is_tmp': is_tmp}


@st.cache_data(ttl=300)
def get_3d_html(view_name, height=350):
    """Two-step render to bypass the IPC frame-size corruption :
    1. Server-side @batch : export GLB to disk, return path (small).
    2. Client-side (this process) : read the local GLB file and let
       `revit.html3d(bytes)` build the inline-base64 HTML locally.

    No big-payload IPC transfer → no channel poisoning."""
    import os
    try:
        info = _export_glb_for_view(view_name)
    except (IpcError, RuntimeError, ConnectionError, OSError) as e:
        st.session_state['_ipc_error'] = f"{type(e).__name__}: {e}"
        st.session_state['_ipc_poisoned'] = True
        return None
    except Exception as e:
        st.session_state['_ipc_error'] = (
            f"GLB export failed server-side "
            f"({type(e).__name__}: {e}). Try another view.")
        return None

    glb_path = info.get('path')
    is_tmp = info.get('is_tmp', False)
    if not glb_path or not os.path.isfile(glb_path):
        st.session_state['_ipc_error'] = (
            f"GLB file not found at {glb_path!r} (Streamlit and Revit "
            "must run on the same machine for this 2-step render).")
        return None
    try:
        with open(glb_path, 'rb') as f:
            glb_bytes = f.read()
        # revit.html3d accepts bytes natively — runs purely client-side
        # since no Revit context is needed when obj is already bytes.
        return revit.html3d(glb_bytes, height=height)
    finally:
        if is_tmp:
            try: os.remove(glb_path)
            except OSError: pass


def _reconnect():
    """Force-disconnect (close ZMQ socket, drop pending frames) then
    reconnect from scratch. Required when the IPC channel is
    DESYNCED (typical symptom: 'invalid frame length: <huge number>'
    from a partial frame left over by a server-side exception
    mid-write). A simple clear of @st.cache_resource isn't enough —
    the underlying socket needs to be torn down."""
    try:
        revit.disconnect(keep_log=False)
    except Exception:
        pass
    _connect.clear()
    fetch_health.clear()
    get_3d_html.clear()
    list_3d_views.clear()
    st.session_state['_ipc_error'] = None
    st.session_state['_ipc_poisoned'] = False


def _reload_revit():
    """Reload `revit` package — both Streamlit process AND IPC server.
    Pick up new wrappers without restarting either."""
    try:
        revit.reload(remote=True)
        fetch_health.clear()
        st.session_state['_ipc_error'] = None
        st.toast('revit reloaded (both sides)', icon='♻️')
    except Exception as e:
        st.session_state['_ipc_error'] = f"reload failed: {e}"


# ────────────────────────────────────────────────────────────────────
# Connection gate
# ────────────────────────────────────────────────────────────────────
_ok, _conn_err = _safe_connect()
if not _ok:
    st.session_state['_ipc_error'] = _conn_err

if st.session_state.get('_ipc_poisoned'):
    st.title('🩺 Revit Model Health Check')
    st.error('⚠️ **IPC channel desynced** — a server-side error left the '
             'ZMQ pipe in a bad state. Full disconnect + reconnect required.')
    err = st.session_state.get('_ipc_error')
    if err:
        st.code(err, language=None)
    st.button('🔌 Force reconnect', type='primary', on_click=_reconnect)
    st.caption('If the reconnect fails too, restart the Revit IPC server '
               '(or Revit itself in the worst case).')
    st.stop()

if st.session_state.get('_ipc_error') or not _ok:
    st.title('🩺 Revit Model Health Check')
    st.error('⚠️ **Revit IPC not reachable.**')
    err = st.session_state.get('_ipc_error') or _conn_err
    if err:
        st.code(err, language=None)
    st.button('🔌 Reconnect', type='primary', on_click=_reconnect)
    st.stop()


# ────────────────────────────────────────────────────────────────────
# SIDEBAR
# ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header('🩺 Actions')
    if st.button('♻️ Force refresh', use_container_width=True):
        fetch_health.clear()
        st.rerun()
    st.button('🔌 Reconnect Revit', use_container_width=True,
              on_click=_reconnect)
    st.button('🔄 Reload revit code', use_container_width=True,
              on_click=_reload_revit,
              help='Pick up newly deployed wrappers without restarting '
                   'Streamlit nor Revit')
    if st.session_state.get('_ipc_error'):
        st.warning(f"⚠️ Last IPC error :\n`{st.session_state['_ipc_error']}`")
    st.divider()
    st.caption(f"Last scan : {datetime.now():%H:%M:%S}")
    st.caption('Data cached 2 min')


# ────────────────────────────────────────────────────────────────────
# DATA
# ────────────────────────────────────────────────────────────────────
with st.spinner('🩺 Auditing model…'):
    snap = fetch_health()

if snap is None:
    st.title('🩺 Revit Model Health Check')
    st.error('⚠️ Lost connection during audit.')
    st.button('🔌 Reconnect', type='primary', on_click=_reconnect)
    st.stop()

kpis = snap['kpis']


def _kpi_color(value, threshold, *, lower_is_better=True):
    """Returns 'red' if KPI exceeds threshold (or below if not lower_is_better)."""
    if lower_is_better:
        return 'red' if value > threshold else 'normal'
    return 'red' if value < threshold else 'normal'


def _metric(col, label, value, threshold=None, *, suffix='',
            lower_is_better=True):
    """Render an st.metric with red value when threshold exceeded.
    Uses delta_color hack : custom HTML for the red color."""
    color = (_kpi_color(value, threshold, lower_is_better=lower_is_better)
             if threshold is not None else 'normal')
    if color == 'red':
        col.markdown(
            f"<div style='font-size:0.8em;color:rgba(250,250,250,0.6)'>{label}</div>"
            f"<div style='font-size:2em;font-weight:600;color:#e74c3c'>"
            f"{value:,.0f}{suffix}</div>",
            unsafe_allow_html=True)
    else:
        col.metric(label, f"{value:,.0f}{suffix}")


# ────────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────────
st.title('🩺 Revit Model Health Check')
st.caption('Live audit of the open Revit document')

# ── Project Information (left) + 3D View (right) ──────────────────
left, right = st.columns([2, 3])
with left:
    st.subheader('Project Information')
    st.markdown(f"**File** : `{snap['project']}`")
    st.markdown(f"**Path** : `{snap['path'] or '<unsaved>'}`")
    st.markdown(f"**Revit Version** : {snap['revit_version_name']} "
                f"(`{snap['revit_version']}`)")
    st.markdown(f"**File Size** : `{snap['file_size_mb']} MB`")
    st.markdown(f"**Author** : {snap['username']}")
with right:
    st.subheader('🎲 3D View')
    views_3d = list_3d_views()
    if views_3d:
        # Placeholder first → user picks explicitly. Auto-defaulting to
        # the first 3D view was risky because some "valid" 3D views
        # (large section boxes, complex curtain walls, etc.) fail
        # during GLB export and poison the IPC channel.
        options = ['— pick a 3D view —'] + list(views_3d)
        sel_view = st.selectbox('View', options, key='3d_view_pick',
                                label_visibility='collapsed', index=0)
        if sel_view == options[0]:
            st.caption('💡 Pick a view to render. Heavy views (entire '
                       'building, complex curtain walls) may fail — if '
                       'so, use Force reconnect.')
        else:
            with st.spinner(f'Rendering `{sel_view}`…'):
                html_3d = get_3d_html(sel_view, 320)
            if html_3d:
                components.html(html_3d, height=340, scrolling=False)
            else:
                st.warning(
                    f'Could not render `{sel_view}` — likely too large '
                    'or geometry unsupported. Try another view, then '
                    '**Force reconnect** in the sidebar to recover IPC.')
    else:
        st.info('No 3D views in this document.')

# ── Warnings / File Size ratio — full-width banner ────────────────
ratio = kpis['warnings_per_mb']
if ratio < THRESHOLDS['warnings_per_mb']:
    st.success(
        f"✅ **Warnings / File Size ratio : {ratio}** per MB — healthy "
        f"(threshold < {THRESHOLDS['warnings_per_mb']}) · "
        f"{kpis['warnings']} warnings / {snap['file_size_mb']} MB")
else:
    st.error(
        f"⚠️ **Warnings / File Size ratio : {ratio}** per MB — "
        f"over threshold ({THRESHOLDS['warnings_per_mb']}) · "
        f"{kpis['warnings']} warnings / {snap['file_size_mb']} MB")

# ── KPI grid (9 tiles, red when threshold exceeded) ───────────────
st.subheader('Model Health KPIs')
r1 = st.columns(5)
_metric(r1[0], 'Views Not on Sheet', kpis['views_not_on_sheet'],
        THRESHOLDS['views_not_on_sheet'])
_metric(r1[1], 'Model Groups', kpis['model_groups'],
        THRESHOLDS['model_groups'])
_metric(r1[2], 'Avg Annotation / Sheet', kpis['avg_annot_per_sheet'],
        THRESHOLDS['avg_annot_per_sheet'])
_metric(r1[3], 'CAD Imports', kpis['cad_imports'],
        THRESHOLDS['cad_imports'])
_metric(r1[4], 'In-Place Families', kpis['in_place_families'],
        THRESHOLDS['in_place_families'])

r2 = st.columns(5)
_metric(r2[0], 'Warnings', kpis['warnings'],
        THRESHOLDS['warnings'])
_metric(r2[1], 'Warnings / MB', kpis['warnings_per_mb'],
        THRESHOLDS['warnings_per_mb'])
_metric(r2[2], 'Model Lines', kpis['model_lines'],
        THRESHOLDS['model_lines'])
_metric(r2[3], 'Detail Groups', kpis['detail_groups'],
        THRESHOLDS['detail_groups'])
_metric(r2[4], 'Total Views / Sheets', kpis['n_views'])

st.divider()

# ── Warnings analysis ─────────────────────────────────────────────
warnings_data = snap['warnings']
if warnings_data:
    wdf = pd.DataFrame(warnings_data)
    col_pie, col_bar = st.columns([1, 2])

    # ── Donut : severity distribution
    with col_pie:
        st.subheader('Warnings by severity')
        sev_counts = wdf['severity'].value_counts().reset_index()
        sev_counts.columns = ['severity', 'count']
        sev_colors = {
            'DocumentCorruption': '#8B0000',
            'Error':              '#e74c3c',
            'Warning':            '#f39c12',
        }
        fig = px.pie(sev_counts, names='severity', values='count',
                     hole=0.55, height=380,
                     color='severity', color_discrete_map=sev_colors)
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=10),
                          legend=dict(orientation='h', yanchor='bottom',
                                      y=-0.15))
        fig.update_traces(textposition='inside', textinfo='percent+label')
        st.plotly_chart(fig, use_container_width=True, key='sev_pie')

    # ── Top warning descriptions (horizontal bar, colored by severity)
    with col_bar:
        st.subheader('Top warning descriptions')
        top = (wdf.groupby(['description', 'severity'], as_index=False)
                  .size()
                  .rename(columns={'size': 'count'})
                  .sort_values('count', ascending=True)
                  .tail(10))
        # Truncate descriptions for display
        top['short'] = top['description'].str.slice(0, 70) + \
                       top['description'].str.len().gt(70).map(
                           lambda b: '…' if b else '')
        fig = px.bar(top, x='count', y='short', orientation='h',
                     text='count', color='severity',
                     color_discrete_map=sev_colors,
                     height=400)
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=10),
                          yaxis_title=None, xaxis_title='Count',
                          legend=dict(orientation='h', yanchor='bottom',
                                      y=1.02))
        fig.update_traces(textposition='outside')
        st.plotly_chart(fig, use_container_width=True, key='top_warn_bar')
else:
    st.success('🎉 No warnings in this model.')

st.divider()
st.caption(
    f"Powered by revit + streamlit + plotly · "
    f"Revit {snap['revit_version']} · "
    f"{kpis['warnings']} warnings · "
    f"{kpis['in_place_families']} in-place families · "
    f"data cached 2 min")

# Clear stale IPC error after successful render
if st.session_state.get('_ipc_error'):
    st.session_state['_ipc_error'] = None
