"""
Chore Rotation App  (Streamlit + SQLite, mobile-first, single file)

Run:   pip install streamlit pandas
       streamlit run chores_app.py

Rotation logic
  * Weekly chores: assignee = PEOPLE[(base + weeks_since_ANCHOR) % n]  -> swaps every Monday.
  * Daily chores : set pattern chosen in "Log & Setup":
        "Alternate daily"  -> flips every day
        "Swap weekly"      -> flips every Monday
        "Fixed"            -> never flips
  * Override     : stored per chore PER PERIOD (that day / that week), so a trade never
                   leaks into future periods. "Auto" removes the override.
  * Completion   : one row per (chore, period) with who + timestamp. A new day/week has a
                   new period key, so everything resets with no cleanup job.
"""
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ----------------------------- CONFIG ---------------------------------------
DB_PATH = "chores.db"
PEOPLE = ["Noah", "Alexis"]
TZ = ZoneInfo("America/Chicago")
ANCHOR_MONDAY = date(2026, 1, 5)  # week 0 for rotation math (must be a Monday)
ZONES = ["Kitchen", "Bathroom", "Bedroom", "Living room", "Whole apartment"]
DAILY_MODES = ["Alternate daily", "Swap weekly", "Fixed"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# (name, kind, zone, minutes, base person index, suggested weekday or None)
SEED = [
    ("Put away clothes & bathroom items", "daily", "Bathroom", 5, 0, None),
    ("Make the bed", "daily", "Bedroom", 2, 1, None),
    ("Quick sink / vanity wipe", "daily", "Bathroom", 3, 1, None),
    ("Dishes & kitchen sink", "daily", "Kitchen", 15, 0, None),
    ("Wipe kitchen counters", "daily", "Kitchen", 5, 1, None),
    ("Tidy living room", "daily", "Living room", 10, 1, None),
    ("Prep essentials for tomorrow", "daily", "Whole apartment", 5, 0, None),
    ("Clean toilet, sink & mirror", "weekly", "Bathroom", 15, 0, 0),
    ("Dust living room", "weekly", "Living room", 10, 1, 0),
    ("Vacuum living room & bedroom", "weekly", "Whole apartment", 15, 0, 1),
    ("Clean stovetop & microwave", "weekly", "Kitchen", 15, 1, 1),
    ("Trash & recycling", "weekly", "Whole apartment", 10, 1, 2),
    ("Clean shower & surfaces", "weekly", "Bathroom", 15, 0, 2),
    ("Mop kitchen & bathroom floors", "weekly", "Whole apartment", 15, 0, 3),
    ("Change sheets & wash bedding", "weekly", "Bedroom", 15, 1, 3),
    ("Vacuum couch & tidy storage", "weekly", "Living room", 15, 0, 4),
    ("Wipe appliance fronts", "weekly", "Kitchen", 15, 1, 4),
    ("15-minute zone task", "weekly", "Whole apartment", 15, 1, 5),
]

# ----------------------------- DATABASE -------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS chores(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('daily','weekly')),
                zone TEXT NOT NULL, minutes INTEGER DEFAULT 10,
                base_idx INTEGER DEFAULT 0, day_hint INTEGER,
                active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS overrides(
                chore_id INTEGER NOT NULL, period_key TEXT NOT NULL, assignee TEXT NOT NULL,
                PRIMARY KEY(chore_id, period_key));
            CREATE TABLE IF NOT EXISTS completions(
                chore_id INTEGER NOT NULL, period_key TEXT NOT NULL,
                completed_by TEXT NOT NULL, completed_at TEXT NOT NULL,
                PRIMARY KEY(chore_id, period_key));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        if c.execute("SELECT COUNT(*) FROM chores").fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO chores(name,kind,zone,minutes,base_idx,day_hint) VALUES(?,?,?,?,?,?)", SEED
            )


def get_setting(key, default):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))


# ----------------------------- ROTATION ENGINE ------------------------------
def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def period_key(kind: str, d: date) -> str:
    return d.isoformat() if kind == "daily" else monday_of(d).isoformat()


def auto_assignee(chore, d: date, mode: str) -> str:
    weeks = (monday_of(d) - ANCHOR_MONDAY).days // 7
    if chore["kind"] == "weekly" or mode == "Swap weekly":
        shift = weeks
    elif mode == "Alternate daily":
        shift = d.toordinal()
    else:
        shift = 0
    return PEOPLE[(chore["base_idx"] + shift) % len(PEOPLE)]


def load_state(d: date):
    keys = {period_key("daily", d), period_key("weekly", d)}
    ph = ",".join("?" * len(keys))
    with db() as c:
        chores = c.execute(
            "SELECT * FROM chores WHERE active=1 ORDER BY zone, COALESCE(day_hint,9), id"
        ).fetchall()
        ov = {
            (r["chore_id"], r["period_key"]): r["assignee"]
            for r in c.execute(f"SELECT * FROM overrides WHERE period_key IN ({ph})", tuple(keys))
        }
        comp = {
            (r["chore_id"], r["period_key"]): r
            for r in c.execute(f"SELECT * FROM completions WHERE period_key IN ({ph})", tuple(keys))
        }
    return chores, ov, comp


# ----------------------------- CALLBACKS ------------------------------------
def cb_toggle(chore_id, pkey, wkey):
    done = st.session_state[wkey]
    with db() as c:
        if done:
            ts = datetime.now(TZ).isoformat(timespec="seconds")
            c.execute(
                "INSERT OR REPLACE INTO completions VALUES(?,?,?,?)",
                (chore_id, pkey, st.session_state.me, ts),
            )
        else:
            c.execute("DELETE FROM completions WHERE chore_id=? AND period_key=?", (chore_id, pkey))


def cb_override(chore_id, pkey, wkey):
    choice = st.session_state[wkey]
    with db() as c:
        if choice == "Auto":
            c.execute("DELETE FROM overrides WHERE chore_id=? AND period_key=?", (chore_id, pkey))
        else:
            c.execute("INSERT OR REPLACE INTO overrides VALUES(?,?,?)", (chore_id, pkey, choice))


# ----------------------------- UI HELPERS -----------------------------------
def fmt_ts(iso: str) -> str:
    dt = datetime.fromisoformat(iso).astimezone(TZ)
    return f"{dt:%a} {dt.hour % 12 or 12}:{dt:%M} {dt:%p}"


def who_is(ch, d, mode, ov):
    pkey = period_key(ch["kind"], d)
    return ov.get((ch["id"], pkey)) or auto_assignee(ch, d, mode)


def render_checklist(chores, d, mode, ov, comp, only_me):
    rows = [(ch, who_is(ch, d, mode, ov)) for ch in chores]
    if only_me:
        rows = [(ch, w) for ch, w in rows if w == st.session_state.me]
    if not rows:
        st.info("Nothing assigned here. 🎉")
        return
    done_n = sum((ch["id"], period_key(ch["kind"], d)) in comp for ch, _ in rows)
    st.progress(done_n / len(rows), text=f"{done_n}/{len(rows)} done")
    zone = None
    for ch, who in rows:
        if ch["zone"] != zone:
            zone = ch["zone"]
            st.markdown(f"##### {zone}")
        pkey = period_key(ch["kind"], d)
        rec = comp.get((ch["id"], pkey))
        hint = f" · {DAYS[ch['day_hint']]}" if ch["kind"] == "weekly" and ch["day_hint"] is not None else ""
        wkey = f"chk_{ch['id']}_{pkey}_{int(bool(rec))}"  # key tracks DB state -> refreshes across devices
        st.checkbox(
            f"**{ch['name']}** — {who} · {ch['minutes']}m{hint}",
            value=bool(rec),
            key=wkey,
            on_change=cb_toggle,
            args=(ch["id"], pkey, wkey),
        )
        if rec:
            st.caption(f"✅ {rec['completed_by']} · {fmt_ts(rec['completed_at'])}")


def render_assign(chores, d, mode, ov):
    for ch in chores:
        pkey = period_key(ch["kind"], d)
        cur = ov.get((ch["id"], pkey), "Auto")
        auto = auto_assignee(ch, d, mode)
        wkey = f"ov_{ch['id']}_{pkey}_{cur}"
        st.selectbox(
            f"{ch['name']}  ·  {ch['zone']}",
            ["Auto"] + PEOPLE,
            index=(["Auto"] + PEOPLE).index(cur),
            format_func=lambda o, a=auto: f"Auto ({a})" if o == "Auto" else f"{o} (override)",
            key=wkey,
            on_change=cb_override,
            args=(ch["id"], pkey, wkey),
        )


# ----------------------------- APP ------------------------------------------
st.set_page_config(page_title="Chores", page_icon="🧹", layout="centered", initial_sidebar_state="collapsed")
st.markdown(
    """
    <style>
    .block-container{padding:1rem .8rem 4rem;max-width:640px}
    header[data-testid="stHeader"]{height:0} footer{visibility:hidden}
    div[data-testid="stCheckbox"] label{padding:.5rem 0;font-size:1.02rem;align-items:flex-start}
    div[data-testid="stCheckbox"] input{transform:scale(1.35)}
    h5{margin:.9rem 0 .1rem}
    </style>
    """,
    unsafe_allow_html=True,
)
init_db()

if "me" not in st.session_state:
    q = st.query_params.get("me", PEOPLE[0])
    st.session_state.me = q if q in PEOPLE else PEOPLE[0]

st.markdown("### 🧹 Home Chores")
st.radio("I'm", PEOPLE, horizontal=True, key="me")
st.query_params["me"] = st.session_state.me  # bookmark ?me=Alexis on her phone

view_date = st.date_input("Date", value=datetime.now(TZ).date(), format="MM/DD/YYYY")
mode = get_setting("daily_mode", DAILY_MODES[0])
chores, ov, comp = load_state(view_date)
daily = [c for c in chores if c["kind"] == "daily"]
weekly = sorted([c for c in chores if c["kind"] == "weekly"], key=lambda c: (c["day_hint"] is None, c["day_hint"] or 0, c["id"]))

t_today, t_week, t_assign, t_log = st.tabs(["✅ Today", "📅 Week", "🔁 Assign", "📊 Log & Setup"])

with t_today:
    only_me = st.toggle("Only mine", value=True, key="only_me_daily")
    st.caption(f"Daily · {view_date:%A, %b %d} · resets every day")
    render_checklist(daily, view_date, mode, ov, comp, only_me)

with t_week:
    only_me_w = st.toggle("Only mine", value=True, key="only_me_weekly")
    wk = monday_of(view_date)
    st.caption(f"Week of {wk:%b %d} · swaps & resets every Monday")
    render_checklist(weekly, view_date, mode, ov, comp, only_me_w)

with t_assign:
    st.caption("Trade a chore for just this day/week. Choose Auto to undo.")
    kind = st.radio("Show", ["Daily", "Weekly"], horizontal=True, key="assign_kind")
    render_assign(daily if kind == "Daily" else weekly, view_date, mode, ov)

with t_log:
    wk_iso = monday_of(view_date).isoformat()
    with db() as c:
        stats = pd.read_sql_query(
            """SELECT c.completed_by AS Person, COUNT(*) AS Chores, SUM(ch.minutes) AS Minutes
               FROM completions c JOIN chores ch ON ch.id=c.chore_id
               WHERE substr(c.completed_at,1,10) >= ? GROUP BY c.completed_by""",
            c, params=(wk_iso,),
        )
        hist = pd.read_sql_query(
            """SELECT ch.name AS Chore, c.completed_by AS "Done by", c.completed_at AS "When", c.period_key AS Period
               FROM completions c JOIN chores ch ON ch.id=c.chore_id
               ORDER BY c.completed_at DESC LIMIT 50""",
            c,
        )
    st.markdown("##### This week")
    st.dataframe(stats, hide_index=True, use_container_width=True) if len(stats) else st.caption("No completions yet.")
    st.markdown("##### Recent activity")
    if len(hist):
        hist["When"] = hist["When"].map(fmt_ts)
        st.dataframe(hist, hide_index=True, use_container_width=True)

    st.markdown("##### Daily rotation pattern")
    new_mode = st.radio("Daily tasks", DAILY_MODES, index=DAILY_MODES.index(mode), label_visibility="collapsed")
    if new_mode != mode:
        set_setting("daily_mode", new_mode)
        st.rerun()
    st.caption("Weekly chores always swap every Monday.")

    with st.expander("➕ Add a chore"):
        with st.form("add_chore", clear_on_submit=True):
            name = st.text_input("Name")
            k = st.radio("Type", ["daily", "weekly"], horizontal=True)
            zone = st.selectbox("Zone", ZONES)
            mins = st.number_input("Minutes", 1, 120, 10)
            base = st.selectbox("Starts with", PEOPLE)
            day = st.selectbox("Suggested day (weekly)", ["—"] + DAYS)
            if st.form_submit_button("Add") and name.strip():
                with db() as c:
                    c.execute(
                        "INSERT INTO chores(name,kind,zone,minutes,base_idx,day_hint) VALUES(?,?,?,?,?,?)",
                        (name.strip(), k, zone, int(mins), PEOPLE.index(base), None if day == "—" else DAYS.index(day)),
                    )
                st.rerun()

    with st.expander("🗑️ Archive a chore"):
        opts = {f"{c['name']} ({c['kind']})": c["id"] for c in chores}
        pick = st.selectbox("Chore", list(opts), key="arch_pick") if opts else None
        if pick and st.button("Archive"):
            with db() as c:
                c.execute("UPDATE chores SET active=0 WHERE id=?", (opts[pick],))
            st.rerun()
