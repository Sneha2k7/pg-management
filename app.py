import re
from datetime import date, datetime
from decimal import Decimal
from html import escape as _esc

import mysql.connector
import pandas as pd
import streamlit as st

from zoneinfo import ZoneInfo

def now_ist():
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)

def today_ist():
    return now_ist().date()

st.set_page_config(page_title="Apartment & PG Management", page_icon=":material/apartment:", layout="wide")

try:
    DB_PASSWORD = st.secrets["db_password"]
except Exception:
    st.error('Database password not set. Create .streamlit/secrets.toml containing: db_password = "your mysql password"')
    st.stop()

DB = dict(
    host=st.secrets.get("db_host", "localhost"),
    port=int(st.secrets.get("db_port", 3306)),
    user=st.secrets.get("db_user", "root"),
    password=DB_PASSWORD,
    database=st.secrets.get("db_name", "project"),
)

# ======================= DATABASE HELPERS =======================
import threading

_LOCK = threading.Lock()

@st.cache_resource
def _raw_conn():
    return mysql.connector.connect(**DB, autocommit=True, connection_timeout=10)

def _use(work):
    # one shared connection; reconnect once if the cloud database dropped it
    for attempt in (1, 2):
        with _LOCK:
            try:
                return work(_raw_conn())
            except (mysql.connector.InterfaceError, mysql.connector.OperationalError):
                _raw_conn.clear()
                if attempt == 2:
                    raise

@st.cache_data(ttl=30, show_spinner=False)
def fetch(sql, params=()):
    def work(c):
        cur = c.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            return cur.fetchall(), list(cur.column_names)
        finally:
            cur.close()
    return _use(work)

def rows(sql, params=()):
    return fetch(sql, params)[0]

def one(sql):
    v = list(rows(sql)[0].values())[0]
    return float(v or 0)

def run_many(stmts):
    def work(c):
        cur = c.cursor()
        try:
            c.start_transaction()
            for s, p in stmts:
                cur.execute(s, p)
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            cur.close()
    try:
        _use(work)
    except mysql.connector.Error as e:
        if e.errno == 1062:
            msg = "That value already exists (duplicate email, PAN, ID proof, name, etc.)."
        elif e.errno == 1451:
            msg = "Can't delete this record because other records depend on it."
        elif e.errno == 1452:
            msg = "Invalid reference: the linked record doesn't exist."
        elif e.errno == 3819:
            msg = "A value breaks a table rule (check constraint), e.g. negative amount or check-out before check-in."
        elif e.errno == 1406:
            msg = "Text is too long for that field."
        else:
            msg = e.msg
        st.error(msg, icon=":material/error:")
        return False
    st.cache_data.clear()
    return True

def run(sql, params=()):
    return run_many([(sql, params)])

# ======================= TEXT + TABLE HELPERS =======================
SPECIAL = {"pg": "PG", "upi": "UPI", "wifi": "WiFi", "id": "ID", "pan": "PAN", "no": "No."}
MONEY = ("rent", "amount", "salary", "deposit", "charge", "collected")
NAME_COLS = {"full_name", "tenant", "owner", "staff_name", "visitor_name", "property_name",
             "amenity_name", "assigned_to", "logged_by", "visiting_tenant"}
TITLE_COLS = {"city", "address", "occupation", "category", "unit_type", "property_type",
              "role", "shift", "gender", "payment_mode", "bill_type"}
SENT_COLS = {"description", "purpose", "tenant_policy"}
UPPER_COLS = {"unit_number", "pan_number", "id_proof_number"}
STATUS_COLS = {"status", "payment_status", "agreement_status"}
LONG_COLS = {"description", "purpose", "tenant_policy", "address"}

def esc(s):
    return _esc(str(s)).replace("$", "&#36;")

def T(s):
    if s is None:
        return ""
    return " ".join(SPECIAL.get(w.lower(), w[:1].upper() + w[1:]) for w in str(s).split(" "))

def U(s):
    return "" if s is None else str(s).upper()

def S(s):
    s = str(s)
    return s[:1].upper() + s[1:]

def label(col):
    return " ".join(SPECIAL.get(w, w.capitalize()) for w in col.split("_"))

def inr(v):
    v = float(v)
    return f"₹{v:,.0f}" if v == int(v) else f"₹{v:,.2f}"

def short_inr(v):
    return f"₹{v / 1000:.0f}k" if v >= 1000 else f"₹{v:.0f}"

def ui(s):
    st.markdown(re.sub(r"\n\s*", "", s), unsafe_allow_html=True)

def chip(status):
    s = str(status or "").lower()
    cls = {"available": "ok", "paid": "ok", "resolved": "ok", "active": "ok", "yes": "ok",
           "pending": "warn", "open": "warn",
           "occupied": "info", "partial": "info", "in progress": "info"}.get(s, "mute")
    text = " ".join({"pg": "PG"}.get(w, w[:1].upper() + w[1:]) for w in s.split(" "))
    return f'<span class="chip {cls}">{esc(text)}</span>'

def fmt_cell(col, v):
    if v is None:
        return '<span class="na">—</span>'
    if col in STATUS_COLS:
        return chip(v)
    if col.startswith("is_"):
        return chip("yes" if v else "no")
    if isinstance(v, datetime):
        return v.strftime("%d %b %Y, %H:%M")
    if isinstance(v, date):
        return v.strftime("%b %Y") if col.endswith("_month") else v.strftime("%d %b %Y")
    if isinstance(v, (Decimal, float)):
        if any(k in col for k in MONEY):
            return inr(v)
        return str(int(v)) if v == int(v) else f"{float(v):,.2f}"
    if isinstance(v, int):
        return inr(v) if any(k in col for k in MONEY) else str(v)
    s = str(v)
    if col in UPPER_COLS:
        s = s.upper()
    elif col in NAME_COLS or col in TITLE_COLS:
        s = T(s)
    elif col in SENT_COLS:
        s = S(s)
    elif "email" in col:
        s = s.lower()
    return esc(s)

def render(data, cols):
    if not data:
        ui('<div class="empty">No records found.</div>')
        return
    head_html = "".join(f"<th>{esc(label(c))}</th>" for c in cols)
    body = ""
    for r in data:
        tds = ""
        for c in cols:
            cls = ' class="wrap"' if c in LONG_COLS else ""
            tds += f"<td{cls}>{fmt_cell(c, r[c])}</td>"
        body += f"<tr>{tds}</tr>"
    n = len(data)
    ui(f'<div class="tbl-wrap"><table class="t"><thead><tr>{head_html}</tr></thead><tbody>{body}</tbody></table></div>'
       f'<div class="tbl-foot">{n} record{"s" if n != 1 else ""}</div>')

def table(sql, params=()):
    data, cols = fetch(sql, params)
    render(data, cols)

def notify(msg):
    st.session_state["flash"] = msg

def head(title, sub):
    st.title(title)
    st.caption(sub)
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"), icon=":material/check_circle:")

def opts(data, key, fmt):
    return {r[key]: fmt(r) for r in data}

def txt(v):
    return "" if v is None else str(v)

def ok_phone(x):
    return x.isdigit() and len(x) == 10

def ok_email(x):
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", x) is not None

def ok_pan(x):
    return re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", x) is not None

def delete_button(tbl, id_col, rid, what):
    sure = st.checkbox(f"Yes, delete this {what} permanently", key=f"sure_{tbl}_{rid}")
    if st.button(f"Delete {what}", disabled=not sure, type="primary", key=f"del_{tbl}_{rid}"):
        if run(f"delete from {tbl} where {id_col}=%s", (rid,)):
            notify(f"{S(what)} deleted")
            st.rerun()

# ======================= DASHBOARD =======================
def _svg(inner):
    return ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{inner}</svg>')

I_BUILD = _svg('<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M9 22v-4h6v4M8 6h.01M12 6h.01M16 6h.01M8 10h.01M12 10h.01M16 10h.01M8 14h.01M12 14h.01M16 14h.01"/>')
I_TREND = _svg('<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>')
I_CARD = _svg('<rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/>')
I_CLIP = _svg('<path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 12h6M9 16h4"/>')
I_USERS = _svg('<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>')
I_ALERT = _svg('<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>')
I_LOGIN = _svg('<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>')
I_BOLT = _svg('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>')

def kpi(icon, bg, title, value, sub):
    return (f'<div class="card"><div class="kpi-top"><div class="ico" style="background:{bg}">{icon}</div>'
            f'<span>{title}</span></div><div class="kpi-val">{value}</div><div class="kpi-sub">{sub}</div></div>')

def mini(icon, bg, title, value):
    return (f'<div class="mini"><div class="ico sm" style="background:{bg}">{icon}</div>'
            f'<div><div class="mini-t">{title}</div><div class="mini-v">{value}</div></div></div>')

def initials(name):
    return esc("".join(p[0] for p in (name or "?").split()[:2]).upper())

def dashboard_page():
    hr = now_ist().hour
    hello = "Good morning" if hr < 12 else "Good afternoon" if hr < 17 else "Good evening"

    total = int(one("select count(*) from unit"))
    occ = int(one("select count(*) from unit where status='occupied'"))
    avail = int(one("select count(*) from unit where status='available'"))
    props = int(one("select count(*) from property"))
    tenants = int(one("select count(distinct tenant_id) from allocation where check_out_date is null"))
    collected = one("select coalesce(sum(amount_paid),0) from rent_payment")
    pending = one("select coalesce(sum(amount_due - amount_paid),0) from rent_payment")
    open_c = int(one("select count(*) from maintenance_request where status in ('open','in progress')"))
    in_prog = int(one("select count(*) from maintenance_request where status='in progress'"))
    inside = int(one("select count(*) from visitor_log where check_out_time is null"))
    util_due = one("select coalesce(sum(amount),0) from utility_bill where payment_status='pending'")
    pct = round(occ / total * 100) if total else 0

    bits = []
    if open_c:
        bits.append(f"{open_c} open complaint{'s' if open_c != 1 else ''}")
    if pending > 0:
        bits.append(f"{inr(pending)} rent to collect")
    headline = "You have " + " and ".join(bits) if bits else "Everything is running smoothly"
    ui(f'<div class="greet"><div class="hi">{hello}, {esc(T(st.session_state["user"]["name"]))}</div><div class="big">{headline}</div>'
       f'<div class="sub">{tenants} active tenant{"s" if tenants != 1 else ""} &nbsp;·&nbsp; '
       f'{avail} unit{"s" if avail != 1 else ""} available &nbsp;·&nbsp; '
       f'{props} propert{"y" if props == 1 else "ies"}</div></div>')

    ui('<div class="grid4">'
       + kpi(I_BUILD, "#DCE8DF", "Total Units", total, f"across {props} propert{'y' if props == 1 else 'ies'}")
       + kpi(I_TREND, "#EADFB8", "Occupancy", f"{pct}%", f"{occ} of {total} units occupied")
       + kpi(I_CARD, "#CFE0E4", "Rent Collected", inr(collected), f"{inr(pending)} still pending")
       + kpi(I_CLIP, "#DDDCF1", "Open Complaints", open_c, f"{in_prog} in progress")
       + '</div>')
    ui('<div class="grid4">'
       + mini(I_USERS, "#DCE8DF", "Active Tenants", tenants)
       + mini(I_ALERT, "#F1DAD3", "Rent Pending", inr(pending))
       + mini(I_LOGIN, "#CFE0E4", "Visitors Inside", inside)
       + mini(I_BOLT, "#EADFB8", "Utility Bills Due", inr(util_due))
       + '</div>')

    # rent collection chart (last 6 months)
    first = today_ist().replace(day=1)
    months = []
    for i in range(5, -1, -1):
        y, m = first.year, first.month - i
        while m < 1:
            m += 12
            y -= 1
        months.append(date(y, m, 1))
    data = {}
    for r in rows("select billing_month, sum(amount_due) as due, sum(amount_paid) as paid from rent_payment group by billing_month"):
        key = r["billing_month"].replace(day=1)
        d0, p0 = data.get(key, (0.0, 0.0))
        data[key] = (d0 + float(r["due"]), p0 + float(r["paid"]))
    mx = max([data.get(m, (0, 0))[0] for m in months] + [1])
    best = max(months, key=lambda m: data.get(m, (0, 0))[1])
    cols, labels = "", ""
    for m in months:
        due, paid = data.get(m, (0, 0))
        h = max(round(150 * due / mx), 8)
        fh = round(h * paid / due) if due else 0
        cls = "fill"
        tip = f"{m:%b %Y}: {inr(paid)} collected of {inr(due)} billed"
        val = short_inr(paid) if paid > 0 else ""
        cols += (f'<div class="bar-col" title="{tip}"><div class="bval">{val}</div>'
                 f'<div class="track" style="height:{h}px"><div class="{cls}" style="height:{fh}px"></div></div></div>')
        labels += f"<span>{m:%b}</span>"
    chart = ('<div class="card"><h4>Rent collection</h4><div class="cap">Collected vs billed, last 6 months</div>'
             f'<div class="bars">{cols}</div><div class="mlabels">{labels}</div>'
             '<div class="lgrow"><span><i class="dot" style="background:#2F5D46"></i>Collected</span>'
             '<span><i class="dot" style="background:#DCE8DF"></i>Billed</span></div></div>')

    other = max(total - occ - avail, 0)
    segs = [("Occupied", occ, "#9DC3A5"), ("Available", avail, "#E7D6AA"), ("Other", other, "#B8D3D8")]
    segs = [s for s in segs if s[1] > 0]
    stops, acc = [], 0.0
    for name, n, col in segs:
        end = acc + n / total * 100
        stops.append(f"{col} {acc:.2f}% {end:.2f}%")
        acc = end
    grad = ", ".join(stops) if stops else "#E5E8E4 0% 100%"
    legend = "".join(f'<div class="lg"><span><i class="dot" style="background:{col}"></i>{name}</span><span>{n}</span></div>'
                     for name, n, col in segs)
    donut = ('<div class="card"><h4>Occupancy</h4><div class="cap">Unit status</div><div class="donut-wrap">'
             f'<div class="donut" style="background:conic-gradient({grad})"><div class="donut-in"><b>{pct}%</b><small>occupied</small></div></div>'
             f'<div style="width:100%">{legend}</div></div></div>')
    ui(f'<div class="grid21">{chart}{donut}</div>')

    pay = rows("""select t.full_name as tenant, u.unit_number, rp.billing_month, rp.amount_due, rp.amount_paid, rp.payment_status
                  from rent_payment rp join allocation a on rp.allocation_id=a.allocation_id
                  join tenant t on a.tenant_id=t.tenant_id join unit u on a.unit_id=u.unit_id
                  order by rp.payment_id desc limit 4""")
    prow = ""
    for r in pay:
        prow += (f'<div class="row"><div class="avatar">{initials(r["tenant"])}</div>'
                 f'<div class="main"><div class="t">{esc(T(r["tenant"]))}</div>'
                 f'<div class="s">Unit {esc(U(r["unit_number"]))} · {r["billing_month"]:%b %Y}</div></div>'
                 f'<div style="text-align:right"><div class="amt">{inr(r["amount_paid"])} '
                 f'<span class="of">of {inr(r["amount_due"])}</span></div>{chip(r["payment_status"])}</div></div>')
    pcard = ('<div class="card"><h4>Recent rent payments</h4><div class="cap">Latest bills and what was paid</div>'
             + (prow or '<div class="cap">No payments yet.</div>') + '</div>')

    cm = rows("""select r.category, r.description, r.status, u.unit_number, s.staff_name
                 from maintenance_request r join unit u on r.unit_id=u.unit_id
                 left join staff s on r.staff_id=s.staff_id
                 where r.status in ('open','in progress') order by r.request_id desc limit 4""")
    crow = ""
    for r in cm:
        who = esc(T(r["staff_name"])) if r["staff_name"] else "Unassigned"
        crow += (f'<div class="row"><div class="avatar sand">{esc(r["category"][:1].upper())}</div>'
                 f'<div class="main"><div class="t">{esc(T(r["category"]))} · Unit {esc(U(r["unit_number"]))}</div>'
                 f'<div class="s">{esc(S(r["description"]))}</div></div>'
                 f'<div style="text-align:right">{chip(r["status"])}<div class="s" style="margin-top:4px">{who}</div></div></div>')
    ccard = ('<div class="card"><h4>Open complaints</h4><div class="cap">Waiting for action</div>'
             + (crow or '<div class="cap">No open complaints.</div>') + '</div>')
    ui(f'<div class="grid2">{pcard}{ccard}</div>')

    un = rows("""select u.unit_number, u.unit_type, u.status, u.monthly_rent, p.property_name
                 from unit u join property p on u.property_id=p.property_id
                 order by (u.status='available') desc, u.unit_id desc limit 4""")
    ucards = ""
    for r in un:
        cls = "free" if r["status"].lower() == "available" else "busy"
        ucards += (f'<div class="ucard"><div class="uimg {cls}"><span>{esc(U(r["unit_number"]))}</span></div>'
                   f'<div class="ubody"><div class="n">{esc(T(r["unit_type"]))} {esc(U(r["unit_number"]))}</div>'
                   f'<div class="p">{esc(T(r["property_name"]))}</div>'
                   f'<div class="r"><span>{inr(r["monthly_rent"])}<small>/month</small></span>{chip(r["status"])}</div></div></div>')
    if ucards:
        ui(f'<div class="sec">Units at a glance</div><div class="grid4">{ucards}</div>')

# ======================= OWNERS =======================
def owners_page():
    head("Owners", "Property owners and their contact details")
    t1, t2, t3 = st.tabs(["View", "Add owner", "Edit / Delete"])
    with t1:
        term = st.text_input("Search owners", placeholder="Name, city, phone or email", key="own_search")
        like = f"%{term}%"
        table("""select owner_id, full_name, phone, email, pan_number, city, address, registered_on
                 from owner where full_name like %s or city like %s or email like %s or phone like %s
                 order by owner_id""", (like,) * 4)
    with t2:
        with st.form("add_owner", clear_on_submit=True):
            c1, c2 = st.columns(2)
            name = c1.text_input("Full name")
            phone = c2.text_input("Phone (10 digits)")
            email = c1.text_input("Email")
            pan = c2.text_input("PAN number (e.g. ABCDE1234F)")
            city = c1.text_input("City")
            reg = c2.date_input("Registered on", value=today_ist())
            addr = st.text_input("Address")
            if st.form_submit_button("Add owner"):
                pan = pan.strip().upper()
                if not name.strip():
                    st.error("Name is required")
                elif not ok_phone(phone.strip()):
                    st.error("Phone must be exactly 10 digits")
                elif not ok_email(email.strip()):
                    st.error("Enter a valid email address")
                elif not ok_pan(pan):
                    st.error("PAN must look like ABCDE1234F")
                elif run("""insert into owner (full_name, phone, pan_number, registered_on, city, address, email)
                            values (%s,%s,%s,%s,%s,%s,%s)""",
                         (name.strip(), int(phone.strip()), pan, reg, city.strip() or None,
                          addr.strip() or None, email.strip())):
                    notify("Owner added"); st.rerun()
    with t3:
        o = opts(rows("select owner_id, full_name from owner order by owner_id"), "owner_id",
                 lambda r: f"{r['owner_id']} – {T(r['full_name'])}")
        if not o:
            st.info("No owners yet.")
        else:
            oid = st.selectbox("Select owner", list(o), format_func=o.get, key="own_sel")
            rec = rows("select * from owner where owner_id=%s", (oid,))[0]
            c1, c2 = st.columns(2)
            ph = c1.text_input("Phone", value=txt(rec["phone"]), key=f"own_ph_{oid}")
            em = c2.text_input("Email", value=txt(rec["email"]), key=f"own_em_{oid}")
            ci = c1.text_input("City", value=txt(rec["city"]), key=f"own_ci_{oid}")
            ad = c2.text_input("Address", value=txt(rec["address"]), key=f"own_ad_{oid}")
            if st.button("Save changes", key=f"own_save_{oid}"):
                if not ok_phone(ph.strip()):
                    st.error("Phone must be exactly 10 digits")
                elif not ok_email(em.strip()):
                    st.error("Enter a valid email address")
                elif run("update owner set phone=%s, email=%s, city=%s, address=%s where owner_id=%s",
                         (int(ph.strip()), em.strip(), ci.strip() or None, ad.strip() or None, oid)):
                    notify("Owner updated"); st.rerun()
            st.divider()
            delete_button("owner", "owner_id", oid, "owner")

# ======================= PROPERTIES =======================
def properties_page():
    head("Properties", "Apartments, PGs and hostels managed in the system")
    t1, t2, t3 = st.tabs(["View", "Add property", "Edit / Delete"])
    with t1:
        term = st.text_input("Search properties", placeholder="Property, city or owner", key="prop_search")
        like = f"%{term}%"
        table("""select p.property_id, p.property_name, p.property_type, o.full_name as owner,
                        p.city, p.pincode, p.address, p.tenant_policy
                 from property p join owner o on p.owner_id=o.owner_id
                 where p.property_name like %s or p.city like %s or o.full_name like %s
                 order by p.property_id""", (like,) * 3)
    with t2:
        own = opts(rows("select owner_id, full_name from owner order by full_name"), "owner_id",
                   lambda r: T(r["full_name"]))
        if not own:
            st.warning("Add an owner first.")
        else:
            with st.form("add_property", clear_on_submit=True):
                c1, c2 = st.columns(2)
                oid = c1.selectbox("Owner", list(own), format_func=own.get)
                ptype = c2.selectbox("Property type", ["apartment", "pg", "hostel"], format_func=T)
                name = c1.text_input("Property name")
                city = c2.text_input("City")
                pin = c1.text_input("Pincode (6 digits)")
                addr = c2.text_input("Address")
                policy = st.text_area("Tenant policy (optional)", placeholder="e.g. No smoking, visitors till 9 pm")
                if st.form_submit_button("Add property"):
                    if not name.strip() or not city.strip() or not addr.strip():
                        st.error("Property name, city and address are required")
                    elif not (pin.strip().isdigit() and len(pin.strip()) == 6):
                        st.error("Pincode must be exactly 6 digits")
                    elif run("""insert into property (owner_id, tenant_policy, property_name, property_type,
                                pincode, city, address) values (%s,%s,%s,%s,%s,%s,%s)""",
                             (oid, policy.strip() or None, name.strip(), ptype, int(pin.strip()),
                              city.strip(), addr.strip())):
                        notify("Property added"); st.rerun()
    with t3:
        pr = opts(rows("select property_id, property_name from property order by property_id"), "property_id",
                  lambda r: f"{r['property_id']} – {T(r['property_name'])}")
        if not pr:
            st.info("No properties yet.")
        else:
            pid = st.selectbox("Select property", list(pr), format_func=pr.get, key="prop_sel")
            rec = rows("select * from property where property_id=%s", (pid,))[0]
            c1, c2 = st.columns(2)
            nm = c1.text_input("Property name", value=txt(rec["property_name"]), key=f"pr_nm_{pid}")
            ad = c2.text_input("Address", value=txt(rec["address"]), key=f"pr_ad_{pid}")
            po = st.text_area("Tenant policy", value=txt(rec["tenant_policy"]), key=f"pr_po_{pid}")
            if st.button("Save changes", key=f"pr_save_{pid}"):
                if not nm.strip() or not ad.strip():
                    st.error("Property name and address are required")
                elif run("update property set property_name=%s, address=%s, tenant_policy=%s where property_id=%s",
                         (nm.strip(), ad.strip(), po.strip() or None, pid)):
                    notify("Property updated"); st.rerun()
            st.divider()
            delete_button("property", "property_id", pid, "property")

# ======================= UNITS =======================
UNIT_STATUS = ["available", "occupied"]

def units_page():
    head("Units", "Flats, rooms and bed spaces")
    t1, t2, t3 = st.tabs(["View", "Add unit", "Edit / Delete"])
    with t1:
        c1, c2 = st.columns([3, 1])
        term = c1.text_input("Search units", placeholder="Unit number, type or property", key="unit_search")
        stat = c2.selectbox("Status", ["All"] + UNIT_STATUS, key="unit_status", format_func=T)
        like = f"%{term}%"
        table("""select u.unit_id, p.property_name, u.unit_number, u.floor_no, u.unit_type,
                        u.capacity, u.status, u.monthly_rent
                 from unit u join property p on u.property_id=p.property_id
                 where (u.unit_number like %s or u.unit_type like %s or p.property_name like %s)
                   and (%s='All' or u.status=%s)
                 order by u.unit_id""", (like, like, like, stat, stat))
    with t2:
        props = opts(rows("select property_id, property_name from property order by property_name"),
                     "property_id", lambda r: T(r["property_name"]))
        if not props:
            st.warning("Add a property first.")
        else:
            with st.form("add_unit", clear_on_submit=True):
                c1, c2 = st.columns(2)
                pid = c1.selectbox("Property", list(props), format_func=props.get)
                num = c2.text_input("Unit number")
                floor = c1.number_input("Floor", min_value=0, max_value=50, value=1)
                typ = c2.selectbox("Type", ["flat", "room", "bed space"], format_func=T)
                cap = c1.number_input("Capacity", min_value=1, max_value=20, value=1)
                rent = c2.number_input("Monthly rent (₹)", min_value=0.0, step=500.0)
                if st.form_submit_button("Add unit"):
                    if not num.strip():
                        st.error("Unit number is required")
                    elif rent <= 0:
                        st.error("Monthly rent must be greater than 0")
                    elif rows("select 1 from unit where property_id=%s and unit_number=%s", (pid, num.strip())):
                        st.error("This property already has a unit with that number")
                    elif run("""insert into unit (property_id, unit_number, floor_no, unit_type, capacity, status, monthly_rent)
                                values (%s,%s,%s,%s,%s,'available',%s)""",
                             (pid, num.strip(), int(floor), typ, int(cap), rent)):
                        notify("Unit added"); st.rerun()
    with t3:
        un = opts(rows("""select u.unit_id, u.unit_number, p.property_name from unit u
                          join property p on u.property_id=p.property_id order by u.unit_id"""), "unit_id",
                  lambda r: f"{r['unit_id']} – {U(r['unit_number'])} ({T(r['property_name'])})")
        if not un:
            st.info("No units yet.")
        else:
            uid = st.selectbox("Select unit", list(un), format_func=un.get, key="unit_sel")
            rec = rows("select * from unit where unit_id=%s", (uid,))[0]
            cur_status = rec["status"].lower()
            c1, c2, c3 = st.columns(3)
            rent = c1.number_input("Monthly rent (₹)", min_value=0.0, value=float(rec["monthly_rent"]),
                                   step=500.0, key=f"u_rent_{uid}")
            cap = c2.number_input("Capacity", min_value=1, max_value=20, value=int(rec["capacity"]), key=f"u_cap_{uid}")
            status = c3.selectbox("Status", UNIT_STATUS,
                                  index=UNIT_STATUS.index(cur_status) if cur_status in UNIT_STATUS else 0,
                                  key=f"u_st_{uid}", format_func=T)
            st.caption("Status changes automatically when you allocate or check out a tenant.")
            if st.button("Save changes", key=f"u_save_{uid}"):
                if rent <= 0:
                    st.error("Monthly rent must be greater than 0")
                elif run("update unit set monthly_rent=%s, capacity=%s, status=%s where unit_id=%s",
                         (rent, int(cap), status, uid)):
                    notify("Unit updated"); st.rerun()
            st.divider()
            delete_button("unit", "unit_id", uid, "unit")

# ======================= AMENITIES =======================
def amenities_page():
    head("Amenities", "Amenity catalogue and the amenities installed in each unit")
    t1, t2, t3, t4 = st.tabs(["View", "Add amenity", "Assign to unit", "Edit / Remove"])
    with t1:
        st.subheader("Amenity catalogue")
        table("select amenity_id, amenity_name, category, monthly_charge, is_chargeable from amenity order by amenity_id")
        st.subheader("Installed in units")
        table("""select u.unit_number, p.property_name, am.amenity_name, am.category, ua.installed_on
                 from unit_amenity ua join unit u on ua.unit_id=u.unit_id
                 join property p on u.property_id=p.property_id
                 join amenity am on ua.amenity_id=am.amenity_id
                 order by u.unit_number, am.amenity_name""")
    with t2:
        with st.form("add_amenity", clear_on_submit=True):
            c1, c2 = st.columns(2)
            name = c1.text_input("Amenity name")
            cat = c2.selectbox("Category", ["internet", "service", "facility", "furnishing", "appliance", "safety"],
                               format_func=T)
            charge = c1.number_input("Monthly charge (₹)", min_value=0.0, step=50.0)
            chg = c2.checkbox("Chargeable")
            if st.form_submit_button("Add amenity"):
                if not name.strip():
                    st.error("Amenity name is required")
                elif chg and charge <= 0:
                    st.error("A chargeable amenity needs a charge greater than 0")
                elif run("insert into amenity (amenity_name, category, monthly_charge, is_chargeable) values (%s,%s,%s,%s)",
                         (name.strip(), cat, charge, int(chg))):
                    notify("Amenity added"); st.rerun()
    with t3:
        un = opts(rows("""select u.unit_id, u.unit_number, p.property_name from unit u
                          join property p on u.property_id=p.property_id order by u.unit_id"""), "unit_id",
                  lambda r: f"{U(r['unit_number'])} – {T(r['property_name'])}")
        am = opts(rows("select amenity_id, amenity_name from amenity order by amenity_name"), "amenity_id",
                  lambda r: T(r["amenity_name"]))
        if not un or not am:
            st.warning("You need at least one unit and one amenity.")
        else:
            with st.form("assign_amenity", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                uid = c1.selectbox("Unit", list(un), format_func=un.get)
                aid = c2.selectbox("Amenity", list(am), format_func=am.get)
                dt = c3.date_input("Installed on", value=today_ist())
                if st.form_submit_button("Assign amenity"):
                    if run("insert into unit_amenity (unit_id, amenity_id, installed_on) values (%s,%s,%s)",
                           (uid, aid, dt)):
                        notify("Amenity assigned to unit"); st.rerun()
    with t4:
        st.subheader("Edit amenity")
        data = rows("select * from amenity order by amenity_id")
        am = opts(data, "amenity_id", lambda r: T(r["amenity_name"]))
        if am:
            aid = st.selectbox("Amenity", list(am), format_func=am.get, key="am_sel")
            rec = next(r for r in data if r["amenity_id"] == aid)
            c1, c2 = st.columns(2)
            ch = c1.number_input("Monthly charge (₹)", min_value=0.0, value=float(rec["monthly_charge"]),
                                 step=50.0, key=f"am_ch_{aid}")
            chg = c2.checkbox("Chargeable", value=bool(rec["is_chargeable"]), key=f"am_c_{aid}")
            if st.button("Save changes", key=f"am_save_{aid}"):
                if chg and ch <= 0:
                    st.error("A chargeable amenity needs a charge greater than 0")
                elif run("update amenity set monthly_charge=%s, is_chargeable=%s where amenity_id=%s",
                         (ch, int(chg), aid)):
                    notify("Amenity updated"); st.rerun()
            delete_button("amenity", "amenity_id", aid, "amenity")
        st.divider()
        st.subheader("Remove an amenity from a unit")
        links = rows("""select ua.unit_id, ua.amenity_id, u.unit_number, am.amenity_name
                        from unit_amenity ua join unit u on ua.unit_id=u.unit_id
                        join amenity am on ua.amenity_id=am.amenity_id order by u.unit_number""")
        lk = {(r["unit_id"], r["amenity_id"]): f"{U(r['unit_number'])} – {T(r['amenity_name'])}" for r in links}
        if lk:
            pair = st.selectbox("Installed amenity", list(lk), format_func=lk.get, key="ua_sel")
            if st.button("Remove from unit", type="primary", key="ua_del"):
                if run("delete from unit_amenity where unit_id=%s and amenity_id=%s", pair):
                    notify("Amenity removed from unit"); st.rerun()
        else:
            st.info("No amenities are installed in any unit yet.")

# ======================= TENANTS =======================
def tenants_page():
    head("Tenants", "Tenant profiles and contact details")
    t1, t2, t3 = st.tabs(["View", "Add tenant", "Edit / Delete"])
    with t1:
        term = st.text_input("Search tenants", placeholder="Name, phone, email or occupation", key="ten_search")
        like = f"%{term}%"
        table("""select tenant_id, full_name, gender, phone, email, occupation, id_proof_number,
                        emergency_contact_phone
                 from tenant where full_name like %s or phone like %s or email like %s or occupation like %s
                 order by tenant_id""", (like,) * 4)
    with t2:
        with st.form("add_tenant", clear_on_submit=True):
            c1, c2 = st.columns(2)
            name = c1.text_input("Full name")
            gender = c2.selectbox("Gender", ["male", "female", "other"], format_func=T)
            phone = c1.text_input("Phone (10 digits)")
            email = c2.text_input("Email")
            occ = c1.text_input("Occupation")
            idp = c2.text_input("ID proof number")
            emer = c1.text_input("Emergency contact (10 digits)")
            if st.form_submit_button("Add tenant"):
                if not name.strip():
                    st.error("Name is required")
                elif not ok_phone(phone.strip()):
                    st.error("Phone must be exactly 10 digits")
                elif email.strip() and not ok_email(email.strip()):
                    st.error("Enter a valid email address")
                elif emer.strip() and not ok_phone(emer.strip()):
                    st.error("Emergency contact must be exactly 10 digits")
                elif run("""insert into tenant (full_name, gender, phone, email, occupation, id_proof_number,
                            emergency_contact_phone) values (%s,%s,%s,%s,%s,%s,%s)""",
                         (name.strip(), gender, int(phone.strip()), email.strip() or None, occ.strip() or None,
                          idp.strip() or None, int(emer.strip()) if emer.strip() else None)):
                    notify("Tenant added"); st.rerun()
    with t3:
        tn = opts(rows("select tenant_id, full_name from tenant order by tenant_id"), "tenant_id",
                  lambda r: f"{r['tenant_id']} – {T(r['full_name'])}")
        if not tn:
            st.info("No tenants yet.")
        else:
            tid = st.selectbox("Select tenant", list(tn), format_func=tn.get, key="ten_sel")
            rec = rows("select * from tenant where tenant_id=%s", (tid,))[0]
            c1, c2 = st.columns(2)
            ph = c1.text_input("Phone", value=txt(rec["phone"]), key=f"t_ph_{tid}")
            em = c2.text_input("Email", value=txt(rec["email"]), key=f"t_em_{tid}")
            oc = c1.text_input("Occupation", value=txt(rec["occupation"]), key=f"t_oc_{tid}")
            ec = c2.text_input("Emergency contact", value=txt(rec["emergency_contact_phone"]), key=f"t_ec_{tid}")
            if st.button("Save changes", key=f"t_save_{tid}"):
                if not ok_phone(ph.strip()):
                    st.error("Phone must be exactly 10 digits")
                elif em.strip() and not ok_email(em.strip()):
                    st.error("Enter a valid email address")
                elif ec.strip() and not ok_phone(ec.strip()):
                    st.error("Emergency contact must be exactly 10 digits")
                elif run("update tenant set phone=%s, email=%s, occupation=%s, emergency_contact_phone=%s where tenant_id=%s",
                         (int(ph.strip()), em.strip() or None, oc.strip() or None,
                          int(ec.strip()) if ec.strip() else None, tid)):
                    notify("Tenant updated"); st.rerun()
            st.divider()
            delete_button("tenant", "tenant_id", tid, "tenant")

# ======================= STAFF =======================
def staff_page():
    head("Staff", "Security, maintenance and management staff")
    t1, t2, t3 = st.tabs(["View", "Add staff", "Edit / Delete"])
    roles = ["security", "maintenance", "manager", "cleaner", "cook"]
    shifts = ["day", "night", "general"]
    with t1:
        term = st.text_input("Search staff", placeholder="Name, role or property", key="staff_search")
        like = f"%{term}%"
        table("""select s.staff_id, s.staff_name, s.role, s.shift, s.phone, s.salary, p.property_name, s.joining_date
                 from staff s join property p on s.property_id=p.property_id
                 where s.staff_name like %s or s.role like %s or p.property_name like %s
                 order by s.staff_id""", (like,) * 3)
    with t2:
        props = opts(rows("select property_id, property_name from property order by property_name"),
                     "property_id", lambda r: T(r["property_name"]))
        if not props:
            st.warning("Add a property first.")
        else:
            with st.form("add_staff", clear_on_submit=True):
                c1, c2 = st.columns(2)
                name = c1.text_input("Staff name")
                pid = c2.selectbox("Property", list(props), format_func=props.get)
                role = c1.selectbox("Role", roles, format_func=T)
                shift = c2.selectbox("Shift", shifts, format_func=T)
                phone = c1.text_input("Phone (10 digits)")
                salary = c2.number_input("Monthly salary (₹)", min_value=0.0, step=500.0)
                joined = c1.date_input("Joining date", value=today_ist())
                if st.form_submit_button("Add staff"):
                    if not name.strip():
                        st.error("Name is required")
                    elif not ok_phone(phone.strip()):
                        st.error("Phone must be exactly 10 digits")
                    elif salary <= 0:
                        st.error("Salary must be greater than 0")
                    elif run("""insert into staff (property_id, staff_name, role, phone, shift, salary, joining_date)
                                values (%s,%s,%s,%s,%s,%s,%s)""",
                             (pid, name.strip(), role, int(phone.strip()), shift, salary, joined)):
                        notify("Staff member added"); st.rerun()
    with t3:
        sf = opts(rows("select staff_id, staff_name, role from staff order by staff_id"), "staff_id",
                  lambda r: f"{r['staff_id']} – {T(r['staff_name'])} ({T(r['role'])})")
        if not sf:
            st.info("No staff yet.")
        else:
            sid = st.selectbox("Select staff", list(sf), format_func=sf.get, key="staff_sel")
            rec = rows("select * from staff where staff_id=%s", (sid,))[0]
            c1, c2 = st.columns(2)
            cur_role = rec["role"].lower()
            role = c1.selectbox("Role", roles, index=roles.index(cur_role) if cur_role in roles else 0,
                                key=f"sf_role_{sid}", format_func=T)
            cur_shift = (rec["shift"] or "day").lower()
            shift = c2.selectbox("Shift", shifts, index=shifts.index(cur_shift) if cur_shift in shifts else 0,
                                 key=f"sf_shift_{sid}", format_func=T)
            ph = c1.text_input("Phone", value=txt(rec["phone"]), key=f"sf_ph_{sid}")
            sal = c2.number_input("Monthly salary (₹)", min_value=0.0, value=float(rec["salary"] or 0),
                                  step=500.0, key=f"sf_sal_{sid}")
            if st.button("Save changes", key=f"sf_save_{sid}"):
                if not ok_phone(ph.strip()):
                    st.error("Phone must be exactly 10 digits")
                elif run("update staff set role=%s, shift=%s, phone=%s, salary=%s where staff_id=%s",
                         (role, shift, int(ph.strip()), sal, sid)):
                    notify("Staff updated"); st.rerun()
            st.divider()
            delete_button("staff", "staff_id", sid, "staff member")

# ======================= ALLOCATIONS =======================
def allocations_page():
    head("Allocations", "Assign tenants to units and check them out")
    t1, t2, t3 = st.tabs(["View", "New allocation", "Check-out"])
    with t1:
        flt = st.selectbox("Show", ["All", "Active", "Ended"], key="al_flt")
        table("""select a.allocation_id, t.full_name as tenant, u.unit_number, p.property_name,
                        a.check_in_date, a.check_out_date, a.monthly_rent, a.deposit_paid, a.agreement_status
                 from allocation a join tenant t on a.tenant_id=t.tenant_id
                 join unit u on a.unit_id=u.unit_id join property p on u.property_id=p.property_id
                 where (%s='All' or (%s='Active' and a.check_out_date is null)
                        or (%s='Ended' and a.check_out_date is not null))
                 order by a.allocation_id""", (flt, flt, flt))
    with t2:
        tn = opts(rows("select tenant_id, full_name from tenant order by full_name"), "tenant_id",
                  lambda r: T(r["full_name"]))
        free = rows("""select u.unit_id, u.unit_number, u.monthly_rent, p.property_name from unit u
                       join property p on u.property_id=p.property_id where u.status='available' order by u.unit_id""")
        un = opts(free, "unit_id", lambda r: f"{U(r['unit_number'])} – {T(r['property_name'])}")
        if not tn:
            st.warning("Add a tenant first.")
        elif not un:
            st.warning("No available units right now.")
        else:
            c1, c2 = st.columns(2)
            t = c1.selectbox("Tenant", list(tn), format_func=tn.get, key="al_t")
            u = c2.selectbox("Available unit", list(un), format_func=un.get, key="al_u")
            default_rent = float(next(r for r in free if r["unit_id"] == u)["monthly_rent"])
            c3, c4, c5 = st.columns(3)
            d = c3.date_input("Check-in date", value=today_ist(), key="al_d")
            rent = c4.number_input("Monthly rent (₹)", min_value=0.0, value=default_rent, step=500.0, key=f"al_r_{u}")
            dep = c5.number_input("Deposit (₹)", min_value=0.0, step=1000.0, key="al_dep")
            bill = st.checkbox("Also create the first rent bill", value=True, key="al_bill")
            if st.button("Allocate unit", key="al_go"):
                if rent <= 0:
                    st.error("Monthly rent must be greater than 0")
                else:
                    stmts = [("""insert into allocation (tenant_id, unit_id, check_in_date, monthly_rent, deposit_paid, agreement_status)
                                 values (%s,%s,%s,%s,%s,'active')""", (t, u, d, rent, dep))]
                    if bill:
                        stmts.append(("""insert into rent_payment (allocation_id, payment_status, billing_month, amount_due, amount_paid)
                                         values (last_insert_id(),'pending',%s,%s,0)""", (d.replace(day=1), rent)))
                    stmts.append(("update unit set status='occupied' where unit_id=%s", (u,)))
                    if run_many(stmts):
                        notify("Unit allocated"); st.rerun()
    with t3:
        act = rows("""select a.allocation_id, a.unit_id, a.check_in_date, t.full_name as tenant, u.unit_number
                      from allocation a join tenant t on a.tenant_id=t.tenant_id
                      join unit u on a.unit_id=u.unit_id where a.check_out_date is null order by a.allocation_id""")
        ao = opts(act, "allocation_id",
                  lambda r: f"#{r['allocation_id']} – {T(r['tenant'])} in {U(r['unit_number'])} (since {r['check_in_date']:%d %b %Y})")
        if not ao:
            st.info("No active allocations.")
        else:
            aid = st.selectbox("Active allocation", list(ao), format_func=ao.get, key="co_sel")
            rec = next(r for r in act if r["allocation_id"] == aid)
            out = st.date_input("Check-out date", value=today_ist(), key="co_date")
            if st.button("Check out tenant", key="co_btn"):
                if out < rec["check_in_date"]:
                    st.error("Check-out date can't be before the check-in date")
                elif run_many([("update allocation set check_out_date=%s, agreement_status='ended' where allocation_id=%s", (out, aid)),
                               ("update unit set status='available' where unit_id=%s", (rec["unit_id"],))]):
                    notify("Tenant checked out, the unit is available again"); st.rerun()

# ======================= RENT PAYMENTS =======================
def payments_page():
    head("Rent Payments", "Rent bills, payments received and balances")
    t1, t2, t3 = st.tabs(["View", "Record payment", "Generate bill"])
    with t1:
        flt = st.selectbox("Status", ["All", "pending", "partial", "paid"], key="rp_flt", format_func=T)
        table("""select rp.payment_id, t.full_name as tenant, u.unit_number, rp.billing_month, rp.amount_due,
                        rp.amount_paid, rp.payment_status, rp.payment_mode, rp.payment_date
                 from rent_payment rp join allocation a on rp.allocation_id=a.allocation_id
                 join tenant t on a.tenant_id=t.tenant_id join unit u on a.unit_id=u.unit_id
                 where (%s='All' or rp.payment_status=%s) order by rp.payment_id""", (flt, flt))
    with t2:
        due = rows("""select rp.payment_id, rp.amount_due, rp.amount_paid, rp.billing_month,
                             t.full_name as tenant, u.unit_number
                      from rent_payment rp join allocation a on rp.allocation_id=a.allocation_id
                      join tenant t on a.tenant_id=t.tenant_id join unit u on a.unit_id=u.unit_id
                      where rp.amount_paid < rp.amount_due order by rp.payment_id""")
        po = opts(due, "payment_id",
                  lambda r: f"#{r['payment_id']} – {T(r['tenant'])} ({U(r['unit_number'])}) – {r['billing_month']:%b %Y} – balance {inr(r['amount_due'] - r['amount_paid'])}")
        if not po:
            st.success("No pending rent. Everything is paid.", icon=":material/check_circle:")
        else:
            pid = st.selectbox("Pending bill", list(po), format_func=po.get, key="rp_sel")
            rec = next(r for r in due if r["payment_id"] == pid)
            balance = rec["amount_due"] - rec["amount_paid"]
            c1, c2, c3 = st.columns(3)
            amt = c1.number_input("Amount received (₹)", min_value=0.0, value=float(balance), step=500.0, key=f"rp_amt_{pid}")
            mode = c2.selectbox("Payment mode", ["upi", "cash", "card", "bank transfer"], key="rp_mode", format_func=T)
            pdt = c3.date_input("Payment date", value=today_ist(), key="rp_date")
            if st.button("Record payment", key="rp_go"):
                amt_d = Decimal(str(amt))
                if amt_d <= 0:
                    st.error("Amount must be greater than 0")
                elif amt_d > balance:
                    st.error(f"Amount can't be more than the balance ({inr(balance)})")
                else:
                    new_paid = rec["amount_paid"] + amt_d
                    status = "paid" if new_paid >= rec["amount_due"] else "partial"
                    if run("update rent_payment set amount_paid=%s, payment_status=%s, payment_mode=%s, payment_date=%s where payment_id=%s",
                           (new_paid, status, mode, pdt, pid)):
                        notify("Payment recorded"); st.rerun()
    with t3:
        act = rows("""select a.allocation_id, a.monthly_rent, t.full_name as tenant, u.unit_number
                      from allocation a join tenant t on a.tenant_id=t.tenant_id
                      join unit u on a.unit_id=u.unit_id where a.check_out_date is null order by a.allocation_id""")
        ao = opts(act, "allocation_id", lambda r: f"#{r['allocation_id']} – {T(r['tenant'])} ({U(r['unit_number'])})")
        if not ao:
            st.info("No active allocations to bill.")
        else:
            aid = st.selectbox("Allocation", list(ao), format_func=ao.get, key="gb_sel")
            rec = next(r for r in act if r["allocation_id"] == aid)
            c1, c2 = st.columns(2)
            month = c1.date_input("Billing month", value=today_ist().replace(day=1), key="gb_month")
            due_amt = c2.number_input("Amount due (₹)", min_value=0.0, value=float(rec["monthly_rent"]),
                                      step=500.0, key=f"gb_amt_{aid}")
            if st.button("Generate bill", key="gb_go"):
                first = month.replace(day=1)
                if due_amt <= 0:
                    st.error("Amount due must be greater than 0")
                elif rows("select 1 from rent_payment where allocation_id=%s and billing_month=%s", (aid, first)):
                    st.error("A bill for this allocation and month already exists")
                elif run("""insert into rent_payment (allocation_id, payment_status, billing_month, amount_due, amount_paid)
                            values (%s,'pending',%s,%s,0)""", (aid, first, due_amt)):
                    notify("Rent bill generated"); st.rerun()

# ======================= UTILITY BILLS =======================
def utility_page():
    head("Utility Bills", "Electricity, water, gas and other bills per unit")
    t1, t2, t3 = st.tabs(["View", "Add bill", "Mark paid / Delete"])
    with t1:
        flt = st.selectbox("Status", ["All", "pending", "paid"], key="ub_flt", format_func=T)
        table("""select b.bill_id, p.property_name, u.unit_number, b.bill_type, b.amount, b.units_consumed,
                        b.billing_month, b.due_date, b.payment_status
                 from utility_bill b join unit u on b.unit_id=u.unit_id
                 join property p on u.property_id=p.property_id
                 where (%s='All' or b.payment_status=%s) order by b.bill_id""", (flt, flt))
    with t2:
        un = opts(rows("""select u.unit_id, u.unit_number, p.property_name from unit u
                          join property p on u.property_id=p.property_id order by u.unit_id"""), "unit_id",
                  lambda r: f"{U(r['unit_number'])} – {T(r['property_name'])}")
        if not un:
            st.warning("Add a unit first.")
        else:
            with st.form("add_bill", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                uid = c1.selectbox("Unit", list(un), format_func=un.get)
                typ = c2.selectbox("Bill type", ["electricity", "water", "gas", "internet", "maintenance"], format_func=T)
                amt = c3.number_input("Amount (₹)", min_value=0.0, step=50.0)
                cons = c1.number_input("Units consumed", min_value=0.0, step=1.0)
                bm = c2.date_input("Billing month", value=today_ist().replace(day=1))
                due = c3.date_input("Due date", value=today_ist())
                if st.form_submit_button("Add bill"):
                    if amt <= 0:
                        st.error("Amount must be greater than 0")
                    elif due < bm:
                        st.error("Due date can't be before the billing month")
                    elif run("""insert into utility_bill (unit_id, bill_type, amount, units_consumed, billing_month, due_date, payment_status)
                                values (%s,%s,%s,%s,%s,%s,'pending')""", (uid, typ, amt, cons, bm, due)):
                        notify("Utility bill added"); st.rerun()
    with t3:
        data = rows("""select b.bill_id, b.bill_type, b.amount, b.payment_status, u.unit_number
                       from utility_bill b join unit u on b.unit_id=u.unit_id order by b.bill_id""")
        bo = opts(data, "bill_id",
                  lambda r: f"#{r['bill_id']} – {T(r['bill_type'])} – {U(r['unit_number'])} – {inr(r['amount'])} ({T(r['payment_status'])})")
        if not bo:
            st.info("No utility bills yet.")
        else:
            bid = st.selectbox("Select bill", list(bo), format_func=bo.get, key="ub_sel")
            rec = next(r for r in data if r["bill_id"] == bid)
            if rec["payment_status"].lower() == "paid":
                st.success("This bill is already paid.", icon=":material/check_circle:")
            elif st.button("Mark as paid", key=f"ub_paid_{bid}"):
                if run("update utility_bill set payment_status='paid' where bill_id=%s", (bid,)):
                    notify("Bill marked as paid"); st.rerun()
            st.divider()
            delete_button("utility_bill", "bill_id", bid, "bill")

# ======================= COMPLAINTS =======================
REQ_STATUS = ["open", "in progress", "resolved"]

def complaints_page():
    head("Complaints & Maintenance", "Tenant complaints, staff assignment and resolution")
    t1, t2, t3 = st.tabs(["View", "Raise complaint", "Assign / Update"])
    with t1:
        flt = st.selectbox("Status", ["All"] + REQ_STATUS, key="cm_flt", format_func=T)
        table("""select r.request_id, t.full_name as tenant, u.unit_number, r.category, r.description,
                        r.status, r.raised_on, s.staff_name as assigned_to
                 from maintenance_request r join tenant t on r.tenant_id=t.tenant_id
                 join unit u on r.unit_id=u.unit_id left join staff s on r.staff_id=s.staff_id
                 where (%s='All' or r.status=%s) order by r.request_id""", (flt, flt))
    with t2:
        tn = opts(rows("select tenant_id, full_name from tenant order by full_name"), "tenant_id",
                  lambda r: T(r["full_name"]))
        if not tn:
            st.warning("Add a tenant first.")
        else:
            tid = st.selectbox("Tenant", list(tn), format_func=tn.get, key="cm_tenant")
            mine = opts(rows("""select u.unit_id, u.unit_number from allocation a join unit u on a.unit_id=u.unit_id
                                where a.tenant_id=%s and a.check_out_date is null""", (tid,)),
                        "unit_id", lambda r: U(r["unit_number"]))
            if not mine:
                st.warning("This tenant has no active allocation, so there is no unit to raise a complaint for.")
            else:
                with st.form("add_complaint", clear_on_submit=True):
                    c1, c2, c3 = st.columns(3)
                    uid = c1.selectbox("Unit", list(mine), format_func=mine.get)
                    cat = c2.selectbox("Category", ["plumbing", "electrical", "carpentry", "cleaning", "internet", "other"],
                                       format_func=T)
                    dt = c3.date_input("Raised on", value=today_ist())
                    desc = st.text_area("Describe the problem")
                    if st.form_submit_button("Raise complaint"):
                        if len(desc.strip()) < 5:
                            st.error("Please describe the problem (at least 5 characters)")
                        elif len(desc.strip()) > 255:
                            st.error("Description must be 255 characters or fewer")
                        elif run("""insert into maintenance_request (tenant_id, unit_id, status, raised_on, description, category)
                                    values (%s,%s,'open',%s,%s,%s)""", (tid, uid, dt, desc.strip(), cat)):
                            notify("Complaint raised"); st.rerun()
    with t3:
        data = rows("""select r.request_id, r.staff_id, r.status, r.category, t.full_name as tenant, u.unit_number
                       from maintenance_request r join tenant t on r.tenant_id=t.tenant_id
                       join unit u on r.unit_id=u.unit_id order by r.request_id""")
        ro = opts(data, "request_id",
                  lambda r: f"#{r['request_id']} – {T(r['category'])} – {T(r['tenant'])} ({U(r['unit_number'])}) – {T(r['status'])}")
        if not ro:
            st.info("No complaints yet.")
        else:
            rid = st.selectbox("Select complaint", list(ro), format_func=ro.get, key="cm_sel")
            rec = next(r for r in data if r["request_id"] == rid)
            so = {None: "Unassigned"}
            so.update(opts(rows("select staff_id, staff_name, role from staff order by staff_name"), "staff_id",
                           lambda r: f"{T(r['staff_name'])} ({T(r['role'])})"))
            cur = rec["status"].lower()
            c1, c2 = st.columns(2)
            sid = c1.selectbox("Assign to staff", list(so), format_func=so.get,
                               index=list(so).index(rec["staff_id"]) if rec["staff_id"] in so else 0,
                               key=f"cm_staff_{rid}")
            stat = c2.selectbox("Status", REQ_STATUS, index=REQ_STATUS.index(cur) if cur in REQ_STATUS else 0,
                                key=f"cm_stat_{rid}", format_func=T)
            if st.button("Save changes", key=f"cm_save_{rid}"):
                if run("update maintenance_request set staff_id=%s, status=%s where request_id=%s", (sid, stat, rid)):
                    notify("Complaint updated"); st.rerun()

# ======================= VISITORS =======================
def visitors_page():
    head("Visitors", "Visitor log with check-in and check-out")
    t1, t2, t3 = st.tabs(["View", "Log visitor", "Check-out"])
    with t1:
        flt = st.selectbox("Show", ["All", "Currently inside", "Left"], key="vs_flt")
        table("""select v.visitor_id, v.visitor_name, v.visitor_phone, t.full_name as visiting_tenant,
                        v.purpose, v.check_in_time, v.check_out_time, s.staff_name as logged_by
                 from visitor_log v join tenant t on v.tenant_id=t.tenant_id
                 left join staff s on v.staff_id=s.staff_id
                 where (%s='All' or (%s='Currently inside' and v.check_out_time is null)
                        or (%s='Left' and v.check_out_time is not null))
                 order by v.visitor_id""", (flt, flt, flt))
    with t2:
        tn = opts(rows("select tenant_id, full_name from tenant order by full_name"), "tenant_id",
                  lambda r: T(r["full_name"]))
        if not tn:
            st.warning("Add a tenant first.")
        else:
            so = {None: "Not recorded"}
            so.update(opts(rows("select staff_id, staff_name, role from staff order by staff_name"), "staff_id",
                           lambda r: f"{T(r['staff_name'])} ({T(r['role'])})"))
            with st.form("add_visitor", clear_on_submit=True):
                c1, c2 = st.columns(2)
                vname = c1.text_input("Visitor name")
                vphone = c2.text_input("Visitor phone (optional, 10 digits)")
                tid = c1.selectbox("Visiting tenant", list(tn), format_func=tn.get)
                sid = c2.selectbox("Logged by (staff)", list(so), format_func=so.get)
                purpose = st.text_input("Purpose of visit")
                c3, c4 = st.columns(2)
                vd = c3.date_input("Check-in date", value=today_ist())
                vt = c4.time_input("Check-in time", value=now_ist().time().replace(second=0, microsecond=0))
                if st.form_submit_button("Log visitor"):
                    if not vname.strip():
                        st.error("Visitor name is required")
                    elif vphone.strip() and not ok_phone(vphone.strip()):
                        st.error("Visitor phone must be exactly 10 digits")
                    elif run("""insert into visitor_log (tenant_id, staff_id, visitor_name, visitor_phone, purpose, check_in_time)
                                values (%s,%s,%s,%s,%s,%s)""",
                             (tid, sid, vname.strip(), int(vphone.strip()) if vphone.strip() else None,
                              purpose.strip() or None, datetime.combine(vd, vt))):
                        notify("Visitor logged"); st.rerun()
    with t3:
        inside = rows("""select v.visitor_id, v.visitor_name, v.check_in_time, t.full_name as tenant
                         from visitor_log v join tenant t on v.tenant_id=t.tenant_id
                         where v.check_out_time is null order by v.visitor_id""")
        vo = opts(inside, "visitor_id",
                  lambda r: f"#{r['visitor_id']} – {T(r['visitor_name'])} visiting {T(r['tenant'])} (in at {r['check_in_time']:%d %b, %H:%M})")
        if not vo:
            st.info("No visitors are currently inside.")
        else:
            vid = st.selectbox("Visitor inside", list(vo), format_func=vo.get, key="vs_sel")
            rec = next(r for r in inside if r["visitor_id"] == vid)
            if st.button("Check out now", key="vs_out"):
                now = now_ist()
                if now < rec["check_in_time"]:
                    st.error("Check-out can't be before the check-in time")
                elif run("update visitor_log set check_out_time=%s where visitor_id=%s", (now, vid)):
                    notify("Visitor checked out"); st.rerun()

# ======================= REPORTS =======================
REPORTS = {
    "Units above average rent (subquery)": "select * from unit where monthly_rent > (select avg(monthly_rent) from unit)",
    "Units per property (GROUP BY / HAVING)": "select property_id, count(*) as total_units from unit group by property_id having count(*) > 1",
    "Rent statistics (aggregate functions)": "select sum(monthly_rent) as total_rent, avg(monthly_rent) as average_rent, min(monthly_rent) as minimum_rent, max(monthly_rent) as maximum_rent from unit",
    "Tenant-unit view (VIEW)": "select * from tenant_unit_view",
    "Pending rent (JOIN)": """select t.full_name, rp.amount_due, rp.amount_paid from tenant t
        join allocation a on t.tenant_id=a.tenant_id
        join rent_payment rp on a.allocation_id=rp.allocation_id
        where rp.payment_status='pending'""",
    "Tenants per property (JOIN + GROUP BY)": """select p.property_name, count(distinct a.tenant_id) as total_tenants
        from property p join unit u on p.property_id=u.property_id
        join allocation a on u.unit_id=a.unit_id group by p.property_id, p.property_name""",
    "Open maintenance requests (WHERE)": "select request_id, tenant_id, unit_id, description, raised_on from maintenance_request where status='open'",
    "Unit amenities (JOIN + ORDER BY)": """select u.unit_number, am.amenity_name from unit u
        join unit_amenity ua on u.unit_id=ua.unit_id
        join amenity am on ua.amenity_id=am.amenity_id order by u.unit_number""",
    "Rent collected per property": """select p.property_name, coalesce(sum(rp.amount_paid),0) as rent_collected
        from property p left join unit u on u.property_id=p.property_id
        left join allocation a on a.unit_id=u.unit_id
        left join rent_payment rp on rp.allocation_id=a.allocation_id
        group by p.property_id, p.property_name""",
    "Occupancy per property": """select p.property_name, count(*) as total_units, sum(u.status='occupied') as occupied_units
        from unit u join property p on u.property_id=p.property_id group by p.property_id, p.property_name""",
    "Staff per property": """select p.property_name, s.role, count(*) as staff_count
        from staff s join property p on s.property_id=p.property_id group by p.property_id, p.property_name, s.role""",
    "Unpaid utility bills": """select u.unit_number, b.bill_type, b.amount, b.due_date from utility_bill b
        join unit u on b.unit_id=u.unit_id where b.payment_status='pending'""",
    "Complaints by category": "select category, count(*) as total_requests from maintenance_request group by category",
    "Visits per tenant": """select t.full_name, count(*) as total_visits from visitor_log v
        join tenant t on v.tenant_id=t.tenant_id group by t.tenant_id, t.full_name""",
}

def reports_page():
    head("Reports", "SQL reports: the Review 2 queries plus extra management reports")
    choice = st.selectbox("Choose a report", list(REPORTS))
    st.code(REPORTS[choice], language="sql")
    data, cols = fetch(REPORTS[choice])
    render(data, cols)
    if data:
        csv = pd.DataFrame(data, columns=cols).to_csv(index=False)
        st.download_button("Download CSV", csv, file_name="report.csv", mime="text/csv")

# ======================= STYLE =======================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
.stApp, h1, h2, h3, h4, p, label, button, input, textarea, td, th { font-family: 'Plus Jakarta Sans', 'Segoe UI', sans-serif; }
.stApp { background: #ECEAE4; }
   [data-testid="stHeader"] { background: linear-gradient(90deg, #ECEAE4 calc(100% - 420px), transparent calc(100% - 420px)) !important; box-shadow: 0 1px 0 #DDE0D9; z-index: 999; }
   [data-testid="stStatusWidget"] { display: none !important; }
   div[data-stale="true"] { opacity: 1 !important; transition: none !important; }
   .stApp[data-teststate="running"] [data-testid="stHeader"]::after { content: ""; position: absolute; left: 0; bottom: 0; height: 3px; width: 100%; background: linear-gradient(90deg, #2F5D46, #2F5D46) no-repeat; background-size: 35% 100%; animation: loadbar 1.1s ease-in-out infinite; }
   [data-testid="stToolbarActions"], [data-testid="stMainMenu"], [data-testid="stAppDeployButton"], [data-testid="stStatusWidget"] { display: none !important; }
   .st-key-userbar { position: fixed; top: 0; right: 0; height: 60px; min-width: 420px; justify-content: flex-end; padding: 0 28px; background: #ECEAE4; box-shadow: 0 1px 0 #DDE0D9; z-index: 1000001 !important; flex-direction: row !important; align-items: center; gap: 14px !important; width: auto !important; }
   .st-key-userbar > div { width: auto !important; }
   .st-key-userbar .userbar { white-space: nowrap; }
   .st-key-userbar [data-testid="stBaseButton-secondary"] { padding: .25rem 1.2rem; min-height: 2.2rem; }
   @media (max-width: 900px) { .st-key-userbar { position: static; height: auto; min-width: 0; padding: 0; box-shadow: none; } [data-testid="stHeader"] { background: #ECEAE4 !important; } }
   [data-testid="stHeader"] { background: #ECEAE4 !important; }
   .block-container { padding-top: 4.6rem !important; }
   .st-key-userbar { position: static !important; height: auto !important; min-width: 0 !important; width: 100% !important; padding: 0 !important; background: transparent !important; box-shadow: none !important; justify-content: flex-end !important; margin: 0 0 8px !important; }
   @keyframes loadbar { from { background-position: -40% 0; } to { background-position: 140% 0; } }
[data-testid="stAppDeployButton"], .stDeployButton, #MainMenu, footer { display: none !important; }
.block-container { max-width: 1280px; padding-top: 2.4rem; padding-bottom: 4rem; }
h1, h2, h3 { color: #1D2B24 !important; font-weight: 800 !important; letter-spacing: -0.02em; }
h1 { font-size: 2.1rem !important; padding-bottom: 0 !important; }
h3 { font-size: 1.15rem !important; margin-top: .6rem !important; }
[data-testid="stCaptionContainer"] p { color: #6B7A72; font-size: .95rem; }
[data-testid="stWidgetLabel"] p { font-weight: 600; color: #3B4A42; font-size: .88rem; }
hr { border-color: #DDE0D9 !important; }

/* tabs as pills */
div[data-baseweb="tab-list"] { gap: 6px; background: #fff; padding: 5px; border-radius: 999px; width: fit-content; box-shadow: 0 2px 10px rgba(29,43,36,.06); margin-bottom: 14px; }
button[data-baseweb="tab"] { border-radius: 999px; padding: 6px 20px; height: auto; background: transparent; }
button[data-baseweb="tab"] p { font-weight: 700; color: #4B5A52; font-size: .9rem; }
button[data-baseweb="tab"][aria-selected="true"] { background: #1D2B24; }
button[data-baseweb="tab"][aria-selected="true"] p { color: #fff !important; }
div[data-baseweb="tab-highlight"], div[data-baseweb="tab-border"] { display: none; }

/* forms and inputs */
[data-testid="stForm"] { background: #fff; border: none; border-radius: 22px; padding: 1.6rem; box-shadow: 0 6px 22px rgba(29,43,36,.07); }
[data-baseweb="input"], [data-baseweb="select"] > div, [data-baseweb="textarea"] { border-radius: 12px !important; border-color: #DADFD8 !important; background: #fff !important; }
[data-testid="stAlert"] { border-radius: 14px; }
[data-testid="stCode"] { border-radius: 14px; }

/* buttons */
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"], [data-testid="stDownloadButton"] button { background: #1D2B24; border: none; border-radius: 999px; padding: .45rem 1.7rem; }
[data-testid="stBaseButton-secondary"]:hover, [data-testid="stBaseButton-secondaryFormSubmit"]:hover, [data-testid="stDownloadButton"] button:hover { background: #2F5D46; }
[data-testid="stBaseButton-secondary"]:disabled { background: #C9D1CC; }
[data-testid="stBaseButton-secondary"] p, [data-testid="stBaseButton-secondaryFormSubmit"] p, [data-testid="stDownloadButton"] button p { color: #fff !important; font-weight: 700; }
[data-testid="stBaseButton-primary"] { background: #B5533C; border: none; border-radius: 999px; padding: .45rem 1.7rem; }
[data-testid="stBaseButton-primary"]:hover { background: #92402D; }
[data-testid="stBaseButton-primary"]:disabled { background: #E2C9C2; }
[data-testid="stBaseButton-primary"] p { color: #fff !important; font-weight: 700; }

/* tables */
.tbl-wrap { background: #fff; border-radius: 22px; box-shadow: 0 6px 22px rgba(29,43,36,.07); overflow: auto; max-height: 560px; }
table.t { width: 100%; border-collapse: separate; border-spacing: 0; font-size: .9rem; }
table.t th { position: sticky; top: 0; z-index: 1; background: #F6F5F0; text-align: left; padding: 14px 20px; font-size: .8rem; font-weight: 700; color: #6B7A72; border-bottom: 1px solid #E6E8E2; white-space: nowrap; }
table.t td { padding: 14px 20px; border-bottom: 1px solid #F0F1EC; color: #1D2B24; text-align: left; vertical-align: middle; white-space: nowrap; font-weight: 500; font-variant-numeric: tabular-nums; }
table.t td.wrap { white-space: normal; min-width: 220px; max-width: 340px; }
table.t tbody tr:last-child td { border-bottom: none; }
table.t tbody tr:hover td { background: #F8FAF7; }
.na { color: #A5AFA9; }
.tbl-foot { color: #6B7A72; font-size: .82rem; font-weight: 600; margin: 8px 6px 18px; }
.empty { background: #fff; border-radius: 22px; padding: 36px; text-align: center; color: #6B7A72; font-weight: 600; box-shadow: 0 6px 22px rgba(29,43,36,.07); }

/* chips */
.chip { display: inline-block; padding: 4px 12px; border-radius: 999px; font-size: .75rem; font-weight: 800; }
.chip.ok { background: #DDEBDF; color: #2F5D46; }
.chip.warn { background: #F4E8C5; color: #8A6D1F; }
.chip.info { background: #DCE8EB; color: #3C6470; }
.chip.mute { background: #ECEEEA; color: #5C6A62; }

/* dashboard */
.greet .hi { color: #6B7A72; font-weight: 600; }
.greet .big { font-size: 2.6rem; font-weight: 800; line-height: 1.1; color: #1D2B24; letter-spacing: -0.03em; margin: 4px 0 8px; max-width: 800px; }
.greet .sub { color: #6B7A72; margin-bottom: 24px; font-weight: 500; }
.grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; margin-bottom: 18px; }
.grid21 { display: grid; grid-template-columns: 2fr 1fr; gap: 18px; margin-bottom: 18px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }
.card { background: #fff; border-radius: 24px; padding: 22px 24px; box-shadow: 0 6px 22px rgba(29,43,36,.07); }
.card h4 { margin: 0 0 2px; font-size: 1.1rem; font-weight: 800; color: #1D2B24; }
.card .cap { color: #6B7A72; font-size: .85rem; margin-bottom: 16px; }
.kpi-top { display: flex; align-items: center; gap: 12px; color: #3B4A42; font-weight: 700; }
.ico { width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #2F5D46; flex-shrink: 0; }
.ico.sm { width: 38px; height: 38px; }
.kpi-val { font-size: 2.3rem; font-weight: 800; color: #1D2B24; margin-top: 12px; letter-spacing: -.02em; }
.kpi-sub { color: #6B7A72; font-size: .85rem; font-weight: 500; }
.mini { background: #fff; border-radius: 20px; padding: 14px 18px; display: flex; align-items: center; gap: 14px; box-shadow: 0 6px 22px rgba(29,43,36,.07); }
.mini-t { color: #6B7A72; font-size: .8rem; font-weight: 600; }
.mini-v { color: #1D2B24; font-size: 1.25rem; font-weight: 800; }
.bars { display: flex; align-items: flex-end; justify-content: space-between; height: 205px; gap: 10px; border-bottom: 1px dashed #D5DBD6; padding-bottom: 4px; }
.bar-col { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; gap: 6px; }
.bval { font-size: .75rem; font-weight: 700; color: #2F5D46; min-height: 14px; }
.track { width: 46px; border-radius: 14px; background: #DCE8DF; display: flex; align-items: flex-end; overflow: hidden; }
.fill { width: 100%; background: #2F5D46; border-radius: 14px; }
.fill.top { background: repeating-linear-gradient(135deg, #2F5D46 0 6px, #3F7459 6px 12px); }
.mlabels { display: flex; justify-content: space-between; gap: 10px; margin-top: 8px; color: #6B7A72; font-weight: 600; font-size: .85rem; }
.mlabels span { flex: 1; text-align: center; }
.lgrow { display: flex; gap: 18px; margin-top: 12px; color: #3B4A42; font-weight: 600; font-size: .85rem; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 8px; }
.donut-wrap { display: flex; flex-direction: column; align-items: center; gap: 16px; }
.donut { width: 176px; height: 176px; border-radius: 50%; display: flex; align-items: center; justify-content: center; }
.donut-in { width: 118px; height: 118px; border-radius: 50%; background: #fff; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.donut-in b { font-size: 1.7rem; font-weight: 800; color: #1D2B24; }
.donut-in small { color: #6B7A72; font-weight: 600; }
.lg { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 3px 0; color: #3B4A42; font-weight: 600; font-size: .9rem; }
.row { display: flex; align-items: center; gap: 14px; padding: 13px 0; border-bottom: 1px solid #EEF0EC; }
.row:last-child { border-bottom: none; }
.avatar { width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 800; color: #2F5D46; background: #DCE8DF; flex-shrink: 0; }
.avatar.sand { background: #F4E8C5; color: #8A6D1F; }
.row .main { flex: 1; min-width: 0; }
.row .t { font-weight: 700; color: #1D2B24; }
.row .s { color: #6B7A72; font-size: .82rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row .amt { font-weight: 800; color: #2F5D46; }
.row .of { color: #6B7A72; font-weight: 600; font-size: .8rem; }
.sec { font-size: 1.15rem; font-weight: 800; color: #1D2B24; margin: 10px 0 12px; }
.ucard { background: #fff; border-radius: 24px; overflow: hidden; box-shadow: 0 6px 22px rgba(29,43,36,.07); }
.uimg { height: 104px; display: flex; align-items: center; justify-content: center; }
.uimg span { font-size: 2rem; font-weight: 800; letter-spacing: .04em; color: rgba(29,43,36,.55); }
.uimg.free { background: repeating-linear-gradient(135deg, rgba(47,93,70,.07) 0 8px, transparent 8px 16px), #E3EEE5; }
.uimg.busy { background: repeating-linear-gradient(135deg, rgba(60,100,112,.08) 0 8px, transparent 8px 16px), #E1EAED; }
.ubody { padding: 16px 18px 18px; }
.ubody .n { font-weight: 800; font-size: 1.05rem; color: #1D2B24; }
.ubody .p { color: #6B7A72; font-size: .85rem; }
.ubody .r { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; font-weight: 800; color: #1D2B24; }
.ubody .r small { color: #6B7A72; font-weight: 600; margin-left: 2px; }
@media (max-width: 900px) {
  .grid4 { grid-template-columns: repeat(2, 1fr); }
  .grid21, .grid2 { grid-template-columns: 1fr; }
}
</style>
""", unsafe_allow_html=True)

# ======================= NAVIGATION =======================
import hashlib
import hmac
import secrets as pysecrets

ROLES = ["admin", "manager", "staff"]

# ======================= ACCOUNTS =======================
@st.cache_resource
def ensure_user_table():
    def work(c):
        cur = c.cursor()
        try:
            cur.execute("""create table if not exists app_user (
                             user_id int auto_increment primary key,
                             username varchar(30) not null unique,
                             full_name varchar(100) not null,
                             password_hash varchar(200) not null,
                             role varchar(20) not null check (role in ('admin','manager','staff')),
                             created_on datetime default current_timestamp)""")
        finally:
            cur.close()
    _use(work)
    return True

def hash_pw(pw, salt=None):
    salt = salt or pysecrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"

def check_pw(pw, stored):
    salt = stored.split("$")[0]
    return hmac.compare_digest(hash_pw(pw, salt), stored)

def create_user(full_name, username, pw, pw2, role):
    username = username.strip().lower()
    if not full_name.strip():
        st.error("Full name is required")
    elif not re.fullmatch(r"[a-z0-9_.]{3,30}", username):
        st.error("Username must be 3 to 30 characters: letters, numbers, dot or underscore")
    elif len(pw) < 6:
        st.error("Password must be at least 6 characters")
    elif pw != pw2:
        st.error("Passwords do not match")
    elif rows("select 1 from app_user where username=%s", (username,)):
        st.error("That username is already taken")
    else:
        return run("insert into app_user (username, full_name, password_hash, role) values (%s,%s,%s,%s)",
                   (username, full_name.strip(), hash_pw(pw), role))
    return False

# ======================= USERS PAGE (admin) =======================
def users_page():
    head("Users", "Accounts that can sign in to this system")
    me = st.session_state["user"]
    t1, t2, t3 = st.tabs(["View", "Add user", "Edit / Remove"])
    with t1:
        table("select user_id, username, full_name, role, created_on from app_user order by user_id")
    with t2:
        with st.form("add_user", clear_on_submit=True):
            c1, c2 = st.columns(2)
            name = c1.text_input("Full name")
            uname = c2.text_input("Username")
            p1 = c1.text_input("Password", type="password")
            p2 = c2.text_input("Confirm password", type="password")
            role = st.selectbox("Role", ROLES, index=2, format_func=S)
            if st.form_submit_button("Create user"):
                if create_user(name, uname, p1, p2, role):
                    notify("User created"); st.rerun()
    with t3:
        data = rows("select user_id, username, full_name, role from app_user order by user_id")
        uo = opts(data, "user_id", lambda r: f"{r['username']} – {T(r['full_name'])} ({S(r['role'])})")
        uid = st.selectbox("Select user", list(uo), format_func=uo.get, key="usr_sel")
        rec = next(r for r in data if r["user_id"] == uid)
        if uid == me["id"]:
            st.info("This is your own account, so it can't be changed or removed here.")
        else:
            newrole = st.selectbox("Role", ROLES, index=ROLES.index(rec["role"]), format_func=S,
                                   key=f"usr_role_{uid}")
            if st.button("Save role", key=f"usr_save_{uid}"):
                if run("update app_user set role=%s where user_id=%s", (newrole, uid)):
                    notify("Role updated"); st.rerun()
            st.divider()
            delete_button("app_user", "user_id", uid, "user")

# ======================= LOGIN PAGE =======================
AUTH_ICON = ('<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="2" width="16" height="20" rx="2"/>'
             '<path d="M9 22v-4h6v4M8 6h.01M12 6h.01M16 6h.01M8 10h.01M12 10h.01M16 10h.01M8 14h.01M12 14h.01M16 14h.01"/></svg>')

AUTH_CSS = """
<style>
.stApp { background: radial-gradient(circle at 12% 8%, #DCE8DF 0, rgba(220,232,223,0) 38%),
                     radial-gradient(circle at 92% 92%, #EADFB8 0, rgba(234,223,184,0) 36%), #ECEAE4; }
[data-testid="stHeader"] { display: none; }
.block-container { max-width: 520px !important; margin: 0 auto !important; padding-top: 7vh !important;
  animation: authRise .6s ease both; }
@keyframes authRise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
.auth-mark { width: 60px; height: 60px; border-radius: 20px; background: #1D2B24; color: #fff;
  display: flex; align-items: center; justify-content: center; margin: 0 auto 16px;
  box-shadow: 0 10px 26px rgba(29,43,36,.25); }
.auth-app { text-align: center; color: #6B7A72; font-weight: 700; letter-spacing: .06em; font-size: .78rem; text-transform: uppercase; }
.auth-title { text-align: center; font-size: 2rem; font-weight: 800; color: #1D2B24; letter-spacing: -.02em; margin: 4px 0; }
.auth-sub { text-align: center; color: #6B7A72; font-weight: 500; margin-bottom: 22px; }
div[data-baseweb="tab-list"] { margin-left: auto; margin-right: auto; }
[data-testid="stForm"] { padding: 1.8rem !important; }
button[data-testid="stBaseButton-secondaryFormSubmit"] { width: 100%; }
</style>
"""

def login_gate():
    if st.session_state.get("user"):
        return
    ensure_user_table()
    st.markdown(AUTH_CSS, unsafe_allow_html=True)
    ui(f'<div class="auth-mark">{AUTH_ICON}</div><div class="auth-app">Apartment &amp; PG Management</div>'
       '<div class="auth-title">Welcome back</div>'
       '<div class="auth-sub">Sign in or create an account to continue</div>')
    t1, t2 = st.tabs(["Sign in", "Create account"])
    with t1:
        if not rows("select 1 from app_user limit 1"):
            st.info("No accounts exist yet. Use the Create account tab to make the first admin.")
        with st.form("signin"):
            role_sel = st.segmented_control("Sign in as", ROLES, default="admin", format_func=S, key="si_role")
            uname = st.text_input("Username")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in"):
                rec = rows("select * from app_user where username=%s", (uname.strip().lower(),))
                if not rec or not check_pw(pw, rec[0]["password_hash"]):
                    st.error("Incorrect username or password")
                elif role_sel != rec[0]["role"]:
                    st.error(f"This is a {S(rec[0]['role'])} account. Select {S(rec[0]['role'])} above.")
                else:
                    r = rec[0]
                    st.session_state["user"] = {"id": r["user_id"], "username": r["username"],
                                                "name": r["full_name"], "role": r["role"]}
                    st.session_state["welcome"] = True
                    st.rerun()
    with t2:
        with st.form("signup"):
            name = st.text_input("Full name")
            uname = st.text_input("Username")
            c1, c2 = st.columns(2)
            p1 = c1.text_input("Password", type="password")
            p2 = c2.text_input("Confirm password", type="password")
            role = st.segmented_control("Account type", ROLES, default="staff", format_func=S, key="su_role")
            code = st.text_input("Access code", type="password",
                                 help="Admin accounts need the admin code. Other accounts need the sign-up code.")
            if st.form_submit_button("Create account"):
                need = st.secrets.get("admin_code" if role == "admin" else "signup_code")
                if not role:
                    st.error("Choose an account type")
                elif not need:
                    st.error("Account creation is not set up. Add signup_code and admin_code to the secrets.")
                elif not hmac.compare_digest(code.encode(), str(need).encode()):
                    st.error("Incorrect access code")
                elif create_user(name, uname, p1, p2, role):
                    st.success("Account created. Open the Sign in tab to continue.")
    st.stop()

login_gate()

# ======================= WELCOME TRANSITION =======================
WELCOME_CSS = """
<style>
.welcome { position: fixed; inset: 0; z-index: 99999; background: #ECEAE4; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 12px; pointer-events: none; animation: welcomeOut 2.4s ease forwards; }
.welcome .wm { width: 64px; height: 64px; border-radius: 22px; background: #1D2B24; color: #fff; display: flex;
  align-items: center; justify-content: center; animation: welcomePop .7s ease both; }
.welcome .wh { font-size: 2rem; font-weight: 800; color: #1D2B24; letter-spacing: -.02em; animation: welcomePop .7s .1s ease both; }
.welcome .ws { color: #6B7A72; font-weight: 600; animation: welcomePop .7s .2s ease both; }
@keyframes welcomePop { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
@keyframes welcomeOut { 0%, 70% { opacity: 1; visibility: visible; } 100% { opacity: 0; visibility: hidden; } }
.block-container { animation: pageIn .7s 1.5s ease both; }
@keyframes pageIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
</style>
"""

def show_welcome():
    if st.session_state.pop("welcome", False):
        u = st.session_state["user"]
        st.markdown(WELCOME_CSS, unsafe_allow_html=True)
        ui(f'<div class="welcome"><div class="wm">{AUTH_ICON}</div>'
           f'<div class="wh">Welcome, {esc(T(u["name"]))}</div>'
           f'<div class="ws">Signed in as {esc(S(u["role"]))}</div></div>')

# ======================= ACCOUNT PAGE =======================
def account_page():
    u = st.session_state["user"]
    head("Account", "Your sign-in details")
    ui(f'<div class="card" style="max-width:520px"><div class="row">'
       f'<div class="avatar">{initials(u["name"])}</div>'
       f'<div class="main"><div class="t">{esc(T(u["name"]))}</div>'
       f'<div class="s">Username: {esc(u["username"])}</div></div>'
       f'<span class="chip info">{esc(S(u["role"]))}</span></div></div>')
    st.write("")
    if st.button("Sign out", key="signout"):
        st.session_state.pop("user", None)
        st.rerun()

# ======================= NAVIGATION (by role) =======================
MENU = {
    "Overview": [("dashboard", dashboard_page, "Dashboard", ":material/dashboard:"),
                 ("reports", reports_page, "Reports", ":material/bar_chart:")],
    "Properties": [("owners", owners_page, "Owners", ":material/person:"),
                   ("properties", properties_page, "Properties", ":material/apartment:"),
                   ("units", units_page, "Units", ":material/meeting_room:"),
                   ("amenities", amenities_page, "Amenities", ":material/star:")],
    "People": [("tenants", tenants_page, "Tenants", ":material/group:"),
               ("staff", staff_page, "Staff", ":material/badge:")],
    "Operations": [("allocations", allocations_page, "Allocations", ":material/key:"),
                   ("payments", payments_page, "Rent Payments", ":material/payments:"),
                   ("utility", utility_page, "Utility Bills", ":material/bolt:"),
                   ("complaints", complaints_page, "Complaints", ":material/build:"),
                   ("visitors", visitors_page, "Visitors", ":material/directions_walk:")],
    "Admin": [("users", users_page, "Users", ":material/manage_accounts:")],
}
ACCESS = {
    "admin": None,
    "manager": {"dashboard", "reports", "properties", "units", "amenities", "tenants",
                "allocations", "payments", "utility", "complaints", "visitors"},
    "staff": {"dashboard", "tenants", "complaints", "visitors"},
}

me = st.session_state["user"]
allowed = ACCESS[me["role"]]
pages = {}
dash_page = None
for group, items in MENU.items():
    group_pages = []
    for key, fn, title, icon in items:
        if allowed is None or key in allowed:
            p = st.Page(fn, title=title, icon=icon, default=(key == "dashboard"))
            if key == "dashboard":
                dash_page = p
            group_pages.append(p)
    if group_pages:
        pages[group] = group_pages

first_name = T(me["name"]).split(" ")[0]
pages[f"{first_name} · {S(me['role'])}"] = [
    st.Page(account_page, title="Account", icon=":material/account_circle:")]

pg = st.navigation(pages, position="top")
if st.session_state.get("welcome") and pg.title != "Dashboard":
    st.switch_page(dash_page)
show_welcome()
pg.run()