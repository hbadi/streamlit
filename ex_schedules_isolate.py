"""Streamlit side-car app — pilote Revit live via IPC.

Demo : 1 grille AG Grid avec 2 interactions indépendantes ramenées vers
Revit en temps réel :

    - Click sur une ligne (row selection)  → ISOLATE l'élément dans
      la vue active, les autres en ghost 10% opacity.
    - Cocher la checkbox d'une ligne       → SELECT l'élément dans
      la vue active.
    - Plus aucune sélection ni coche       → RESTORE la vue (clear
      isolate + clear ghost overrides).

Pré-requis :
    pip install streamlit streamlit-aggrid
    revit  (avec un Revit qui tourne en local + IPC actif)

Lancer :
    streamlit run examples/streamlit/ex_schedules_isolate.py

Pourquoi AG Grid et pas st.dataframe / st.data_editor :
    - st.dataframe expose `on_select` (selection) mais pas l'édition.
    - st.data_editor expose l'édition mais pas `on_select`.
    - AG Grid expose les DEUX events séparément (SELECTION_CHANGED +
      VALUE_CHANGED) sur le MÊME widget — c'est l'UX recherchée ici.

Architecture :
    Streamlit (browser)  ─stdio─▶  Python process  ─IPC ZMQ─▶  Revit.exe
                                   (st.cache_resource sur revit.connect
                                    pour réutiliser la connexion entre
                                    reruns)
"""
import streamlit as st
import revit
import pandas as pd
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode


# ────────────────────────────────────────────────────────────────────
# Connexion IPC — 1 seule par session Streamlit (sinon : handshake à
# chaque rerun = app inutilisable).
# ────────────────────────────────────────────────────────────────────
@st.cache_resource
def _connect():
    return revit.connect(name='streamlit-bim-isolate', ghost=True)
_connect()


# ────────────────────────────────────────────────────────────────────
# Server-side helpers — chaque @revit.batch = 1 round-trip IPC (~150ms).
# Bundle les ops dans une fonction pour minimiser les round-trips.
# ────────────────────────────────────────────────────────────────────
@revit.batch
def list_schedules():
    import revit as _r
    return [s.name for s in _r.activeDoc.schedules]


@revit.batch
def get_df(name):
    import revit as _r
    # id=True → colonne 'id' int64 en première position. Indispensable
    # pour pouvoir router les actions Revit depuis les rows Streamlit.
    return _r.activeDoc.schedules[name].to_df(id=True)


@revit.batch
def isolate(ids):
    """Ghost everything else at 10% opacity, garde ids visibles
    normalement. Utilise LinqList.isolate (handle transaction +
    sub-transaction si view modifiable + clear ghost cross-mode)."""
    import revit as _r
    _r.getElementsByIds(ids).isolate(opacity=10)
    return len(ids)


@revit.batch
def select(ids):
    """Native Revit selection in the active view."""
    import revit as _r
    _r.activeDoc.setSelectionInView(ids)
    return len(ids)


@revit.batch
def restore():
    """Reset Temporary Hide/Isolate + clear les ghost overrides
    surgicalement (les overrides hors-package ne sont pas touchées)."""
    import revit as _r
    _r.activeView.restore()


# ────────────────────────────────────────────────────────────────────
# UI
# ────────────────────────────────────────────────────────────────────
st.set_page_config(page_title='BIM Explorer', layout='wide')
st.title('🏢 BIM Explorer')
st.caption('Row click = isolate (ghost 10%) · Checkbox = select · '
           'No-op = restore view')

def _dedupe_with_label_map(df):
    """AG Grid needs unique column ids ; Revit schedules can dup
    display headings ('Material' for door + frame). We suffix
    duplicate ids with ' (2)' etc. internally, but preserve the
    ORIGINAL Revit label in a side map so AG Grid's headerName
    config can re-display the WYSIWYG label."""
    seen, new_cols, label_map = {}, [], {}
    for c in df.columns:
        if c in seen:
            seen[c] += 1
            unique_id = f"{c} ({seen[c]})"
            new_cols.append(unique_id)
            label_map[unique_id] = c
        else:
            seen[c] = 1
            new_cols.append(c)
            label_map[c] = c
    df.columns = new_cols
    return df, label_map


sched_name = st.selectbox('Schedule', list_schedules())
df, label_map = _dedupe_with_label_map(get_df(sched_name).copy())
df.insert(1, '☑', False)            # colonne checkbox éditable
label_map['☑'] = 'Sel.'

# AG Grid : seul widget qui expose row-selection + cell-edit
# indépendamment dans un même tableau.
gb = GridOptionsBuilder.from_dataframe(df)
gb.configure_selection('multiple', use_checkbox=False)
gb.configure_column('☑', editable=True,
                    cellEditor='agCheckboxCellEditor',
                    cellRenderer='agCheckboxCellRenderer',
                    headerName='Sel.', width=70)
gb.configure_column('id', editable=False, width=90, headerName='id')
for c in df.columns:
    if c not in ('☑', 'id'):
        gb.configure_column(c, editable=False,
                            headerName=label_map.get(c, c))
opts = gb.build()

resp = AgGrid(
    df,
    gridOptions=opts,
    update_mode=GridUpdateMode.SELECTION_CHANGED | GridUpdateMode.VALUE_CHANGED,
    height=460,
    fit_columns_on_grid_load=True,
    key='grid',
)

# Lecture des 2 signaux (API streamlit-aggrid v1.2+) :
#   resp.selected_rows : DataFrame des rows highlighted, ou None
#   resp.data          : DataFrame complète après éditions
sel = resp.selected_rows
edited = resp.data

isolate_ids = (sel['id'].astype(int).tolist()
               if sel is not None and not sel.empty else [])

if isinstance(edited, pd.DataFrame) and '☑' in edited.columns:
    select_ids = edited.loc[edited['☑'].astype(bool), 'id']\
                       .astype(int).tolist()
else:
    select_ids = []

# Routing — chaque branche = 1 IPC call. Le restore est appelé à
# chaque rerun où ni l'un ni l'autre n'est actif (acceptable en demo,
# pour optim : tracker un flag "déjà restored" dans st.session_state).
if not isolate_ids and not select_ids:
    restore()
    st.caption('→ view restored (no selection · no check)')
else:
    cols = st.columns(2)
    if isolate_ids:
        cols[0].success(f"🔍 Isolated {isolate(isolate_ids)} (ghost 10%)")
    if select_ids:
        cols[1].success(
            f"☑ Selected {select(select_ids)} in active view")
