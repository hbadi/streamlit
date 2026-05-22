"""ex_dashboard.py — Live BIM Dashboard from the open Revit document.

EFFET WHAOUHHHH : tu ouvres une URL dans ton browser, et tu vois en
temps réel l'état du modèle Revit ouvert :

    - KPIs en hero strip (levels / rooms / sheets / views / schedules)
    - Top catégories par count (cliquer une barre → isolate dans Revit)
    - Rooms par niveau (bar chart)
    - Floor area par niveau (treemap)
    - **Rooms drill-down** : treemap level → rooms, click level pour
      ouvrir les cartes des pièces (Area / Perimeter / Isolate par pièce)
    - Pie chart des catégories majeures
    - Schedule browser interactif (sélection ligne → isolate)
    - **Sidebar sticky** avec Restore view / Force refresh toujours
      visibles, même au scroll
    - Restore view = clear isolations + reset des sélections grille
      (évite que le clic suivant ré-isole les lignes laissées sélectionnées)

Pré-requis :
    pip install streamlit streamlit-aggrid plotly
    revit (avec Revit qui tourne + IPC actif)

Lancer :
    streamlit run examples/streamlit/ex_dashboard.py

Architecture :
    Browser ──HTTP──▶ Streamlit ──IPC ZMQ──▶ Revit.exe
                       │
                       └─ st.cache_resource : 1 connexion par session
                       └─ st.cache_data ttl=120s : évite de re-batcher
                          les snapshots lourds à chaque interaction
"""
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

import revit
from revit.remote._client import IpcError


# ────────────────────────────────────────────────────────────────────
# Config + connexion (cachées entre reruns)
# ────────────────────────────────────────────────────────────────────
st.set_page_config(page_title='BIM Dashboard',
                   page_icon='🏢', layout='wide',
                   initial_sidebar_state='expanded')


@st.cache_resource
def _connect():
    """Cached connection. Raises if Revit IPC is unreachable — caller
    must catch and show the disconnected UI."""
    return revit.connect(name='bim-dashboard', ghost=True)


def _safe_connect() -> tuple[bool, str | None]:
    """Returns (ok, error_msg). Never raises. Used to gate the rest
    of the dashboard."""
    try:
        _connect()
        return True, None
    except (IpcError, RuntimeError, ConnectionError, OSError) as e:
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _safe_call(fn, *args, fallback=None, **kwargs):
    """Wrap any @revit.batch call so an IPC drop doesn't crash the
    page. Returns *fallback* on failure and flags the session for
    the disconnected UI on the next rerun."""
    try:
        return fn(*args, **kwargs)
    except (IpcError, RuntimeError, ConnectionError, OSError) as e:
        st.session_state['_ipc_error'] = f"{type(e).__name__}: {e}"
        return fallback
    except Exception as e:
        st.session_state['_ipc_error'] = f"{type(e).__name__}: {e}"
        return fallback


# Session state initialisation
st.session_state.setdefault('_just_restored', False)
st.session_state.setdefault('_grid_key', 0)
st.session_state.setdefault('_drill_level', None)
st.session_state.setdefault('_ipc_error', None)


# ────────────────────────────────────────────────────────────────────
# Server-side fetchers — bundle pour minimiser les IPC calls
# ────────────────────────────────────────────────────────────────────
@revit.batch
def _fetch_overview_raw():
    """Snapshot complet en 1 round-trip IPC. cat.count() = raw .NET
    fast count (×11 vs len(.instances))."""
    import revit as _r
    doc = _r.activeDoc
    cats = list(doc.categories)

    cat_counts = sorted(
        ({'category': c.name, 'count': c.count()} for c in cats),
        key=lambda r: -r['count'])
    cat_counts = [r for r in cat_counts if r['count'] > 0]

    try:
        rooms = doc.categories['Rooms'].instances
        rooms_by_level = rooms.CountBy(
            lambda r: r.level.name or '<unassigned>').ToDict()
        floor_area_by_level = rooms.SumBy(
            lambda r: r.level.name or '<unassigned>',
            lambda r: r['Area'] or 0).ToDict()
    except Exception:
        rooms_by_level, floor_area_by_level = {}, {}

    return {
        'project':        doc.title,
        'n_levels':       len(doc.levels),
        'n_rooms':        sum(rooms_by_level.values()),
        'n_sheets':       len(doc.sheets),
        'n_views':        len(doc.views),
        'n_schedules':    len(doc.schedules),
        'n_phases':       len(doc.phases),
        'n_cats_active':  len(cat_counts),
        'cat_counts':     cat_counts,
        'rooms_by_level': rooms_by_level,
        'floor_area':     floor_area_by_level,
    }


@st.cache_data(ttl=120)
def fetch_overview():
    """Cached snapshot. Returns ``None`` on IPC failure (the disconnected
    UI then takes over). The None result is NOT cached — next call retries."""
    try:
        return _fetch_overview_raw()
    except (IpcError, RuntimeError, ConnectionError, OSError) as e:
        st.session_state['_ipc_error'] = f"{type(e).__name__}: {e}"
        return None


@revit.batch
def _fetch_rooms_raw():
    """Per-room detail : id / number / name / level / area / perimeter.
    Used by the rooms treemap + cards drill-down."""
    import revit as _r
    out = []
    try:
        for r in _r.activeDoc.categories['Rooms'].instances:
            out.append({
                'id':        int(r.id),
                'number':    r['Number'] or '',
                'name':      r['Name'] or '<unnamed>',
                'level':     r.level.name if r.level else '<unassigned>',
                'area':      round(r['Area'] or 0, 2),
                'perimeter': round(r['Perimeter'] or 0, 2),
            })
    except Exception:
        pass
    return out


@st.cache_data(ttl=120)
def fetch_rooms():
    """Cached. Returns ``[]`` on IPC failure (caller renders empty info)."""
    try:
        return _fetch_rooms_raw()
    except (IpcError, RuntimeError, ConnectionError, OSError) as e:
        st.session_state['_ipc_error'] = f"{type(e).__name__}: {e}"
        return []


@revit.batch
def list_schedules():
    import revit as _r
    return [s.name for s in _r.activeDoc.schedules]


@revit.batch
def get_schedule_df(name):
    import revit as _r
    return _r.activeDoc.schedules[name].to_df(id=True)


@revit.batch
def isolate_category(cat_name):
    import revit as _r
    cat = _r.activeDoc.categories[cat_name]
    cat.instances.isolate(opacity=10)
    return cat.count()


@revit.batch
def isolate_ids(ids):
    import revit as _r
    _r.getElementsByIds(ids).isolate(opacity=10)
    return len(ids)


@revit.batch
def restore():
    import revit as _r
    _r.activeView.restore()


def _trigger_restore():
    """Restore view + reset grid state + tag the next rerun as 'just
    restored' so the isolate branches don't immediately re-fire from
    leftover selections."""
    if _safe_call(restore) is None and st.session_state.get('_ipc_error'):
        return    # IPC down — error UI will take over on rerun
    st.session_state['_just_restored'] = True
    st.session_state['_grid_key'] += 1     # force AG Grid recreation
                                           # → wipe selection state
    st.toast('View restored', icon='✅')


def _force_refresh():
    fetch_overview.clear()
    fetch_rooms.clear()
    st.toast('Cache cleared, fetching fresh data…', icon='♻️')


def _reconnect():
    """Clear cached connection + data caches, retry on next rerun."""
    _connect.clear()
    fetch_overview.clear()
    fetch_rooms.clear()
    st.session_state['_ipc_error'] = None


def _reload_revit():
    """Reload the `revit` package — BOTH this Streamlit process AND the
    IPC server side. Pick up new wrappers / fixes deployed mid-session
    without restarting Streamlit or Revit. Doesn't restore class
    identity for pre-reload instances (refresh `doc = revit.activeDoc`
    after if you held a ref)."""
    try:
        revit.reload(remote=True)
        fetch_overview.clear()
        fetch_rooms.clear()
        st.session_state['_ipc_error'] = None
        st.toast('revit reloaded (both sides)', icon='♻️')
    except Exception as e:
        st.session_state['_ipc_error'] = f"reload failed: {e}"


# ────────────────────────────────────────────────────────────────────
# CONNECTION GATE — show disconnected UI if Revit is unreachable
# ────────────────────────────────────────────────────────────────────
_ok, _conn_err = _safe_connect()
if not _ok:
    st.session_state['_ipc_error'] = _conn_err

if st.session_state.get('_ipc_error') or not _ok:
    st.title('🏢 BIM Dashboard')
    st.error('⚠️ **Revit IPC not reachable.**')
    err = st.session_state.get('_ipc_error') or _conn_err
    if err:
        st.code(err, language=None)
    st.markdown(
        """
        **Vérifie** :
        - Revit est lancé et un document est ouvert
        - L'IPC RevitShell est actif (panel `RevitShell` →
          `Start IPC`, ou `ipcAutoStartWithRevit` dans `config.yaml`)
        - Un marker existe dans
          `%LOCALAPPDATA%\\RevitShell\\processes\\*.json`
        """)
    st.button('🔌 Reconnect', type='primary', on_click=_reconnect)
    st.stop()


# ────────────────────────────────────────────────────────────────────
# SIDEBAR — sticky actions (always visible, even when scrolled)
# ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header('🏢 Actions')
    st.button('🔄 Restore view', use_container_width=True,
              on_click=_trigger_restore)
    st.button('♻️ Force refresh', use_container_width=True,
              on_click=_force_refresh)
    st.button('🔌 Reconnect Revit', use_container_width=True,
              on_click=_reconnect,
              help='Use if Revit was restarted or IPC dropped')
    st.button('🔄 Reload revit code', use_container_width=True,
              on_click=_reload_revit,
              help='Pick up newly deployed wrappers/fixes — no need to '
                   'restart Streamlit nor Revit (works for new properties '
                   'on existing classes ; mixin/inheritance changes still '
                   'need Revit restart)')
    if st.session_state.get('_ipc_error'):
        st.warning(
            f"⚠️ Last IPC error :\n`{st.session_state['_ipc_error']}`")
    st.divider()
    st.caption(f"Refresh : {datetime.now():%H:%M:%S}")
    st.caption('Data cached 2 min · click ♻️ to invalidate')

# ────────────────────────────────────────────────────────────────────
# MAIN — header + KPIs
# ────────────────────────────────────────────────────────────────────
with st.spinner('📡 Scanning BIM model (cached 2 min)…'):
    overview = fetch_overview()

if overview is None:
    # IPC dropped during fetch — show banner + reconnect, stop rendering.
    st.title('🏢 BIM Dashboard')
    st.error('⚠️ Lost connection to Revit during data fetch.')
    err = st.session_state.get('_ipc_error')
    if err:
        st.code(err, language=None)
    st.button('🔌 Reconnect', type='primary', on_click=_reconnect)
    st.stop()

st.title(f"🏢 {overview['project']}")
st.caption('Live BIM Dashboard · click charts to interact with Revit')

k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric('Levels',      overview['n_levels'])
k2.metric('Rooms',       overview['n_rooms'])
k3.metric('Sheets',      overview['n_sheets'])
k4.metric('Views',       overview['n_views'])
k5.metric('Schedules',   overview['n_schedules'])
k6.metric('Active cats', overview['n_cats_active'])

st.divider()

# ── Row 1 — Categories bar (click → isolate) + Rooms by level ─────
col_a, col_b = st.columns(2)

with col_a:
    st.subheader('Top categories by count')
    st.caption('💡 Click a bar to isolate that category in Revit')
    cat_df = pd.DataFrame(overview['cat_counts']).head(15)
    fig = px.bar(cat_df, x='count', y='category',
                 orientation='h', text='count', height=440)
    fig.update_layout(
        yaxis={'categoryorder': 'total ascending'},
        margin=dict(l=0, r=0, t=10, b=10), showlegend=False)
    fig.update_traces(textposition='outside')
    sel = st.plotly_chart(fig, on_select='rerun', key='cat_chart',
                          use_container_width=True)
    if (not st.session_state['_just_restored']
            and sel and sel.selection and sel.selection.points):
        cat_name = sel.selection.points[0]['y']
        n = _safe_call(isolate_category, cat_name, fallback=0)
        if n:
            st.success(f"🔍 Isolated {n} × `{cat_name}` (ghost 10%)")

with col_b:
    st.subheader('Rooms by level')
    rb = overview['rooms_by_level']
    if rb:
        rb_df = (pd.DataFrame(list(rb.items()), columns=['level', 'rooms'])
                   .sort_values('rooms', ascending=False))
        fig = px.bar(rb_df, x='level', y='rooms', text='rooms', height=440,
                     color='rooms', color_continuous_scale='Blues')
        fig.update_layout(
            margin=dict(l=0, r=0, t=10, b=10),
            coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True, key='rb_chart')
    else:
        st.info('No rooms in this model.')

# ── Row 2 — Floor area treemap + Categories donut ─────────────────
col_c, col_d = st.columns(2)

with col_c:
    st.subheader('Floor area by level')
    fa = overview['floor_area']
    if fa:
        fa_df = pd.DataFrame(list(fa.items()), columns=['level', 'area_m2'])
        fa_df['area_m2'] = fa_df['area_m2'].round(1)
        fig = px.treemap(fa_df, path=['level'], values='area_m2',
                         color='area_m2', color_continuous_scale='Greens',
                         height=400)
        fig.update_traces(textinfo='label+value', texttemplate=
                          '<b>%{label}</b><br>%{value} m²')
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=10))
        st.plotly_chart(fig, use_container_width=True, key='fa_chart')
    else:
        st.info('No floor area data.')

with col_d:
    st.subheader('Top 10 categories — share of count')
    pie_df = cat_df.head(10)
    fig = px.pie(pie_df, names='category', values='count',
                 hole=0.5, height=400)
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=10),
                      legend=dict(orientation='v', x=1.02))
    st.plotly_chart(fig, use_container_width=True, key='pie_chart')

st.divider()

# ── Row 3 — Rooms treemap (drill-down by level) + cards view ──────
st.subheader('🏠 Rooms — drill-down by level')
st.caption('💡 Click a **level** to drill in and see the rooms · '
           'click a **room** to isolate it')

with st.spinner('Loading rooms…'):
    rooms = fetch_rooms()

if rooms:
    rooms_df = pd.DataFrame(rooms)
    levels = set(rooms_df['level'].unique())

    fig = px.treemap(
        rooms_df,
        path=[px.Constant('all rooms'), 'level', 'name'],
        values='area',
        color='area',
        color_continuous_scale='Greens',
        height=460,
        custom_data=['id', 'number', 'level'],
    )
    fig.update_traces(
        textinfo='label+value',
        texttemplate='<b>%{label}</b><br>%{value} m²',
        hovertemplate='<b>%{label}</b><br>Area: %{value} m²<extra></extra>')
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=10))
    sel_rt = st.plotly_chart(fig, on_select='rerun', key='room_tree',
                             use_container_width=True)

    if (not st.session_state['_just_restored']
            and sel_rt and sel_rt.selection and sel_rt.selection.points):
        pt = sel_rt.selection.points[0]
        label = pt.get('label', '')
        if label in levels:
            # Click sur un level → open drill-down
            st.session_state['_drill_level'] = label
        elif label and label != 'all rooms':
            # Click sur une room (leaf) → isolate this room
            match = rooms_df[rooms_df['name'] == label]
            if not match.empty:
                rid = int(match.iloc[0]['id'])
                if _safe_call(isolate_ids, [rid], fallback=None) is not None:
                    st.success(f"🔍 Isolated room `{label}`")

    # Cards view for the opened level
    if st.session_state['_drill_level']:
        lvl = st.session_state['_drill_level']
        col_t, col_back = st.columns([4, 1])
        col_t.markdown(f"#### Rooms in **{lvl}**")
        if col_back.button('← All levels'):
            st.session_state['_drill_level'] = None
            st.rerun()

        rooms_in_lvl = rooms_df[rooms_df['level'] == lvl] \
                          .sort_values('number')
        n_cols = 4
        cols = st.columns(n_cols)
        for i, room in enumerate(rooms_in_lvl.itertuples()):
            with cols[i % n_cols]:
                with st.container(border=True):
                    st.markdown(
                        f"**`{room.number}`** {room.name}")
                    mc1, mc2 = st.columns(2)
                    mc1.metric('Area',  f"{room.area:.1f} m²")
                    mc2.metric('Perim.', f"{room.perimeter:.1f} m")
                    if st.button('🔍 Isolate', key=f'iso_room_{room.id}',
                                 use_container_width=True):
                        if _safe_call(isolate_ids, [int(room.id)],
                                      fallback=None) is not None:
                            st.toast(f"Isolated room {room.name}",
                                     icon='🔍')
else:
    st.info('No rooms in this model.')

st.divider()

# ── Row 4 — Schedule browser : click row → isolate elements ───────
st.subheader('📋 Schedules — pick rows to isolate in Revit')
st.caption('💡 Click rows to isolate · use Restore view (sidebar) to '
           'clear isolation AND grid selection')
schedules = _safe_call(list_schedules, fallback=[])
if schedules:
    sn = st.selectbox('Schedule', schedules, key='sched_pick')
    sched_df = _safe_call(get_schedule_df, sn, fallback=pd.DataFrame())
    if sched_df is None or sched_df.empty:
        st.warning(f"Couldn't fetch schedule `{sn}`.")
        st.stop()
    sched_df = sched_df.copy()

    gb = GridOptionsBuilder.from_dataframe(sched_df)
    gb.configure_selection('multiple', use_checkbox=False)
    gb.configure_column('id', editable=False, width=90)
    for c in sched_df.columns:
        if c != 'id':
            gb.configure_column(c, editable=False)
    opts = gb.build()

    # Key rotates on Restore — recreates the widget = wipes selection.
    grid_key = f'sched_grid_{st.session_state["_grid_key"]}'
    resp = AgGrid(
        sched_df, gridOptions=opts,
        update_mode=GridUpdateMode.SELECTION_CHANGED,
        height=340, fit_columns_on_grid_load=True, key=grid_key)

    sel_rows = resp.selected_rows
    if (not st.session_state['_just_restored']
            and sel_rows is not None and not sel_rows.empty):
        eids = sel_rows['id'].astype(int).tolist()
        n = _safe_call(isolate_ids, eids, fallback=0)
        if n:
            st.success(f"🔍 Isolated {n} elements from `{sn}` (ghost 10%)")
else:
    st.info('No schedules in this model (or IPC dropped).')

# ────────────────────────────────────────────────────────────────────
# End-of-run :
#   - reset the "just restored" flag so the NEXT user interaction
#     triggers isolate again normally ;
#   - if we got here without st.stop(), the page rendered fine →
#     clear any stale IPC error from a previous failed call.
# ────────────────────────────────────────────────────────────────────
if st.session_state['_just_restored']:
    st.session_state['_just_restored'] = False
if st.session_state.get('_ipc_error'):
    st.session_state['_ipc_error'] = None

st.divider()
st.caption(
    f"Powered by revit + streamlit + plotly · "
    f"{overview['n_cats_active']} active categories · "
    f"{overview['n_schedules']} schedules · "
    f"{overview['n_rooms']} rooms · "
    f"data cached 2 min")
