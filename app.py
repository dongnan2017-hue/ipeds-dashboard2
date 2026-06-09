"""
IPEDS 2024-25 Interactive Dashboard
Run: streamlit run app.py
"""

import os
import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

# Larger, more readable default for every plotly figure in the app
pio.templates["ipeds"] = go.layout.Template(
    layout=go.Layout(
        font=dict(size=15, family="Arial, sans-serif"),
        title=dict(font=dict(size=17)),
        xaxis=dict(title=dict(font=dict(size=14)), tickfont=dict(size=13)),
        yaxis=dict(title=dict(font=dict(size=14)), tickfont=dict(size=13)),
        legend=dict(font=dict(size=13)),
        hoverlabel=dict(font=dict(size=14)),
        annotationdefaults=dict(font=dict(size=13)),
    )
)
pio.templates.default = "plotly+ipeds"

try:
    import statsmodels  # noqa: F401
    _HAS_STATSMODELS = True
except ImportError:
    _HAS_STATSMODELS = False

def _trend_kw() -> dict:
    """Return trendline kwargs for px.scatter only when statsmodels is available."""
    return {"trendline": "ols", "trendline_scope": "overall"} if _HAS_STATSMODELS else {}

# Paths relative to this file — works locally and on Streamlit Cloud
_HERE       = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(_HERE, "ipeds.duckdb")


def _db_is_valid() -> bool:
    """Return True only if the local DB exists and contains METRICS_LONG (multi-year)."""
    if not os.path.exists(DB_PATH):
        return False
    try:
        import duckdb as _ddb
        _con = _ddb.connect(DB_PATH, read_only=True)
        _con.execute("SELECT 1 FROM METRICS_LONG LIMIT 1")
        _con.close()
        return True
    except Exception:
        return False


def _download_db() -> None:
    """Download ipeds.duckdb from Google Drive."""
    gdrive_id = st.secrets.get("GDRIVE_DB_ID", None)
    if not gdrive_id:
        st.error(
            "Database file not found and `GDRIVE_DB_ID` secret is not set.\n\n"
            "**Locally:** run `build_db.py` first.\n\n"
            "**Streamlit Cloud:** add `GDRIVE_DB_ID` in the app's Secrets settings."
        )
        st.stop()

    import requests

    DOWNLOAD_URLS = [
        f"https://drive.usercontent.google.com/download?id={gdrive_id}&export=download&authuser=0&confirm=t",
        f"https://drive.google.com/uc?export=download&id={gdrive_id}&confirm=t",
    ]

    with st.spinner("Downloading database from Google Drive (this may take ~60 s for the multi-year DB)…"):
        tmp_path = DB_PATH + ".tmp"
        downloaded = False
        for url in DOWNLOAD_URLS:
            try:
                resp = requests.get(url, stream=True, timeout=300,
                                    headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    continue
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                size_mb = os.path.getsize(tmp_path) / 1024 / 1024
                if size_mb >= 10:
                    if os.path.exists(DB_PATH):
                        os.remove(DB_PATH)
                    os.rename(tmp_path, DB_PATH)
                    downloaded = True
                    break
                else:
                    os.remove(tmp_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        if not downloaded:
            st.error(
                "### Database download failed\n\n"
                "**Most likely cause:** the file is not shared publicly.\n\n"
                "**Fix:**\n"
                "1. Open Google Drive → right-click `ipeds.duckdb` → **Share**\n"
                "2. Change access to **Anyone with the link** → **Viewer**\n"
                "3. Copy the new link — the File ID is the long string between `/d/` and `/view`\n"
                "4. Update `GDRIVE_DB_ID` in your Streamlit Cloud **Secrets** settings\n"
                "5. Click **Refresh Data** in the sidebar"
            )
            st.stop()


def _ensure_db() -> None:
    """Ensure a valid multi-year database is present; download if missing or outdated."""
    if _db_is_valid():
        return
    # DB missing OR is the old single-year version (no METRICS_LONG) — re-download
    if os.path.exists(DB_PATH):
        st.info("Updating database to multi-year version — downloading from Google Drive…")
        os.remove(DB_PATH)
    _download_db()

st.set_page_config(
    page_title="IPEDS 2024-25 Dashboard",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Categorical decodings ────────────────────────────────────────────────────
SECTOR_MAP = {
    1: "Public 4-year+",      2: "Private NP 4-year+",   3: "Private FP 4-year+",
    4: "Public 2-year",       5: "Private NP 2-year",    6: "Private FP 2-year",
    7: "Public <2-year",      8: "Private NP <2-year",   9: "Private FP <2-year",
    -3: "Not available",
}
CONTROL_MAP = {
    1: "Public", 2: "Private non-profit", 3: "Private for-profit", -3: "Not available"
}
LEVEL_MAP = {1: "4-year", 2: "2-year", 3: "Less than 2-year", -3: "Not available"}
LOCALE_MAP = {
    11: "City: Large",    12: "City: Midsize",    13: "City: Small",
    21: "Suburb: Large",  22: "Suburb: Midsize",  23: "Suburb: Small",
    31: "Town: Fringe",   32: "Town: Distant",    33: "Town: Remote",
    41: "Rural: Fringe",  42: "Rural: Distant",   43: "Rural: Remote",
    -3: "Not available",
}
CONTROL_COLORS = {
    "Public":             "#2563EB",   # vivid blue
    "Private non-profit": "#059669",   # emerald green
    "Private for-profit": "#D97706",   # warm amber
    "Not available":      "#94A3B8",   # slate gray
}

# ── Focus institution (shared pages highlight this one) ──────────────────────
# The "College of Interest" selector in the sidebar sets focus_unitid / focus_name
# in session_state. When nothing is chosen these return None / "" and the shared
# pages simply draw no highlight. ALBION_UNITID is retained only to gate the
# Albion-specific narrative on the National Overview scatter explorer.
ALBION_UNITID = 168546

def _focus_uid():
    """UNITID of the chosen College of Interest, or None if none chosen."""
    v = st.session_state.get("focus_unitid")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None

def _focus_name() -> str:
    """Name of the chosen College of Interest, or '' if none chosen."""
    return str(st.session_state.get("focus_name") or "")
SECTOR_COLORS = {
    "Public 4-year+":      "#2563EB",  "Private NP 4-year+":  "#059669",
    "Private FP 4-year+":  "#D97706",  "Public 2-year":       "#60A5FA",
    "Private NP 2-year":   "#34D399",  "Private FP 2-year":   "#FCD34D",
    "Public <2-year":      "#818CF8",  "Private NP <2-year":  "#6EE7B7",
    "Private FP <2-year":  "#FDE68A",  "Not available":       "#94A3B8",
}
CALSYS_MAP = {
    1: "Semester", 2: "Quarter", 3: "Trimester", 4: "4-1-4 / 4-4-1",
    5: "Other", 6: "Differs by program", 7: "Continuous", -1: "N/A",
}
FACSTAT_MAP = {
    0: "All faculty", 10: "With faculty status (total)",
    20: "Tenured", 30: "On tenure track (not yet tenured)",
    40: "Not on tenure track (total)", 41: "Multi-year contract",
    42: "Annual contract", 43: "Less-than-annual contract",
    44: "At-will", 45: "Other",
    50: "Without faculty status",
}
OCC_LABELS = {
    100: "All staff",                   200: "Instructional (total)",
    210: "Instruction only",            220: "Instruction/research/pub svc",
    300: "Research",                    400: "Public service",
    500: "Management",                  600: "Business & financial ops",
    700: "Computer, eng & science",     800: "Community svc, legal, arts",
    900: "Healthcare",                 1000: "Librarians & curators",
    1100: "Student & academic affairs",1200: "Office & admin support",
    1300: "Natural res, constr & maint",1400: "Service",
    1500: "Sales & related",           1600: "Production, transport & matl",
}

# Variables available in the scatter explorer (label → column name)
SCATTER_VARS = {
    # Admissions
    "Acceptance Rate (%)":      "DVADM01",
    "Yield Rate (%)":           "DVADM04",
    # Outcomes
    "Grad Rate 150% (%)":       "GRRTTOT",
    "Bach 6-yr Rate (%)":       "GBA6RTT",
    "Pell Grad Rate (%)":       "PGGRRTT",
    "FT Retention Rate (%)":    "RET_PCF",
    "PT Retention Rate (%)":    "RET_PCP",
    "FT Award Rate 8yr (%)":    "OM1TOTLAWDP8",
    "PT Award Rate 8yr (%)":    "OM2TOTLAWDP8",
    "Transfer-out Rate (%)":    "TRRTTOT",
    # Enrollment
    "Total Enrollment":         "ENRTOT",
    "FTE Enrollment":           "FTE",
    "Undergrad Enrollment":     "EFUG",
    "Graduate Enrollment":      "EFGRAD",
    "FT Enrollment":            "ENRFT",
    "PT Enrollment":            "ENRPT",
    # Costs
    "In-State COA ($)":         "CINSON",
    "Out-of-State COA ($)":     "COTSON",
    "Tuition & Fees ($)":        "TUFEYR3",
    # Financial Aid
    "% Receiving Any Aid":      "ANYAIDP",
    "% Receiving Pell":         "PGRNT_P",
    "Avg Pell Grant ($)":       "PGRNT_A",
    "Avg Any Grant ($)":        "AGRNT_A",
    "% Student Loans":          "LOAN_P",
    # Faculty & HR
    "Avg Faculty Salary ($)":   "SALTOTL",
    "Student-Faculty Ratio":    "STUFACR",
    "Total FTE Staff":          "SFTETOTL",
    "Instructional FTE":        "SFTEINST",
    # Demographics
    "% Women":                  "PCTENRW",
    "% White":                  "PCTENRWH",
    "% Black/AA":               "PCTENRBK",
    "% Hispanic":               "PCTENRHS",
    "% Asian/PI":               "PCTENRAP",
    "% Exclusively DE":         "PCTDEEXC",
    # Library
    "Library Expend/FTE ($)":   "LEXPTOTF",
    "E-Books per FTE":          "LEBOOKSP",
    # Degrees
    "Bachelor's Awarded":       "BASDEG",
    "Master's Awarded":         "MASDEG",
    "Associate's Awarded":      "ASCDEG",
}

SCATTER_SUGGESTIONS = [
    # (label, x_var, y_var, z_var)
    ("Selectivity → Outcomes",  "Acceptance Rate (%)",  "Grad Rate 150% (%)",    "Total Enrollment"),
    ("Cost → Equity",           "In-State COA ($)",     "% Receiving Pell",      "Avg Pell Grant ($)"),
    ("Faculty → Retention",     "Student-Faculty Ratio","FT Retention Rate (%)", "Avg Faculty Salary ($)"),
    ("Equity Lens",             "% Receiving Pell",     "Pell Grad Rate (%)",    "Total Enrollment"),
]

SCATTER_RATIONALE = [
    # Selectivity → Outcomes
    (
        "**Selectivity → Outcomes** tests whether graduation success is *earned* or merely *selected into*. "
        "Highly selective colleges (low acceptance rates) graduate at high rates partly because they admit "
        "already-prepared students. The more interesting institutions are in the **upper-right quadrant**: "
        "high acceptance rates AND high six-year graduation rates. These are the high-access, high-success "
        "models worth studying. Bubble size = total enrollment, so you can tell whether strong performance "
        "holds at scale or only at small, boutique institutions."
    ),
    # Cost → Equity
    (
        "**Cost → Equity** challenges the assumption that expensive colleges are only for the wealthy. "
        "Some institutions with sticker prices above $60,000 enroll *more* Pell Grant recipients than many "
        "public universities — and they fund them generously (large bubbles = high average Pell award). "
        "When reading this chart, look at both the vertical position (how many low-income students they "
        "serve) and the bubble size (how much financial support they actually provide). Institutions in the "
        "upper half with large bubbles are making a high-cost education broadly accessible."
    ),
    # Faculty → Retention
    (
        "**Faculty → Retention** explores the structural conditions that keep first-year students enrolled. "
        "A low student-to-faculty ratio creates the conditions for individualized attention — a known driver "
        "of early persistence. Bubble size adds a second dimension: institutions that also pay competitive "
        "faculty salaries (larger bubbles) invest in attracting and retaining strong teachers, not just "
        "hiring more of them. The **upper-left quadrant** — small class sizes, well-compensated faculty, "
        "high retention — represents the full-investment model of student success."
    ),
    # Equity Lens
    (
        "**Equity Lens** is perhaps the most policy-relevant view. Among institutions that enroll *identical* "
        "shares of Pell Grant students, graduation outcomes vary by 30–40 percentage points. This variation "
        "cannot be explained by who they admit — it reflects what they do *after* enrollment: advising, "
        "support services, financial aid packaging, and campus culture. Look **above the trend line** for "
        "institutions beating expectations for their Pell population. A small bubble well above the line is "
        "the strongest signal: that institution is closing equity gaps without needing massive scale."
    ),
]


# ── Data loading ─────────────────────────────────────────────────────────────

_SQL_2425 = """
    SELECT
        h.UNITID, h.INSTNM, h.CITY, h.STABBR, h.WEBADDR, h.COUNTYCD,
        h.SECTOR, h.ICLEVEL, h.CONTROL, h.HLOFFER, h.DEGGRANT,
        h.HBCU, h.TRIBAL, h.MEDICAL, h.LOCALE, h.INSTSIZE, h.OBEREG,
        h.CARNEGIEIC, h.CARNEGIERSCH, h.CARNEGIESIZE, h.CARNEGIEALF,
        h.LANDGRNT, h.CYACTIVE, h.LONGITUD, h.LATITUDE,
        h.ZIP, h.CHFNM, h.CHFTITLE, h.GENTELE, h.OPEID,
        h.ADMINURL, h.FAIDURL, h.APPLURL, h.NPRICURL, h.VETURL, h.DISAURL,
        e.ENRTOT, e.FTE, e.EFUG, e.EFGRAD, e.ENRFT, e.ENRPT,
        e.PCTENRW, e.PCTENRWH, e.PCTENRBK, e.PCTENRHS,
        e.PCTENRAP, e.PCTENRAN, e.PCTENRUN, e.PCTENRNR, e.PCTENR2M,
        e.PCTDEEXC, e.PCTDESOM, e.PCTDENON, e.PCTFT1ST,
        ef12.UNDUP AS EF12UNDUP, ef12.UNDUPUG AS EF12UNDUPUG,
        ef12.E12FT AS EF12FT, ef12.E12PT AS EF12PT,
        ef12.E12UGFT AS EF12UGFT, ef12.E12GRAD AS EF12GRAD,
        a.DVADM01, a.DVADM02, a.DVADM03, a.DVADM04,
        a.DVADM05, a.DVADM06, a.DVADM07, a.DVADM08,
        a.DVADM09, a.DVADM10, a.DVADM11, a.DVADM12,
        g.GRRTTOT, g.GRRTM, g.GRRTW,
        g.GRRTAN, g.GRRTAP, g.GRRTAS, g.GRRTNH, g.GRRTBK, g.GRRTHS, g.GRRTWH, g.GRRT2M, g.GRRTUN, g.GRRTNR,
        g.GBA4RTT, g.GBA5RTT, g.GBA6RTT, g.GBA6RTM, g.GBA6RTW,
        g.GBA6RTAN, g.GBA6RTAP, g.GBA6RTAS, g.GBA6RTNH, g.GBA6RTBK, g.GBA6RTHS, g.GBA6RTWH, g.GBA6RT2M, g.GBA6RTUN, g.GBA6RTNR,
        g.GBATRRT, g.PGGRRTT, g.PGBA6RT, g.SSGRRTT, g.SSBA6RT, g.NRGRRTT, g.NRBA6RT, g.TRRTTOT,
        c.CINSON, c.COTSON, c.CINDON, c.TUFEYR3,
        d.BASDEG, d.MASDEG, d.DOCDEGRS, d.DOCDEGPP, d.DOCDEGOT,
        d.ASCDEG, d.CERT1A, d.CERT1B, d.CERT2, d.CERT4,
        r.SALTOTL, r.SALPROF, r.SALASSC, r.SALASST, r.SALINST,
        r.SALLECT, r.SALNRNK,
        r.SFTETOTL, r.SFTEPSTC, r.SFTEINST, r.SFTERSRC, r.SFTEPBSV,
        r.SFTELCAI, r.SFTELCA, r.SFTEOTIS, r.SFTEMNGM, r.SFTEBFO,
        r.SFTECES, r.SFTECLAM, r.SFTEHLTH, r.SFTEOTHR, r.SFTESRVC,
        r.SFTESALE, r.SFTEOFAS, r.SFTENRCM, r.SFTEPTMM,
        f.F1CORREV, f.F1COREXP, f.F2CORREV, f.F2COREXP,
        ef.RET_PCF, ef.RET_PCP, ef.STUFACR,
        ef.GRCOHRT, ef.UGENTERN, ef.PGRCOHRT,
        ef.RRFTCT, ef.RRFTCTA, ef.RET_NMF,
        ef.RRPTCT, ef.RRPTCTA, ef.RET_NMP,
        s.ANYAIDP, s.PGRNT_P, s.PGRNT_A, s.AGRNT_P, s.AGRNT_A,
        s.LOAN_P, s.LOAN_A, s.FGRNT_P, s.IGRNT_P, s.SGRNT_P,
        om.OM1TOTLAWDP4, om.OM1TOTLAWDP6, om.OM1TOTLAWDP8,
        om.OM1TOTLENYP8,
        om.OM1PELLAWDP4, om.OM1PELLAWDP6, om.OM1PELLAWDP8,
        om.OM1NPELAWDP4, om.OM1NPELAWDP6, om.OM1NPELAWDP8,
        om.OM2TOTLAWDP4, om.OM2TOTLAWDP6, om.OM2TOTLAWDP8,
        om.OM2TOTLENYP8,
        om.OM2PELLAWDP4, om.OM2PELLAWDP6, om.OM2PELLAWDP8,
        om.OM2NPELAWDP4, om.OM2NPELAWDP6, om.OM2NPELAWDP8,
        om.OM3TOTLAWDP4, om.OM3TOTLAWDP6, om.OM3TOTLAWDP8,
        om.OM4TOTLAWDP4, om.OM4TOTLAWDP6, om.OM4TOTLAWDP8,
        lib.LPBOOKSP, lib.LEBOOKSP, lib.LEXPTOTF, lib.LTOTLFTE AS LIBLFTE
    FROM HD2024 h
    LEFT JOIN DRVEF2024      e    ON h.UNITID = e.UNITID
    LEFT JOIN DRVEF122024    ef12 ON h.UNITID = ef12.UNITID
    LEFT JOIN DRVADM2024     a    ON h.UNITID = a.UNITID
    LEFT JOIN DRVGR2024      g    ON h.UNITID = g.UNITID
    LEFT JOIN DRVCOST2024    c    ON h.UNITID = c.UNITID
    LEFT JOIN DRVC2024       d    ON h.UNITID = d.UNITID
    LEFT JOIN DRVHR2024      r    ON h.UNITID = r.UNITID
    LEFT JOIN DRVF2024       f    ON h.UNITID = f.UNITID
    LEFT JOIN EF2024D        ef   ON h.UNITID = ef.UNITID
    LEFT JOIN SFA2324        s    ON h.UNITID = s.UNITID
    LEFT JOIN DRVOM2024      om   ON h.UNITID = om.UNITID
    LEFT JOIN DRVAL2024      lib  ON h.UNITID = lib.UNITID
"""

_SQL_2223 = """
    SELECT
        h.UNITID, h.INSTNM, h.CITY, h.STABBR, h.WEBADDR, h.COUNTYCD,
        h.SECTOR, h.ICLEVEL, h.CONTROL, h.HLOFFER, h.DEGGRANT,
        h.HBCU, h.TRIBAL, h.MEDICAL, h.LOCALE, h.INSTSIZE, h.OBEREG,
        NULL AS CARNEGIEIC, NULL AS CARNEGIERSCH, NULL AS CARNEGIESIZE, NULL AS CARNEGIEALF,
        h.LANDGRNT, h.CYACTIVE, h.LONGITUD, h.LATITUDE,
        h.ZIP, h.CHFNM, h.CHFTITLE, h.GENTELE, h.OPEID,
        h.ADMINURL, h.FAIDURL, h.APPLURL, h.NPRICURL, h.VETURL, h.DISAURL,
        e.ENRTOT, e.FTE, e.EFUG, e.EFGRAD, e.ENRFT, e.ENRPT,
        e.PCTENRW, e.PCTENRWH, e.PCTENRBK, e.PCTENRHS,
        e.PCTENRAP, e.PCTENRAN, e.PCTENRUN, e.PCTENRNR, e.PCTENR2M,
        e.PCTDEEXC, e.PCTDESOM, e.PCTDENON, e.PCTFT1ST,
        ef12.UNDUP AS EF12UNDUP, ef12.UNDUPUG AS EF12UNDUPUG,
        ef12.E12FT AS EF12FT, ef12.E12PT AS EF12PT,
        ef12.E12UGFT AS EF12UGFT, ef12.E12GRAD AS EF12GRAD,
        a.DVADM01, a.DVADM02, a.DVADM03, a.DVADM04,
        a.DVADM05, a.DVADM06, a.DVADM07, a.DVADM08,
        a.DVADM09, a.DVADM10, a.DVADM11, a.DVADM12,
        g.GRRTTOT, g.GRRTM, g.GRRTW,
        g.GRRTAN, g.GRRTAP, g.GRRTAS, g.GRRTNH, g.GRRTBK, g.GRRTHS, g.GRRTWH, g.GRRT2M, g.GRRTUN, g.GRRTNR,
        g.GBA4RTT, g.GBA5RTT, g.GBA6RTT, g.GBA6RTM, g.GBA6RTW,
        g.GBA6RTAN, g.GBA6RTAP, g.GBA6RTAS, g.GBA6RTNH, g.GBA6RTBK, g.GBA6RTHS, g.GBA6RTWH, g.GBA6RT2M, g.GBA6RTUN, g.GBA6RTNR,
        g.GBATRRT, g.PGGRRTT, g.PGBA6RT, g.SSGRRTT, g.SSBA6RT, g.NRGRRTT, g.NRBA6RT, g.TRRTTOT,
        c.CINSON, c.COTSON, c.CINDON, c.TUFEYR3,
        d.BASDEG, d.MASDEG, d.DOCDEGRS, d.DOCDEGPP, d.DOCDEGOT,
        d.ASCDEG, d.CERT1A, d.CERT1B, d.CERT2, d.CERT4,
        r.SALTOTL, r.SALPROF, r.SALASSC, r.SALASST, r.SALINST,
        r.SALLECT, r.SALNRNK,
        r.SFTETOTL, r.SFTEPSTC, r.SFTEINST, r.SFTERSRC, r.SFTEPBSV,
        r.SFTELCAI, r.SFTELCA, r.SFTEOTIS, r.SFTEMNGM, r.SFTEBFO,
        r.SFTECES, r.SFTECLAM, r.SFTEHLTH, r.SFTEOTHR, r.SFTESRVC,
        r.SFTESALE, r.SFTEOFAS, r.SFTENRCM, r.SFTEPTMM,
        f.F1CORREV, f.F1COREXP, f.F2CORREV, f.F2COREXP,
        ef.RET_PCF, ef.RET_PCP, ef.STUFACR,
        ef.GRCOHRT, ef.UGENTERN, ef.PGRCOHRT,
        ef.RRFTCT, ef.RRFTCTA, ef.RET_NMF,
        ef.RRPTCT, ef.RRPTCTA, ef.RET_NMP,
        s.ANYAIDP, s.PGRNT_P, s.PGRNT_A, s.AGRNT_P, s.AGRNT_A,
        s.LOAN_P, s.LOAN_A, s.FGRNT_P, s.IGRNT_P, s.SGRNT_P,
        om.OM1TOTLAWDP4, om.OM1TOTLAWDP6, om.OM1TOTLAWDP8,
        om.OM1TOTLENYP8,
        om.OM1PELLAWDP4, om.OM1PELLAWDP6, om.OM1PELLAWDP8,
        om.OM1NPELAWDP4, om.OM1NPELAWDP6, om.OM1NPELAWDP8,
        om.OM2TOTLAWDP4, om.OM2TOTLAWDP6, om.OM2TOTLAWDP8,
        om.OM2TOTLENYP8,
        om.OM2PELLAWDP4, om.OM2PELLAWDP6, om.OM2PELLAWDP8,
        om.OM2NPELAWDP4, om.OM2NPELAWDP6, om.OM2NPELAWDP8,
        om.OM3TOTLAWDP4, om.OM3TOTLAWDP6, om.OM3TOTLAWDP8,
        om.OM4TOTLAWDP4, om.OM4TOTLAWDP6, om.OM4TOTLAWDP8,
        lib.LPBOOKSP, lib.LEBOOKSP, lib.LEXPTOTF, lib.LTOTLFTE AS LIBLFTE
    FROM HD2022 h
    LEFT JOIN DRVEF2022      e    ON h.UNITID = e.UNITID
    LEFT JOIN DRVEF122022    ef12 ON h.UNITID = ef12.UNITID
    LEFT JOIN DRVADM2022     a    ON h.UNITID = a.UNITID
    LEFT JOIN DRVGR2022      g    ON h.UNITID = g.UNITID
    LEFT JOIN DRVIC2022      c    ON h.UNITID = c.UNITID
    LEFT JOIN DRVC2022       d    ON h.UNITID = d.UNITID
    LEFT JOIN DRVHR2022      r    ON h.UNITID = r.UNITID
    LEFT JOIN DRVF2022       f    ON h.UNITID = f.UNITID
    LEFT JOIN EF2022D        ef   ON h.UNITID = ef.UNITID
    LEFT JOIN SFA2122_P1     s    ON h.UNITID = s.UNITID
    LEFT JOIN DRVOM2022      om   ON h.UNITID = om.UNITID
    LEFT JOIN DRVAL2022      lib  ON h.UNITID = lib.UNITID
"""

_SQL_2324 = """
    SELECT
        h.UNITID, h.INSTNM, h.CITY, h.STABBR, h.WEBADDR, h.COUNTYCD,
        h.SECTOR, h.ICLEVEL, h.CONTROL, h.HLOFFER, h.DEGGRANT,
        h.HBCU, h.TRIBAL, h.MEDICAL, h.LOCALE, h.INSTSIZE, h.OBEREG,
        NULL AS CARNEGIEIC, NULL AS CARNEGIERSCH, NULL AS CARNEGIESIZE, NULL AS CARNEGIEALF,
        h.LANDGRNT, h.CYACTIVE, h.LONGITUD, h.LATITUDE,
        h.ZIP, h.CHFNM, h.CHFTITLE, h.GENTELE, h.OPEID,
        h.ADMINURL, h.FAIDURL, h.APPLURL, h.NPRICURL,
        NULL AS VETURL, NULL AS DISAURL,
        e.ENRTOT, e.FTE, e.EFUG, e.EFGRAD, e.ENRFT, e.ENRPT,
        e.PCTENRW, e.PCTENRWH, e.PCTENRBK, e.PCTENRHS,
        e.PCTENRAP, e.PCTENRAN, e.PCTENRUN, e.PCTENRNR, e.PCTENR2M,
        e.PCTDEEXC, NULL AS PCTDESOM, NULL AS PCTDENON, e.PCTFT1ST,
        ef12.UNDUP AS EF12UNDUP, ef12.UNDUPUG AS EF12UNDUPUG,
        ef12.E12FT AS EF12FT, ef12.E12PT AS EF12PT,
        ef12.E12UGFT AS EF12UGFT, ef12.E12GRAD AS EF12GRAD,
        a.DVADM01, a.DVADM02, a.DVADM03, a.DVADM04,
        a.DVADM05, a.DVADM06, a.DVADM07, a.DVADM08,
        a.DVADM09, a.DVADM10, a.DVADM11, a.DVADM12,
        g.GRRTTOT, g.GRRTM, g.GRRTW,
        g.GRRTAN, g.GRRTAP, g.GRRTAS, g.GRRTNH, g.GRRTBK, g.GRRTHS, g.GRRTWH, g.GRRT2M, g.GRRTUN, g.GRRTNR,
        g.GBA4RTT, g.GBA5RTT, g.GBA6RTT,
        NULL AS GBA6RTM, NULL AS GBA6RTW,
        NULL AS GBA6RTAN, NULL AS GBA6RTAP, NULL AS GBA6RTAS, NULL AS GBA6RTNH,
        NULL AS GBA6RTBK, NULL AS GBA6RTHS, NULL AS GBA6RTWH, NULL AS GBA6RT2M,
        NULL AS GBA6RTUN, NULL AS GBA6RTNR,
        NULL AS GBATRRT, g.PGGRRTT, NULL AS PGBA6RT,
        NULL AS SSGRRTT, NULL AS SSBA6RT, NULL AS NRGRRTT, NULL AS NRBA6RT, g.TRRTTOT,
        c.CINSON, c.COTSON, NULL AS CINDON, c.TUFEYR3,
        d.BASDEG, d.MASDEG, d.DOCDEGRS, d.DOCDEGPP, NULL AS DOCDEGOT,
        d.ASCDEG, d.CERT1A, d.CERT1B, d.CERT2, d.CERT4,
        r.SALTOTL,
        NULL AS SALPROF, NULL AS SALASSC, NULL AS SALASST, NULL AS SALINST,
        NULL AS SALLECT, NULL AS SALNRNK,
        r.SFTETOTL, NULL AS SFTEPSTC, r.SFTEINST, NULL AS SFTERSRC, NULL AS SFTEPBSV,
        NULL AS SFTELCAI, NULL AS SFTELCA, NULL AS SFTEOTIS, NULL AS SFTEMNGM, NULL AS SFTEBFO,
        NULL AS SFTECES, NULL AS SFTECLAM, NULL AS SFTEHLTH, NULL AS SFTEOTHR, NULL AS SFTESRVC,
        NULL AS SFTESALE, NULL AS SFTEOFAS, NULL AS SFTENRCM, NULL AS SFTEPTMM,
        f.F1CORREV, f.F1COREXP, f.F2CORREV, f.F2COREXP,
        ef.RET_PCF, ef.RET_PCP, ef.STUFACR,
        NULL AS GRCOHRT, NULL AS UGENTERN, NULL AS PGRCOHRT,
        NULL AS RRFTCT, NULL AS RRFTCTA, NULL AS RET_NMF,
        NULL AS RRPTCT, NULL AS RRPTCTA, NULL AS RET_NMP,
        s.ANYAIDP, s.PGRNT_P, s.PGRNT_A, s.AGRNT_P, s.AGRNT_A,
        s.LOAN_P, NULL AS LOAN_A, s.FGRNT_P, s.IGRNT_P, s.SGRNT_P,
        NULL AS OM1TOTLAWDP4, NULL AS OM1TOTLAWDP6, om.OM1TOTLAWDP8,
        NULL AS OM1TOTLENYP8,
        NULL AS OM1PELLAWDP4, NULL AS OM1PELLAWDP6, om.OM1PELLAWDP8,
        NULL AS OM1NPELAWDP4, NULL AS OM1NPELAWDP6, om.OM1NPELAWDP8,
        NULL AS OM2TOTLAWDP4, NULL AS OM2TOTLAWDP6, om.OM2TOTLAWDP8,
        NULL AS OM2TOTLENYP8,
        NULL AS OM2PELLAWDP4, NULL AS OM2PELLAWDP6, NULL AS OM2PELLAWDP8,
        NULL AS OM2NPELAWDP4, NULL AS OM2NPELAWDP6, NULL AS OM2NPELAWDP8,
        NULL AS OM3TOTLAWDP4, NULL AS OM3TOTLAWDP6, NULL AS OM3TOTLAWDP8,
        NULL AS OM4TOTLAWDP4, NULL AS OM4TOTLAWDP6, NULL AS OM4TOTLAWDP8,
        NULL AS LPBOOKSP, lib.LEBOOKSP, lib.LEXPTOTF, NULL AS LIBLFTE
    FROM HD2023 h
    LEFT JOIN DRVEF2023      e    ON h.UNITID = e.UNITID
    LEFT JOIN DRVEF122023    ef12 ON h.UNITID = ef12.UNITID
    LEFT JOIN DRVADM2023     a    ON h.UNITID = a.UNITID
    LEFT JOIN DRVGR2023      g    ON h.UNITID = g.UNITID
    LEFT JOIN DRVIC2023      c    ON h.UNITID = c.UNITID
    LEFT JOIN DRVC2023       d    ON h.UNITID = d.UNITID
    LEFT JOIN DRVHR2023      r    ON h.UNITID = r.UNITID
    LEFT JOIN DRVF2023       f    ON h.UNITID = f.UNITID
    LEFT JOIN EF2023D        ef   ON h.UNITID = ef.UNITID
    LEFT JOIN SFA2223_P1     s    ON h.UNITID = s.UNITID
    LEFT JOIN DRVOM2023      om   ON h.UNITID = om.UNITID
    LEFT JOIN DRVAL2023      lib  ON h.UNITID = lib.UNITID
"""


@st.cache_data(show_spinner="Loading IPEDS data …")
def load_master(year: str = "2024-25") -> pd.DataFrame:
    con = duckdb.connect(DB_PATH, read_only=True)
    sql = _SQL_2425 if year == "2024-25" else _SQL_2324 if year == "2023-24" else _SQL_2223
    df = con.execute(sql).df()
    def _make_map(varname: str) -> dict:
        rows = con.execute(
            f"SELECT CODEVALUE, VALUELABEL FROM META_VALUES "
            f"WHERE upper(VARNAME)='{varname.upper()}'"
        ).fetchall()
        return {int(r[0]): r[1] for r in rows if r[0] is not None}

    carnegie_ic_map   = _make_map("CARNEGIEIC")
    carnegie_rsch_map = _make_map("CARNEGIERSCH")
    carnegie_size_map = _make_map("CARNEGIESIZE")
    carnegie_alf_map  = _make_map("CARNEGIEALF")
    instsize_map      = _make_map("INSTSIZE")
    obereg_map        = _make_map("OBEREG")
    hloffer_map       = _make_map("HLOFFER")

    con.close()

    df["SECTOR_LBL"]       = df["SECTOR"].map(SECTOR_MAP).fillna("Not available")
    df["CONTROL_LBL"]      = df["CONTROL"].map(CONTROL_MAP).fillna("Not available")
    df["LEVEL_LBL"]        = df["ICLEVEL"].map(LEVEL_MAP).fillna("Not available")
    df["LOCALE_LBL"]       = df["LOCALE"].map(LOCALE_MAP).fillna("Not available")
    df["CARNEGIE_LBL"]     = df["CARNEGIEIC"].map(carnegie_ic_map).fillna("Not classified")
    df["CARNEGIERSCH_LBL"] = df["CARNEGIERSCH"].map(carnegie_rsch_map).fillna("Not classified")
    df["CARNEGIESIZE_LBL"] = df["CARNEGIESIZE"].map(carnegie_size_map).fillna("Not classified")
    df["CARNEGIEALF_LBL"]  = df["CARNEGIEALF"].map(carnegie_alf_map).fillna("Not classified")
    df["INSTSIZE_LBL"]     = df["INSTSIZE"].map(instsize_map).fillna("Not available")
    df["OBEREG_LBL"]       = df["OBEREG"].map(obereg_map).fillna("Not available")
    df["HLOFFER_LBL"]      = df["HLOFFER"].map(hloffer_map).fillna("Not available")
    df["DISPLAY_NAME"]     = df["INSTNM"] + " (" + df["STABBR"] + ")"
    return df


@st.cache_data(show_spinner=False)
def load_trends() -> pd.DataFrame:
    """Load METRICS_LONG (all years) with label columns decoded."""
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute("SELECT * FROM METRICS_LONG").df()

    def _map(varname: str) -> dict:
        rows = con.execute(
            f"SELECT CODEVALUE, VALUELABEL FROM META_VALUES "
            f"WHERE upper(VARNAME)='{varname.upper()}'"
        ).fetchall()
        return {int(r[0]): r[1] for r in rows if r[0] is not None}

    instsize_map = _map("INSTSIZE")
    obereg_map   = _map("OBEREG")
    con.close()

    df["CONTROL_LBL"] = df["CONTROL"].map(CONTROL_MAP).fillna("Not available")
    df["SECTOR_LBL"]  = df["SECTOR"].map(SECTOR_MAP).fillna("Not available")
    df["LEVEL_LBL"]   = df["ICLEVEL"].map(LEVEL_MAP).fillna("Not available")
    df["INSTSIZE_LBL"] = df["INSTSIZE"].map(instsize_map).fillna("Not available")
    df["OBEREG_LBL"]   = df["OBEREG"].map(obereg_map).fillna("Not available")
    return df


def fmt(val, style="number", na="N/A"):
    if pd.isna(val):
        return na
    if style == "pct":
        return f"{val:.1f}%"
    if style == "dollar":
        return f"${val:,.0f}"
    if style == "int":
        return f"{int(val):,}"
    return f"{val:,.1f}"


def val_label(con, varname: str, code) -> str:
    if pd.isna(code):
        return "N/A"
    try:
        r = con.execute(
            "SELECT VALUELABEL FROM META_VALUES "
            f"WHERE upper(VARNAME)='{varname.upper()}' "
            f"AND cast(CODEVALUE as varchar)='{int(code)}' LIMIT 1"
        ).fetchone()
        return r[0] if r else str(int(code))
    except Exception:
        return str(code)


def yesno(val) -> str:
    if pd.isna(val):
        return "N/A"
    return "Yes" if int(val) == 1 else "No"


# ── Sidebar filters ──────────────────────────────────────────────────────────
def apply_filters(df: pd.DataFrame) -> tuple:
    """Return (filtered_df, selected_group_names). Group names are always empty now —
    the inherited grouping.csv cohorts are no longer surfaced anywhere in the app."""
    st.sidebar.header("Filters")

    sel_groups: list[str] = []

    st.sidebar.subheader("General Filters")
    active_only = st.sidebar.checkbox("Active institutions only", value=True)
    if active_only and "CYACTIVE" in df.columns:
        df = df[df["CYACTIVE"] == 1]

    states = sorted(df["STABBR"].dropna().unique())
    sel_states = st.sidebar.multiselect("State / Territory", states)
    if sel_states:
        df = df[df["STABBR"].isin(sel_states)]

    controls = sorted(df["CONTROL_LBL"].dropna().unique())
    sel_ctrl = st.sidebar.multiselect("Control", controls)
    if sel_ctrl:
        df = df[df["CONTROL_LBL"].isin(sel_ctrl)]

    levels = sorted(df["LEVEL_LBL"].dropna().unique())
    sel_lvl = st.sidebar.multiselect("Level", levels)
    if sel_lvl:
        df = df[df["LEVEL_LBL"].isin(sel_lvl)]

    if st.sidebar.checkbox("HBCU only"):
        df = df[df["HBCU"] == 1]
    if st.sidebar.checkbox("Tribal college only"):
        df = df[df["TRIBAL"] == 1]

    regions = sorted(df["OBEREG_LBL"].dropna().unique())
    sel_reg = st.sidebar.multiselect("Geographic Region", regions)
    if sel_reg:
        df = df[df["OBEREG_LBL"].isin(sel_reg)]

    sizes = sorted(df["INSTSIZE_LBL"].dropna().unique())
    sel_sz = st.sidebar.multiselect("Institution Size", sizes)
    if sel_sz:
        df = df[df["INSTSIZE_LBL"].isin(sel_sz)]

    carnegies = sorted(df["CARNEGIE_LBL"].dropna().unique())
    sel_carn = st.sidebar.multiselect("Carnegie Classification", carnegies)
    if sel_carn:
        df = df[df["CARNEGIE_LBL"].isin(sel_carn)]

    max_enr = int(df["ENRTOT"].max(skipna=True)) if df["ENRTOT"].notna().any() else 100_000
    min_enr = st.sidebar.number_input("Min total enrollment", min_value=0, max_value=max_enr, value=0, step=100)
    if min_enr > 0:
        df = df[df["ENRTOT"].fillna(0) >= min_enr]

    st.sidebar.caption(f"**{len(df):,}** institutions match")
    return df, sel_groups


def _inst_table(df_tbl: pd.DataFrame, sort_col: str, ascending: bool = False, height: int = 520):
    """Render an institution table; the focus institution's row highlighted in vivid amber."""
    tbl = (df_tbl
           .sort_values(sort_col, ascending=ascending, na_position="last")
           .reset_index(drop=True))
    _fname = _focus_name()
    albion_idx = (set(tbl.index[tbl.iloc[:, 0].astype(str).str.contains(_fname, na=False, regex=False)])
                  if _fname else set())
    def _style_row(r):
        if r.name in albion_idx:
            return ["background-color:#FDE68A;color:#78350F;font-weight:bold"] * len(r)
        return [""] * len(r)
    st.dataframe(
        tbl.style.apply(_style_row, axis=1),
        use_container_width=True, height=height,
    )


def _add_albion_vline(fig, alb_row, col: str, label=None):
    """Add an amber dotted vertical reference line at the focus institution's value."""
    if alb_row is None:
        return
    if label is None:
        label = f"◆ {_focus_name()}"
    val = alb_row.get(col) if isinstance(alb_row, dict) else getattr(alb_row, col, None)
    try:
        val = float(val)
    except (TypeError, ValueError):
        return
    if pd.isna(val):
        return
    fig.add_vline(
        x=val,
        line_color="#D97706",
        line_width=2.5,
        line_dash="dot",
        annotation_text=label,
        annotation_position="top right",
        annotation_font_color="#92400E",
        annotation_font_size=11,
        annotation_bgcolor="rgba(253,230,138,0.92)",
        annotation_bordercolor="#D97706",
        annotation_borderwidth=1,
    )


# ── Shared YoY helpers (used by National Trends and Albion Trends tabs) ──────
TREND_METRICS = {
    "Grad Rate 150% (%)":     ("GRRTTOT",      True),
    "FT Retention Rate (%)":  ("RET_PCF",       True),
    "Acceptance Rate (%)":    ("DVADM01",       False),
    "% Receiving Pell":       ("PGRNT_P",       True),
    "In-State COA ($)":       ("CINSON",        False),
    "Avg Faculty Salary ($)": ("SALTOTL",       True),
    "FT Enrollment":          ("ENRFT",         True),
    "8-yr Award Rate (%)":    ("OM1TOTLAWDP8",  True),
    "Student:Faculty Ratio":  ("STUFACR",       False),
}


def _render_inst_pivot(inst_df: "pd.DataFrame"):
    """Render Metric | year… | Δ table for the given institution rows."""
    rows_out = []
    for label, (col, _) in TREND_METRICS.items():
        row = {"Metric": label}
        for _, r in inst_df.iterrows():
            val = r.get(col)
            row[r["YEAR"]] = float(val) if val is not None and not pd.isna(val) else float("nan")
        rows_out.append(row)
    piv = pd.DataFrame(rows_out).set_index("Metric")
    _yr_cols = sorted(c for c in piv.columns if len(c) == 7 and c[4] == "-")
    if len(_yr_cols) >= 2:
        piv["Δ"] = piv[_yr_cols[-1]] - piv[_yr_cols[-2]]

    def _cd(v):
        if pd.isna(v):
            return ""
        return "color:#059669;font-weight:bold" if v > 0 else (
               "color:#DC2626;font-weight:bold" if v < 0 else "")

    st.dataframe(
        piv.style
           .map(_cd, subset=["Δ"] if "Δ" in piv.columns else [])
           .format(lambda v: f"{v:,.1f}" if pd.notna(v) else "—"),
        use_container_width=True,
    )


# ── Page 1: National Overview ────────────────────────────────────────────────
def page_overview(df: pd.DataFrame, sel_groups: list | None = None, year: str = "2024-25"):
    h_col, y_col = st.columns([7, 3])
    with h_col:
        if sel_groups:
            group_label = ", ".join(sel_groups)
            st.title(f"Cohort Overview — {group_label}")
        else:
            st.title(f"National Overview — IPEDS {year}")
    with y_col:
        st.markdown("<div style='padding-top:1.1rem'></div>", unsafe_allow_html=True)
        st.radio("Data Year", ["2024-25", "2023-24", "2022-23"], horizontal=True,
                 key="year_National Overview", label_visibility="collapsed")
    _alb = df[df["UNITID"] == _focus_uid()]
    alb_row = _alb.iloc[0] if not _alb.empty else None

    total    = len(df)
    tot_enr  = int(df["ENRTOT"].sum(skipna=True))
    med_adm  = df["DVADM01"].median(skipna=True)
    med_gr   = df["GRRTTOT"].median(skipna=True)
    med_ret  = df["RET_PCF"].median(skipna=True)
    tot_deg  = int(df[["BASDEG","MASDEG","DOCDEGRS","DOCDEGPP","ASCDEG"]].sum(skipna=True).sum())
    med_om8  = df["OM1TOTLAWDP8"].median(skipna=True)
    med_sal  = df["SALTOTL"].median(skipna=True)
    med_coa  = df["CINSON"].median(skipna=True)

    c1,c2,c3,c4,c5,c6,c7,c8,c9 = st.columns(9)
    c1.metric("Institutions",          f"{total:,}")
    c2.metric("Total Enrollment",      f"{tot_enr:,}")
    c3.metric("Total Degrees Awarded", f"{tot_deg:,}")
    c4.metric("Median Accept Rate",    f"{med_adm:.1f}%" if not pd.isna(med_adm) else "N/A")
    c5.metric("Median Grad Rate 150%", f"{med_gr:.1f}%"  if not pd.isna(med_gr)  else "N/A")
    c6.metric("Median Retention (FT)", f"{med_ret:.1f}%" if not pd.isna(med_ret) else "N/A")
    c7.metric("Median 8-yr Award",     f"{med_om8:.1f}%" if not pd.isna(med_om8) else "N/A")
    c8.metric("Median Fac. Salary",    f"${med_sal:,.0f}" if not pd.isna(med_sal) else "N/A")
    c9.metric("Median In-State COA",   f"${med_coa:,.0f}" if not pd.isna(med_coa) else "N/A")

    st.divider()
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9 = st.tabs([
        "Map & Directory", "Enrollment & Demographics", "Admissions & Selectivity",
        "Graduation & Outcomes", "Costs & Financial Aid", "Faculty & Finance",
        "Completions & Degrees", "Institutional Finance", "Libraries",
    ])

    with tab1:
        # ── Variable intelligence: 5 curated map dimensions ───────────────────
        # Each chosen because it (a) varies meaningfully across geography,
        # (b) is policy-actionable, and (c) has broad IPEDS coverage.
        MAP_TIER_VARS = {
            "Grad Rate Tier": dict(
                col="GRRTTOT", slider_min=0, slider_max=100, slider_step=1, slider_fmt="%d%%",
                tier=lambda v: ("🟢 High (≥ 60%)" if v >= 60 else "🟡 Mid (40–59%)" if v >= 40 else "🔴 Low (< 40%)") if pd.notna(v) else "⚪ No data",
                colors={"🟢 High (≥ 60%)": "#059669", "🟡 Mid (40–59%)": "#D97706", "🔴 Low (< 40%)": "#DC2626", "⚪ No data": "#9CA3AF"},
                order=["🟢 High (≥ 60%)", "🟡 Mid (40–59%)", "🔴 Low (< 40%)", "⚪ No data"],
                why="**Grad Rate (150%)** is the single most-cited institutional effectiveness metric in higher ed policy. Geographic clustering is stark: Southern and rural states tend red; New England and selective private corridors run green. This is the closest thing to a universal report card for U.S. colleges.",
            ),
            "FT Retention Tier": dict(
                col="RET_PCF", slider_min=0, slider_max=100, slider_step=1, slider_fmt="%d%%",
                tier=lambda v: ("🟢 High (≥ 80%)" if v >= 80 else "🟡 Mid (60–79%)" if v >= 60 else "🔴 Low (< 60%)") if pd.notna(v) else "⚪ No data",
                colors={"🟢 High (≥ 80%)": "#059669", "🟡 Mid (60–79%)": "#D97706", "🔴 Low (< 60%)": "#DC2626", "⚪ No data": "#9CA3AF"},
                order=["🟢 High (≥ 80%)", "🟡 Mid (60–79%)", "🔴 Low (< 60%)", "⚪ No data"],
                why="**First-Year Retention** is the strongest *leading* indicator of eventual graduation — students who don't return for Year 2 almost never graduate. It's more actionable than grad rate because it captures problems *before* students leave, giving institutions a shorter feedback loop for intervention.",
            ),
            "Pell Access Tier": dict(
                col="PGRNT_P", slider_min=0, slider_max=100, slider_step=1, slider_fmt="%d%%",
                tier=lambda v: ("🟢 High Access (≥ 40%)" if v >= 40 else "🟡 Moderate (20–39%)" if v >= 20 else "🔴 Low Access (< 20%)") if pd.notna(v) else "⚪ No data",
                colors={"🟢 High Access (≥ 40%)": "#059669", "🟡 Moderate (20–39%)": "#D97706", "🔴 Low Access (< 20%)": "#DC2626", "⚪ No data": "#9CA3AF"},
                order=["🟢 High Access (≥ 40%)", "🟡 Moderate (20–39%)", "🔴 Low Access (< 20%)", "⚪ No data"],
                why="**Pell Recipient Share** is the best available proxy for socioeconomic access. Green clusters in urban cores, HBCUs, and community college corridors; red in high-wealth suburban and elite private regions. This map reveals which institutions are actually serving the students who need higher education most.",
            ),
            "Affordability Tier": dict(
                col="CINSON", slider_min=0, slider_max=90000, slider_step=500, slider_fmt="$%d",
                tier=lambda v: ("🟢 Affordable (< $25K)" if v < 25000 else "🟡 Moderate ($25K–$40K)" if v < 40000 else "🔴 Expensive (> $40K)") if pd.notna(v) else "⚪ No data",
                colors={"🟢 Affordable (< $25K)": "#059669", "🟡 Moderate ($25K–$40K)": "#D97706", "🔴 Expensive (> $40K)": "#DC2626", "⚪ No data": "#9CA3AF"},
                order=["🟢 Affordable (< $25K)", "🟡 Moderate ($25K–$40K)", "🔴 Expensive (> $40K)", "⚪ No data"],
                why="**In-State Cost of Attendance** shows powerful geographic clustering: expensive institutions concentrate on the coasts and in wealthier states. This map exposes the geography of college affordability — and why students in high-cost regions face fundamentally different access barriers than those in the Midwest or South.",
            ),
            "Selectivity Tier": dict(
                col="DVADM01", slider_min=0, slider_max=100, slider_step=1, slider_fmt="%d%%",
                tier=lambda v: ("🟢 Selective (< 30%)" if v < 30 else "🟡 Moderate (30–60%)" if v <= 60 else "🔴 Open (> 60%)") if pd.notna(v) else "⚪ No data",
                colors={"🟢 Selective (< 30%)": "#059669", "🟡 Moderate (30–60%)": "#D97706", "🔴 Open (> 60%)": "#DC2626", "⚪ No data": "#9CA3AF"},
                order=["🟢 Selective (< 30%)", "🟡 Moderate (30–60%)", "🔴 Open (> 60%)", "⚪ No data"],
                why="**Acceptance Rate** maps the geography of selectivity. Green (selective) institutions cluster heavily in the Northeast and coastal metros. Crucially, the majority of the country is open-access — a policy *strength*, not a weakness. This map challenges the assumption that college quality = selectivity.",
            ),
        }
        TIER_VAR_NAMES = list(MAP_TIER_VARS.keys())
        VIEW_OPTIONS   = ["Institution Type"] + TIER_VAR_NAMES

        # ── Controls row ──────────────────────────────────────────────────────
        mc1, mc2, mc3 = st.columns([2, 4, 2])
        with mc1:
            view_by = st.selectbox("View map by", VIEW_OPTIONS, key="map_view_by")
        active_tier_name = view_by if view_by != "Institution Type" else "Grad Rate Tier"
        tvar = MAP_TIER_VARS[active_tier_name]

        # Clear matrix click filter when the tier variable changes
        if st.session_state.get("_map_last_tier") != active_tier_name:
            st.session_state["map_matrix_sel"] = None
            st.session_state["_map_last_tier"] = active_tier_name

        with mc2:
            if view_by != "Institution Type":
                val_range = st.slider(
                    f"Filter by {active_tier_name.replace(' Tier','')} value",
                    tvar["slider_min"], tvar["slider_max"],
                    (tvar["slider_min"], tvar["slider_max"]),
                    step=tvar["slider_step"], format=tvar["slider_fmt"],
                    key=f"map_slider_{active_tier_name}",
                )
            else:
                val_range = None
        with mc3:
            incl_no_data = st.checkbox(
                "Include institutions with no data", value=True, key="map_incl_no_data"
            )

        # "Why this variable?" analyst note
        if view_by != "Institution Type":
            with st.expander(f"Why **{active_tier_name}**?", expanded=False):
                st.markdown(tvar["why"])

        # ── Build and filter map_df ───────────────────────────────────────────
        map_df = df.dropna(subset=["LATITUDE", "LONGITUD"]).copy()
        map_df = map_df[
            map_df["LATITUDE"].between(-90, 90) &
            map_df["LONGITUD"].between(-180, 180)
        ].copy()

        # Compute tier for the active variable (always, regardless of color mode)
        map_df["_tier"] = map_df[tvar["col"]].apply(tvar["tier"])

        # Apply value-range slider filter
        if val_range is not None:
            has_data = map_df[tvar["col"]].notna()
            in_range = map_df[tvar["col"]].between(val_range[0], val_range[1])
            map_df = map_df[(~has_data | in_range) if incl_no_data else (has_data & in_range)]
        elif not incl_no_data:
            map_df = map_df[map_df[tvar["col"]].notna()]

        map_df = map_df.reset_index(drop=True)

        # Save pre-matrix-filter snapshot for the matrix chart
        map_df_for_matrix = map_df.copy()

        # Apply matrix click filter (stored in session state)
        mat_filter = st.session_state.get("map_matrix_sel")
        if mat_filter:
            _mf_ctrl, _mf_tier = mat_filter
            map_df = map_df[
                (map_df["CONTROL_LBL"] == _mf_ctrl) & (map_df["_tier"] == _mf_tier)
            ].reset_index(drop=True)
            fcol1, fcol2 = st.columns([6, 1])
            fcol1.info(
                f"🔍 Matrix filter active: **{_mf_ctrl}** × **{_mf_tier}** "
                f"— {len(map_df):,} institutions shown"
            )
            if fcol2.button("✕ Clear", key="clear_mat_filter"):
                st.session_state["map_matrix_sel"] = None
                st.rerun()

        # ── Render map ────────────────────────────────────────────────────────
        if not map_df.empty:
            map_df["_enr"]     = map_df["ENRTOT"].apply(lambda x: f"{int(x):,}" if pd.notna(x) else "N/A")
            map_df["_adm"]     = map_df["DVADM01"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A")
            map_df["_gr"]      = map_df["GRRTTOT"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A")
            map_df["_ret"]     = map_df["RET_PCF"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A")
            map_df["_coa"]     = map_df["CINSON"].apply(lambda x: f"${x:,.0f}" if pd.notna(x) else "N/A")
            map_df["_sal"]     = map_df["SALTOTL"].apply(lambda x: f"${x:,.0f}" if pd.notna(x) else "N/A")
            map_df["_sfr"]     = map_df["STUFACR"].apply(lambda x: f"{x:.0f}:1" if pd.notna(x) else "N/A")
            map_df["_pell"]    = map_df["PGRNT_P"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A")
            map_df["_enrplot"] = map_df["ENRTOT"].fillna(50).clip(lower=50)

            lat_span = map_df["LATITUDE"].max() - map_df["LATITUDE"].min()
            lon_span = map_df["LONGITUD"].max() - map_df["LONGITUD"].min()
            span = max(lat_span, lon_span)
            if span > 30:   _map_zoom = 3
            elif span > 15: _map_zoom = 4
            elif span > 8:  _map_zoom = 5
            elif span > 4:  _map_zoom = 6
            elif span > 2:  _map_zoom = 7
            elif span > 0:  _map_zoom = 8
            else:           _map_zoom = 10
            _map_center = {"lat": float(map_df["LATITUDE"].mean()),
                           "lon": float(map_df["LONGITUD"].mean())}

            if view_by == "Institution Type":
                _color_col   = "CONTROL_LBL"
                _color_map   = CONTROL_COLORS
                _legend_title = "Control"
            else:
                _color_col   = "_tier"
                _color_map   = tvar["colors"]
                _legend_title = active_tier_name
                map_df["_tier"] = pd.Categorical(
                    map_df["_tier"], categories=tvar["order"], ordered=True
                )
                map_df = map_df.sort_values("_tier").reset_index(drop=True)

            fig = px.scatter_mapbox(
                map_df, lat="LATITUDE", lon="LONGITUD",
                color=_color_col, color_discrete_map=_color_map,
                hover_name="INSTNM",
                size="_enrplot", size_max=40,
                custom_data=["CITY","STABBR","SECTOR_LBL",
                             "_enr","_adm","_gr","_ret","_coa","_sal","_sfr","_pell",
                             "DISPLAY_NAME"],
                zoom=_map_zoom, center=_map_center,
                mapbox_style="open-street-map",
            )
            fig.update_traces(
                opacity=0.75,
                hovertemplate=(
                    "<b>%{hovertext}</b><br>"
                    "%{customdata[0]}, %{customdata[1]}<br>"
                    "<b>Sector:</b> %{customdata[2]}<br>"
                    "──────────────────<br>"
                    "<b>Enrollment:</b> %{customdata[3]}<br>"
                    "<b>Acceptance Rate:</b> %{customdata[4]}<br>"
                    "<b>Grad Rate (150%):</b> %{customdata[5]}<br>"
                    "<b>Retention Rate (FT):</b> %{customdata[6]}<br>"
                    "<b>In-State COA:</b> %{customdata[7]}<br>"
                    "<b>Avg Faculty Salary:</b> %{customdata[8]}<br>"
                    "<b>Student:Faculty Ratio:</b> %{customdata[9]}<br>"
                    "<b>% Receiving Pell:</b> %{customdata[10]}<br>"
                    "<extra></extra>"
                ),
                selector=dict(type="scattermapbox"),
            )

            # State abbreviation labels
            state_lbl = map_df.groupby("STABBR").agg(
                lat=("LATITUDE", "mean"), lon=("LONGITUD", "mean")
            ).reset_index()
            fig.add_trace(go.Scattermapbox(
                lat=state_lbl["lat"], lon=state_lbl["lon"],
                mode="text", text=state_lbl["STABBR"],
                textfont=dict(size=13, color="#222222", family="Arial Black"),
                hoverinfo="skip", showlegend=False, name="",
            ))

            # Focus institution star marker
            if alb_row is not None:
                try:
                    _alat = float(alb_row.get("LATITUDE"))
                    _alon = float(alb_row.get("LONGITUD"))
                    _alb_gr  = alb_row.get("GRRTTOT")
                    _alb_enr = alb_row.get("ENRTOT")
                    _fname = _focus_name()
                    _floc  = str(alb_row.get("STABBR") or "")
                    _alb_gr_str  = f"{float(_alb_gr):.1f}%"  if pd.notna(_alb_gr)  else "N/A"
                    _alb_enr_str = f"{int(float(_alb_enr)):,}" if pd.notna(_alb_enr) else "N/A"
                    if pd.notna(_alat) and pd.notna(_alon):
                        fig.add_trace(go.Scattermapbox(
                            lat=[_alat], lon=[_alon],
                            mode="markers+text",
                            marker=dict(size=24, color="#F59E0B", opacity=1),
                            text=[f"★ {_fname}"],
                            textposition="top right",
                            textfont=dict(size=13, color="#1E3A5F", family="Arial Black"),
                            name=f"★ {_fname}",
                            hovertemplate=(
                                f"<b>★ {_fname}</b><br>{_floc}<br>"
                                f"Grad Rate: {_alb_gr_str}<br>"
                                f"Enrollment: {_alb_enr_str}<br>"
                                "<extra></extra>"
                            ),
                            showlegend=True,
                        ))
                except (TypeError, ValueError):
                    pass

            _title_suffix = (
                f" · filter: {val_range[0]}–{val_range[1]}"
                + (tvar["slider_fmt"].replace("%d", "").replace("%%", "%"))
                if val_range else ""
            )
            fig.update_layout(
                height=620,
                margin=dict(l=0, r=0, t=36, b=0),
                legend=dict(title=_legend_title),
                title=f"<b>{len(map_df):,} institutions</b> · Hover for details · Click to select{_title_suffix}",
            )
            map_event = st.plotly_chart(fig, use_container_width=True,
                                        on_select="rerun", selection_mode="points")
            if map_event and map_event.selection and map_event.selection.points:
                pt = map_event.selection.points[0]
                cd = pt.get("customdata")
                inst_name = cd[11] if cd and len(cd) > 11 else None
                if inst_name:
                    bc1, bc2 = st.columns([4, 1])
                    bc1.success(f"Selected: **{inst_name.split(' (')[0]}**")
                    if bc2.button("View Profile →", type="primary", key="map_go"):
                        st.session_state["sel_inst"] = inst_name
                        st.session_state["page"] = "Institution Profile"
                        st.rerun()

        # ── Type × Tier matrix (clickable) + distribution bar ─────────────────
        st.divider()
        mat_col1, mat_col2 = st.columns([3, 2])
        with mat_col1:
            st.subheader(f"Institution Type × {active_tier_name}")
            st.caption(
                "**Click any cell to filter the map** to that institution type + tier combination. "
                "Matrix always reflects the slider filter, not the cell selection."
            )
            if not map_df_for_matrix.empty:
                _mat_grp = (
                    map_df_for_matrix.groupby(["CONTROL_LBL", "_tier"])
                    .size().reset_index(name="Count")
                )
                _mat_pivot = _mat_grp.pivot(
                    index="CONTROL_LBL", columns="_tier", values="Count"
                ).fillna(0).astype(int)
                present_tiers = [t for t in tvar["order"] if t in _mat_pivot.columns]
                _mat_pivot = _mat_pivot[present_tiers]

                # Build chart: heatmap for visuals + scatter overlay for reliable clicks
                fig_mat = go.Figure()
                fig_mat.add_trace(go.Heatmap(
                    z=_mat_pivot.values.tolist(),
                    x=list(_mat_pivot.columns),
                    y=list(_mat_pivot.index),
                    texttemplate="%{z}",
                    textfont=dict(size=14),
                    colorscale=[[0, "#F0FDF4"], [1, "#059669"]],
                    showscale=False,
                    hoverinfo="skip",
                    xgap=2, ygap=2,
                ))

                # Invisible scatter on each cell — these fire reliable on_select events
                _xs, _ys, _cdata, _htexts = [], [], [], []
                for _ctrl in _mat_pivot.index:
                    for _tier_lbl in _mat_pivot.columns:
                        _cnt = int(_mat_pivot.loc[_ctrl, _tier_lbl])
                        _xs.append(_tier_lbl)
                        _ys.append(_ctrl)
                        _cdata.append([_ctrl, _tier_lbl])
                        _htexts.append(
                            f"<b>{_ctrl}</b><br>{_tier_lbl}<br>"
                            f"Count: {_cnt}<br><i>Click to filter map</i>"
                        )
                fig_mat.add_trace(go.Scatter(
                    x=_xs, y=_ys, mode="markers",
                    marker=dict(size=40, opacity=0.01, symbol="square"),
                    customdata=_cdata,
                    hovertext=_htexts,
                    hovertemplate="%{hovertext}<extra></extra>",
                    showlegend=False, name="_click",
                ))

                fig_mat.update_layout(
                    height=280, margin=dict(l=0, r=0, t=10, b=0),
                    xaxis_title="", yaxis_title="",
                    plot_bgcolor="white",
                )
                mat_event = st.plotly_chart(
                    fig_mat, use_container_width=True,
                    on_select="rerun", selection_mode="points",
                    key="map_matrix_chart",
                )
                if mat_event and mat_event.selection and mat_event.selection.points:
                    _mp  = mat_event.selection.points[0]
                    _cd  = _mp.get("customdata")
                    if _cd and len(_cd) >= 2:
                        st.session_state["map_matrix_sel"] = (_cd[0], _cd[1])
                        st.rerun()

        with mat_col2:
            st.subheader("Tier Distribution")
            st.caption(f"Share of visible institutions by {active_tier_name.replace(' Tier', '').lower()} tier.")
            if not map_df_for_matrix.empty:
                _tc = map_df_for_matrix["_tier"].value_counts().reset_index()
                _tc.columns = ["Tier", "Count"]
                _tc["_ord"] = _tc["Tier"].apply(
                    lambda t: tvar["order"].index(t) if t in tvar["order"] else 99
                )
                _tc = _tc.sort_values("_ord").drop(columns="_ord")
                fig_tier = px.bar(
                    _tc, x="Count", y="Tier", orientation="h",
                    color="Tier", color_discrete_map=tvar["colors"],
                    text="Count",
                )
                fig_tier.update_traces(textposition="outside")
                fig_tier.update_layout(
                    height=280, showlegend=False,
                    margin=dict(l=0, r=50, t=10, b=0),
                    xaxis_title="", yaxis_title="",
                )
                st.plotly_chart(fig_tier, use_container_width=True)

        # ── Institution Directory ─────────────────────────────────────────────
        st.subheader("Institution Directory")
        st.caption("Click a row, then press **View Profile →** to open the institution profile.")
        dir_df = df[["DISPLAY_NAME","INSTNM","CITY","STABBR","SECTOR_LBL",
                      "CARNEGIE_LBL","INSTSIZE_LBL","OBEREG_LBL",
                      "ENRTOT","DVADM01","GRRTTOT","CINSON"]].rename(columns={
            "INSTNM":"Institution","CITY":"City","STABBR":"State",
            "SECTOR_LBL":"Sector","CARNEGIE_LBL":"Carnegie 2025",
            "INSTSIZE_LBL":"Size","OBEREG_LBL":"Region",
            "ENRTOT":"Enrollment",
            "DVADM01":"Accept %","GRRTTOT":"Grad Rate %","CINSON":"In-State COA"
        }).reset_index(drop=True)
        dir_event = st.dataframe(
            dir_df.drop(columns=["DISPLAY_NAME"]),
            use_container_width=True, height=400,
            on_select="rerun", selection_mode="single-row",
        )
        if dir_event and dir_event.selection and dir_event.selection.rows:
            row_idx = dir_event.selection.rows[0]
            inst_name = dir_df.iloc[row_idx]["DISPLAY_NAME"]
            dc1, dc2 = st.columns([4, 1])
            dc1.success(f"Selected: **{dir_df.iloc[row_idx]['Institution']}**")
            if dc2.button("View Profile →", type="primary", key="dir_go"):
                st.session_state["sel_inst"] = inst_name
                st.session_state["page"] = "Institution Profile"
                st.rerun()

    with tab2:
        c1, c2 = st.columns(2)
        with c1:
            ctrl_enr = df.groupby("CONTROL_LBL")["ENRTOT"].sum().reset_index()
            fig = px.pie(ctrl_enr, values="ENRTOT", names="CONTROL_LBL",
                         color="CONTROL_LBL", color_discrete_map=CONTROL_COLORS,
                         title="Total Enrollment by Control")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            sec_cnt = df.groupby("SECTOR_LBL").size().reset_index(name="Count")
            fig = px.bar(sec_cnt.sort_values("Count"), x="Count", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Institution Count by Sector")
            fig.update_layout(showlegend=False, height=400, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            race_cols = {"White":"PCTENRWH","Black/AA":"PCTENRBK","Hispanic":"PCTENRHS",
                         "Asian/PI":"PCTENRAP","AI/AN":"PCTENRAN","Two+":"PCTENR2M",
                         "Unknown":"PCTENRUN","Nonresident":"PCTENRNR"}
            race_med = {k: df[v].median(skipna=True) for k, v in race_cols.items()}
            race_df = pd.DataFrame(race_med.items(), columns=["Group","Median %"]).dropna()
            fig = px.bar(race_df.sort_values("Median %"), x="Median %", y="Group",
                         orientation="h", title="Median Race/Ethnicity Composition (%)")
            fig.update_layout(height=360, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            enr_df = df.groupby("SECTOR_LBL").agg(
                FT=("ENRFT","sum"), PT=("ENRPT","sum")
            ).reset_index().dropna()
            enr_long = enr_df.melt(id_vars="SECTOR_LBL", var_name="Status", value_name="Enrollment")
            fig = px.bar(enr_long, x="SECTOR_LBL", y="Enrollment", color="Status",
                         barmode="stack", title="FT vs PT Enrollment by Sector",
                         color_discrete_sequence=["#1f77b4","#aec7e8"],
                         labels={"SECTOR_LBL":""})
            fig.update_layout(height=360, xaxis_tickangle=-25)
            st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            de_med = {
                "Exclusively DE": df["PCTDEEXC"].median(skipna=True),
                "Some DE":        df["PCTDESOM"].median(skipna=True),
                "No DE":          df["PCTDENON"].median(skipna=True),
            }
            de_df2 = pd.DataFrame(de_med.items(), columns=["Mode","Median %"]).dropna()
            fig = px.pie(de_df2, values="Median %", names="Mode",
                         title="National Median: Distance Education Mode",
                         color_discrete_sequence=["#1f77b4","#ff7f0e","#2ca02c"])
            fig.update_layout(height=340)
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            ft1st = df.groupby("SECTOR_LBL")["PCTFT1ST"].median().reset_index().dropna()
            fig = px.bar(ft1st.sort_values("PCTFT1ST"), x="PCTFT1ST", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median % First-time Full-time Students by Sector",
                         labels={"PCTFT1ST":"Median % FT First-time","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            carn_cnt = (df[df["CARNEGIE_LBL"] != "Not classified"]
                        .groupby("CARNEGIE_LBL").size().reset_index(name="Count")
                        .sort_values("Count"))
            if not carn_cnt.empty:
                fig = px.bar(carn_cnt, x="Count", y="CARNEGIE_LBL", orientation="h",
                             title="Institution Count by Carnegie Classification 2025",
                             labels={"CARNEGIE_LBL": ""})
                fig.update_layout(height=500, showlegend=False, yaxis_title="",
                                  margin=dict(l=0, r=30, t=40, b=0))
                st.plotly_chart(fig, use_container_width=True)
        with c2:
            reg_cnt = (df[df["OBEREG_LBL"] != "Not available"]
                       .groupby("OBEREG_LBL").size().reset_index(name="Count")
                       .sort_values("Count"))
            if not reg_cnt.empty:
                fig = px.bar(reg_cnt, x="Count", y="OBEREG_LBL", orientation="h",
                             title="Institution Count by Geographic Region",
                             labels={"OBEREG_LBL": ""})
                fig.update_layout(height=500, showlegend=False, yaxis_title="",
                                  margin=dict(l=0, r=30, t=40, b=0))
                st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            sz_cnt = (df[df["INSTSIZE_LBL"] != "Not available"]
                      .groupby("INSTSIZE_LBL").size().reset_index(name="Count"))
            if not sz_cnt.empty:
                size_order = [
                    "Under 1,000", "1,000 - 4,999", "5,000 - 9,999",
                    "10,000 - 19,999", "20,000 and above",
                ]
                sz_cnt["_ord"] = sz_cnt["INSTSIZE_LBL"].apply(
                    lambda x: next((i for i, s in enumerate(size_order) if s.lower() in x.lower()), 99)
                )
                sz_cnt = sz_cnt.sort_values("_ord").drop(columns="_ord")
                fig = px.bar(sz_cnt, x="INSTSIZE_LBL", y="Count",
                             title="Institutions by Enrollment Size Category",
                             labels={"INSTSIZE_LBL": ""})
                fig.update_layout(height=360, showlegend=False,
                                  margin=dict(l=0, r=10, t=40, b=0))
                st.plotly_chart(fig, use_container_width=True)
        with c2:
            hl_cnt = (df[df["HLOFFER_LBL"] != "Not available"]
                      .groupby("HLOFFER_LBL").size().reset_index(name="Count")
                      .sort_values("Count"))
            if not hl_cnt.empty:
                fig = px.bar(hl_cnt, x="Count", y="HLOFFER_LBL", orientation="h",
                             title="Institutions by Highest Level Offered",
                             labels={"HLOFFER_LBL": ""})
                fig.update_layout(height=360, showlegend=False, yaxis_title="",
                                  margin=dict(l=0, r=30, t=40, b=0))
                st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Institution-level Enrollment Data")
        _inst_table(df[[
            "INSTNM","STABBR","SECTOR_LBL",
            "ENRTOT","EFUG","EFGRAD","ENRFT","ENRPT",
            "PCTENRW","PCTENRWH","PCTENRBK","PCTENRHS","PCTENRAP","PCTENRAN","PCTENRNR","PCTENR2M",
            "PCTDEEXC","PCTFT1ST",
        ]].rename(columns={
            "INSTNM":"Institution","STABBR":"State","SECTOR_LBL":"Sector",
            "ENRTOT":"Total Enr","EFUG":"UG","EFGRAD":"Grad",
            "ENRFT":"FT","ENRPT":"PT",
            "PCTENRW":"% Women","PCTENRWH":"% White","PCTENRBK":"% Black/AA",
            "PCTENRHS":"% Hispanic","PCTENRAP":"% Asian/PI","PCTENRAN":"% AI/AN",
            "PCTENRNR":"% Nonresident","PCTENR2M":"% Two+",
            "PCTDEEXC":"% Excl. DE","PCTFT1ST":"% FT First-time",
        }), sort_col="Total Enr")

    with tab3:  # Admissions & Selectivity
        c1, c2 = st.columns(2)
        with c1:
            adm_df = df.dropna(subset=["DVADM01"]).groupby("SECTOR_LBL")["DVADM01"].median().reset_index()
            fig = px.bar(adm_df.sort_values("DVADM01"), x="DVADM01", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Acceptance Rate by Sector",
                         labels={"DVADM01":"Median Accept %","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            _add_albion_vline(fig, alb_row, "DVADM01")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            yield_df = df.dropna(subset=["DVADM04"]).groupby("SECTOR_LBL")["DVADM04"].median().reset_index()
            fig = px.bar(yield_df.sort_values("DVADM04"), x="DVADM04", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Yield Rate by Sector",
                         labels={"DVADM04":"Median Yield %","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            _add_albion_vline(fig, alb_row, "DVADM04")
            st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            adm_hist = df["DVADM01"].dropna()
            fig = px.histogram(adm_hist, x="DVADM01", nbins=40,
                               title="Distribution of Acceptance Rates (all institutions)",
                               labels={"DVADM01":"Acceptance Rate (%)"})
            fig.update_layout(height=340, showlegend=False)
            _add_albion_vline(fig, alb_row, "DVADM01")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            # Open admission breakdown
            open_adm_counts = {
                "Selective":     int((df["DVADM01"].notna()).sum()),
                "No admit data": int((df["DVADM01"].isna()).sum()),
            }
            oa_df = pd.DataFrame(open_adm_counts.items(), columns=["Type","Count"])
            fig = px.pie(oa_df, values="Count", names="Type",
                         title="Institutions Reporting Acceptance Rate",
                         color_discrete_sequence=["#1f77b4","#aaaaaa"])
            fig.update_layout(height=340)
            st.plotly_chart(fig, use_container_width=True)

        # Gender gap in acceptance rates
        adm_gender = df.dropna(subset=["DVADM02","DVADM03"]).groupby("SECTOR_LBL").agg(
            Men=("DVADM02","median"), Women=("DVADM03","median")
        ).reset_index()
        if not adm_gender.empty:
            ag_long = adm_gender.melt(id_vars="SECTOR_LBL", var_name="Gender", value_name="Accept %")
            fig = px.bar(ag_long, x="SECTOR_LBL", y="Accept %", color="Gender",
                         barmode="group", title="Median Acceptance Rate by Gender & Sector",
                         color_discrete_sequence=["#1f77b4","#e377c2"],
                         labels={"SECTOR_LBL":""})
            fig.update_layout(height=360, xaxis_tickangle=-25)
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Institution-level Admissions Data")
        _inst_table(df[[
            "INSTNM","STABBR","SECTOR_LBL",
            "DVADM01","DVADM02","DVADM03",
            "DVADM04","DVADM05","DVADM06",
            "DVADM07","DVADM08","DVADM11","DVADM12",
        ]].rename(columns={
            "INSTNM":"Institution","STABBR":"State","SECTOR_LBL":"Sector",
            "DVADM01":"Accept Rate %","DVADM02":"Accept (Men) %","DVADM03":"Accept (Women) %",
            "DVADM04":"Yield Rate %","DVADM05":"Yield (Men) %","DVADM06":"Yield (Women) %",
            "DVADM07":"Yield FT Men %","DVADM08":"Yield FT Women %",
            "DVADM11":"Yield FT Total %","DVADM12":"Yield PT Total %",
        }), sort_col="Accept Rate %", ascending=True)

    with tab4:  # Graduation & Outcomes
        # Retention rates — top row
        st.subheader("Retention Rates")
        c1, c2 = st.columns(2)
        with c1:
            ret_df = df.dropna(subset=["RET_PCF"]).groupby("SECTOR_LBL")["RET_PCF"].median().reset_index()
            fig = px.bar(ret_df.sort_values("RET_PCF"), x="RET_PCF", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Full-time Retention Rate by Sector",
                         labels={"RET_PCF":"Median FT Retention %","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="",
                              xaxis_range=[0, 100])
            _add_albion_vline(fig, alb_row, "RET_PCF")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            ret_pt = df.dropna(subset=["RET_PCP"]).groupby("SECTOR_LBL")["RET_PCP"].median().reset_index()
            fig = px.bar(ret_pt.sort_values("RET_PCP"), x="RET_PCP", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Part-time Retention Rate by Sector",
                         labels={"RET_PCP":"Median PT Retention %","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="",
                              xaxis_range=[0, 100])
            st.plotly_chart(fig, use_container_width=True)

        # FT vs PT retention by control
        ret_ctrl = df.dropna(subset=["RET_PCF","RET_PCP"]).groupby("CONTROL_LBL").agg(
            FullTime=("RET_PCF","median"), PartTime=("RET_PCP","median")
        ).reset_index()
        if not ret_ctrl.empty:
            rc_long = ret_ctrl.melt(id_vars="CONTROL_LBL", var_name="Status", value_name="Retention %")
            fig = px.bar(rc_long, x="CONTROL_LBL", y="Retention %", color="Status",
                         barmode="group", title="Median Retention Rate: FT vs PT by Control",
                         color_discrete_sequence=["#1f77b4","#aec7e8"],
                         labels={"CONTROL_LBL":""})
            fig.update_layout(height=340, yaxis_range=[0, 100])
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Graduation Rates")
        c1, c2 = st.columns(2)
        with c1:
            gr_df = df.dropna(subset=["GRRTTOT"]).groupby("SECTOR_LBL")["GRRTTOT"].median().reset_index()
            fig = px.bar(gr_df.sort_values("GRRTTOT"), x="GRRTTOT", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Graduation Rate (150%) by Sector",
                         labels={"GRRTTOT":"Median Grad Rate (%)","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            _add_albion_vline(fig, alb_row, "GRRTTOT")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            om_df = df.dropna(subset=["OM1TOTLAWDP8"]).groupby("SECTOR_LBL")["OM1TOTLAWDP8"].median().reset_index()
            fig = px.bar(om_df.sort_values("OM1TOTLAWDP8"), x="OM1TOTLAWDP8", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median 8-Year Award Rate (FT First-time) by Sector",
                         labels={"OM1TOTLAWDP8":"Median Award % at 8 Yrs","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)

        # Pell gap: overall vs Pell grad rate
        pell_gap = df.dropna(subset=["GRRTTOT","PGGRRTT"]).groupby("CONTROL_LBL").agg(
            Overall=("GRRTTOT","median"), Pell=("PGGRRTT","median")
        ).reset_index()
        if not pell_gap.empty:
            pg_long = pell_gap.melt(id_vars="CONTROL_LBL", var_name="Group", value_name="Grad Rate %")
            fig = px.bar(pg_long, x="CONTROL_LBL", y="Grad Rate %", color="Group",
                         barmode="group", title="Overall vs Pell Recipient Graduation Rate (150%) by Control",
                         color_discrete_sequence=["#1f77b4","#2ca02c"],
                         labels={"CONTROL_LBL":""})
            fig.update_layout(height=360, yaxis_range=[0, 100])
            st.plotly_chart(fig, use_container_width=True)

        # National race/ethnicity grad rate medians
        c1, c2 = st.columns(2)
        with c1:
            race_gr = {
                "White":       df["GRRTWH"].median(skipna=True),
                "Black/AA":    df["GRRTBK"].median(skipna=True),
                "Hispanic":    df["GRRTHS"].median(skipna=True),
                "Asian/PI":    df["GRRTAP"].median(skipna=True),
                "AI/AN":       df["GRRTAN"].median(skipna=True),
                "Two+ races":  df["GRRT2M"].median(skipna=True),
            }
            rg_df = pd.DataFrame([(k, v) for k, v in race_gr.items() if pd.notna(v)],
                                  columns=["Race/Ethnicity","Median Grad Rate %"])
            fig = px.bar(rg_df.sort_values("Median Grad Rate %"), x="Median Grad Rate %", y="Race/Ethnicity",
                         orientation="h", color="Race/Ethnicity",
                         title="National Median 150% Grad Rate by Race/Ethnicity",
                         color_discrete_sequence=px.colors.qualitative.Set2)
            fig.update_layout(height=360, showlegend=False, xaxis_range=[0,100], yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Institution-level Graduation & Outcomes Data")
        _inst_table(df[[
            "INSTNM","STABBR","SECTOR_LBL",
            "RET_PCF","RET_PCP",
            "GRRTTOT","GRRTM","GRRTW","GBA4RTT","GBA5RTT","GBA6RTT",
            "PGGRRTT","SSGRRTT","NRGRRTT","TRRTTOT",
            "OM1TOTLAWDP8","OM2TOTLAWDP8","OM1PELLAWDP8","OM1NPELAWDP8",
        ]].rename(columns={
            "INSTNM":"Institution","STABBR":"State","SECTOR_LBL":"Sector",
            "RET_PCF":"FT Retention %","RET_PCP":"PT Retention %",
            "GRRTTOT":"Grad Rate 150% %","GRRTM":"GR 150% Men %","GRRTW":"GR 150% Women %",
            "GBA4RTT":"Bach 4-yr %","GBA5RTT":"Bach 5-yr %","GBA6RTT":"Bach 6-yr %",
            "PGGRRTT":"Pell Grad %","SSGRRTT":"Sub-Loan Grad %","NRGRRTT":"Neither Grad %",
            "TRRTTOT":"Transfer-out %",
            "OM1TOTLAWDP8":"FT Award 8yr %","OM2TOTLAWDP8":"PT Award 8yr %",
            "OM1PELLAWDP8":"Pell Award 8yr %","OM1NPELAWDP8":"Non-Pell Award 8yr %",
        }), sort_col="Grad Rate 150% %")

    with tab5:  # Costs & Financial Aid
        c1, c2 = st.columns(2)
        with c1:
            cost_df = df.dropna(subset=["CINSON"]).groupby("SECTOR_LBL")["CINSON"].median().reset_index()
            fig = px.bar(cost_df.sort_values("CINSON"), x="CINSON", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median In-State COA by Sector",
                         labels={"CINSON":"Median COA ($)","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            _add_albion_vline(fig, alb_row, "CINSON")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            oos_cost = df.dropna(subset=["COTSON"]).groupby("SECTOR_LBL")["COTSON"].median().reset_index()
            fig = px.bar(oos_cost.sort_values("COTSON"), x="COTSON", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Out-of-State COA by Sector",
                         labels={"COTSON":"Median OOS COA ($)","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            aid_df = df.groupby("CONTROL_LBL").agg(
                Pell=("PGRNT_P","median"), Loans=("LOAN_P","median"), AnyAid=("ANYAIDP","median"),
            ).reset_index()
            aid_long = aid_df.melt(id_vars="CONTROL_LBL", var_name="Aid Type", value_name="Median %")
            fig = px.bar(aid_long, x="CONTROL_LBL", y="Median %", color="Aid Type", barmode="group",
                         title="Median % Receiving Aid by Control",
                         labels={"CONTROL_LBL": ""})
            fig.update_layout(height=360, xaxis_title="")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            avg_aid = df.groupby("SECTOR_LBL").agg(
                AvgPell=("PGRNT_A","median"), AvgGrant=("AGRNT_A","median"),
            ).reset_index().dropna()
            aa_long = avg_aid.melt(id_vars="SECTOR_LBL", var_name="Aid Type", value_name="Avg Amount ($)")
            fig = px.bar(aa_long, x="SECTOR_LBL", y="Avg Amount ($)", color="Aid Type",
                         barmode="group", title="Median Avg Aid Amount by Sector",
                         color_discrete_sequence=["#2ca02c","#ff7f0e"],
                         labels={"SECTOR_LBL":""})
            fig.update_layout(height=360, xaxis_tickangle=-25)
            fig.update_yaxes(tickprefix="$", tickformat=",.0f")
            st.plotly_chart(fig, use_container_width=True)

        # Tuition distribution
        tui_hist = df["TUFEYR3"].dropna()
        if not tui_hist.empty:
            fig = px.histogram(tui_hist, x="TUFEYR3", nbins=50,
                               title=f"Distribution of Tuition & Fees {year} (all institutions)",
                               labels={"TUFEYR3":"Tuition & Fees ($)"})
            fig.update_layout(height=320, showlegend=False)
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            _add_albion_vline(fig, alb_row, "TUFEYR3")
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Institution-level Costs & Financial Aid Data")
        _inst_table(df[[
            "INSTNM", "STABBR", "SECTOR_LBL",
            "CINSON", "COTSON", "TUFEYR3",
            "ANYAIDP", "PGRNT_P", "PGRNT_A",
            "AGRNT_P", "AGRNT_A", "LOAN_P", "LOAN_A",
        ]].rename(columns={
            "INSTNM": "Institution", "STABBR": "State", "SECTOR_LBL": "Sector",
            "CINSON": "In-State COA ($)", "COTSON": "OOS COA ($)", "TUFEYR3": f"Tuition {year} ($)",
            "ANYAIDP": "% Any Aid", "PGRNT_P": "% Pell", "PGRNT_A": "Avg Pell ($)",
            "AGRNT_P": "% Any Grant", "AGRNT_A": "Avg Grant ($)",
            "LOAN_P": "% Loans", "LOAN_A": "Avg Loan ($)",
        }), sort_col="In-State COA ($)")

    with tab6:  # Faculty & Finance
        c1, c2 = st.columns(2)
        with c1:
            sal_df = df.dropna(subset=["SALTOTL"]).groupby("SECTOR_LBL")["SALTOTL"].median().reset_index()
            fig = px.bar(sal_df.sort_values("SALTOTL"), x="SALTOTL", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Avg Faculty Salary by Sector",
                         labels={"SALTOTL":"Median Avg Salary ($)","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            _add_albion_vline(fig, alb_row, "SALTOTL")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            sfr_df = df.dropna(subset=["STUFACR"]).groupby("SECTOR_LBL")["STUFACR"].median().reset_index()
            fig = px.bar(sfr_df.sort_values("STUFACR"), x="STUFACR", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Student-to-Faculty Ratio by Sector",
                         labels={"STUFACR":"Median Student:Faculty","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            _add_albion_vline(fig, alb_row, "STUFACR")
            st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            fte_sec = df.dropna(subset=["SFTETOTL"]).groupby("SECTOR_LBL")["SFTETOTL"].median().reset_index()
            fig = px.bar(fte_sec.sort_values("SFTETOTL"), x="SFTETOTL", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Total FTE Staff by Sector",
                         labels={"SFTETOTL":"Median FTE Staff","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            # National FTE by occupational category (sum)
            fte_cat = {
                "Instruction":    df["SFTEINST"].sum(skipna=True),
                "Research":       df["SFTERSRC"].sum(skipna=True),
                "Public Service": df["SFTEPBSV"].sum(skipna=True),
                "Management":     df["SFTEMNGM"].sum(skipna=True),
                "Business & Fin": df["SFTEBFO"].sum(skipna=True),
                "Comp/Eng/Sci":   df["SFTECES"].sum(skipna=True),
                "Healthcare":     df["SFTEHLTH"].sum(skipna=True),
                "Service":        df["SFTESRVC"].sum(skipna=True),
                "Office & Admin": df["SFTEOFAS"].sum(skipna=True),
            }
            fc_df = pd.DataFrame(fte_cat.items(), columns=["Category","Total FTE"])
            fc_df = fc_df[fc_df["Total FTE"] > 0].sort_values("Total FTE", ascending=True)
            fig = px.bar(fc_df, x="Total FTE", y="Category", orientation="h",
                         color_discrete_sequence=["#17becf"],
                         title="Total National FTE by Occupational Category")
            fig.update_layout(height=360, showlegend=False, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)

        sal_hist = df["SALTOTL"].dropna()
        if not sal_hist.empty:
            fig = px.histogram(sal_hist, x="SALTOTL", nbins=40,
                               title="Distribution of Avg Faculty Salaries (all institutions)",
                               labels={"SALTOTL":"Avg Faculty Salary ($)"})
            fig.update_layout(height=320, showlegend=False)
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Institution-level Faculty & Staffing Data")
        _inst_table(df[[
            "INSTNM", "STABBR", "SECTOR_LBL",
            "SALTOTL", "STUFACR",
            "SFTETOTL", "SFTEINST", "SFTERSRC", "SFTEPBSV",
            "SFTEMNGM", "SFTEBFO", "SFTECES", "SFTEHLTH", "SFTESRVC", "SFTEOFAS",
        ]].rename(columns={
            "INSTNM": "Institution", "STABBR": "State", "SECTOR_LBL": "Sector",
            "SALTOTL": "Avg Faculty Salary ($)", "STUFACR": "Student:Faculty Ratio",
            "SFTETOTL": "Total FTE Staff", "SFTEINST": "Instructional FTE",
            "SFTERSRC": "Research FTE", "SFTEPBSV": "Public Service FTE",
            "SFTEMNGM": "Management FTE", "SFTEBFO": "Business/Fin FTE",
            "SFTECES": "Comp/Eng/Sci FTE", "SFTEHLTH": "Healthcare FTE",
            "SFTESRVC": "Service FTE", "SFTEOFAS": "Office/Admin FTE",
        }), sort_col="Avg Faculty Salary ($)")

    with tab7:  # Completions & Degrees
        # Total degrees by type (national aggregate)
        deg_totals = {
            "Bachelor's":           int(df["BASDEG"].sum(skipna=True)),
            "Master's":             int(df["MASDEG"].sum(skipna=True)),
            "Doctor's - Research":  int(df["DOCDEGRS"].sum(skipna=True)),
            "Doctor's - Prof.":     int(df["DOCDEGPP"].sum(skipna=True)),
            "Doctor's - Other":     int(df["DOCDEGOT"].sum(skipna=True)),
            "Associate's":          int(df["ASCDEG"].sum(skipna=True)),
            "Cert ≥1yr, <4yr":      int(df["CERT4"].sum(skipna=True)),
            "Cert ≥1yr, <2yr":      int(df["CERT2"].sum(skipna=True)),
            "Cert ≥12wk, <1yr":     int(df["CERT1B"].sum(skipna=True)),
            "Cert <12 weeks":       int(df["CERT1A"].sum(skipna=True)),
        }
        dt_df = pd.DataFrame(deg_totals.items(), columns=["Award Level","Total Awarded"])
        dt_df = dt_df[dt_df["Total Awarded"] > 0].sort_values("Total Awarded")

        c1, c2 = st.columns(2)
        with c1:
            fig = px.bar(dt_df, x="Total Awarded", y="Award Level", orientation="h",
                         color_discrete_sequence=["#17becf"],
                         title=f"Total Awards Conferred by Level ({year})")
            fig.update_layout(height=400, showlegend=False, yaxis_title="")
            fig.update_xaxes(tickformat=",.0f")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            fig = px.pie(dt_df, values="Total Awarded", names="Award Level",
                         title="Share of Awards by Level")
            fig.update_layout(height=400)
            st.plotly_chart(fig, use_container_width=True)

        # Bachelor's awarded by sector
        c1, c2 = st.columns(2)
        with c1:
            ba_sec = df.groupby("SECTOR_LBL")["BASDEG"].sum().reset_index().dropna()
            ba_sec = ba_sec[ba_sec["BASDEG"] > 0].sort_values("BASDEG")
            fig = px.bar(ba_sec, x="BASDEG", y="SECTOR_LBL", orientation="h",
                         color="SECTOR_LBL", color_discrete_map=SECTOR_COLORS,
                         title="Total Bachelor's Degrees Awarded by Sector",
                         labels={"BASDEG":"Bachelor's Degrees","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            ma_sec = df.groupby("SECTOR_LBL")["MASDEG"].sum().reset_index().dropna()
            ma_sec = ma_sec[ma_sec["MASDEG"] > 0].sort_values("MASDEG")
            fig = px.bar(ma_sec, x="MASDEG", y="SECTOR_LBL", orientation="h",
                         color="SECTOR_LBL", color_discrete_map=SECTOR_COLORS,
                         title="Total Master's Degrees Awarded by Sector",
                         labels={"MASDEG":"Master's Degrees","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)

        # Median degrees per institution by sector
        med_deg = df.groupby("SECTOR_LBL").agg(
            Bach=("BASDEG","median"), Masters=("MASDEG","median"), Assoc=("ASCDEG","median")
        ).reset_index().dropna(how="all", subset=["Bach","Masters","Assoc"])
        if not med_deg.empty:
            md_long = med_deg.melt(id_vars="SECTOR_LBL", var_name="Level", value_name="Median Awarded")
            fig = px.bar(md_long, x="SECTOR_LBL", y="Median Awarded", color="Level",
                         barmode="group", title="Median Degrees Awarded per Institution by Sector",
                         labels={"SECTOR_LBL": ""},
                         color_discrete_sequence=px.colors.qualitative.Set2)
            fig.update_layout(height=380, xaxis_tickangle=-25)
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Institution-level Completions & Degrees Data")
        _inst_table(df[[
            "INSTNM", "STABBR", "SECTOR_LBL",
            "BASDEG", "MASDEG", "ASCDEG",
            "DOCDEGRS", "DOCDEGPP", "DOCDEGOT",
            "CERT4", "CERT2", "CERT1B", "CERT1A",
        ]].rename(columns={
            "INSTNM": "Institution", "STABBR": "State", "SECTOR_LBL": "Sector",
            "BASDEG": "Bachelor's", "MASDEG": "Master's", "ASCDEG": "Associate's",
            "DOCDEGRS": "Doctorate (Research)", "DOCDEGPP": "Doctorate (Prof.)", "DOCDEGOT": "Doctorate (Other)",
            "CERT4": "Cert ≥1yr <4yr", "CERT2": "Cert ≥1yr <2yr",
            "CERT1B": "Cert ≥12wk <1yr", "CERT1A": "Cert <12wk",
        }), sort_col="Bachelor's")

    with tab8:  # Institutional Finance
        st.caption(f"Finance data source: DRVF{year[:4]}. Public = GASB (F1); Private NP = FASB (F2).")
        enr_pos = df["ENRTOT"].notna() & (df["ENRTOT"].fillna(0) > 0)
        pub = (df["CONTROL"] == 1) & enr_pos
        prv = (df["CONTROL"] == 2) & enr_pos
        rev_per = pd.Series(float("nan"), index=df.index, dtype="float64")
        rev_per[pub & df["F1CORREV"].notna()] = (df["F1CORREV"] / df["ENRTOT"])[pub & df["F1CORREV"].notna()]
        rev_per[prv & df["F2CORREV"].notna()] = (df["F2CORREV"] / df["ENRTOT"])[prv & df["F2CORREV"].notna()]
        exp_per = pd.Series(float("nan"), index=df.index, dtype="float64")
        exp_per[pub & df["F1COREXP"].notna()] = (df["F1COREXP"] / df["ENRTOT"])[pub & df["F1COREXP"].notna()]
        exp_per[prv & df["F2COREXP"].notna()] = (df["F2COREXP"] / df["ENRTOT"])[prv & df["F2COREXP"].notna()]

        c1, c2 = st.columns(2)
        with c1:
            tmp = df.copy(); tmp["_rps"] = rev_per
            rev_df = tmp.dropna(subset=["_rps"]).groupby("CONTROL_LBL")["_rps"].median().reset_index()
            fig = px.bar(rev_df.sort_values("_rps"), x="_rps", y="CONTROL_LBL",
                         orientation="h", color="CONTROL_LBL",
                         color_discrete_map=CONTROL_COLORS,
                         title="Median Core Revenue per Student by Control",
                         labels={"_rps":"Median Revenue/Student ($)","CONTROL_LBL":""})
            fig.update_layout(showlegend=False, height=300, yaxis_title="")
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            tmp2 = df.copy(); tmp2["_eps"] = exp_per
            exp_df = tmp2.dropna(subset=["_eps"]).groupby("CONTROL_LBL")["_eps"].median().reset_index()
            fig = px.bar(exp_df.sort_values("_eps"), x="_eps", y="CONTROL_LBL",
                         orientation="h", color="CONTROL_LBL",
                         color_discrete_map=CONTROL_COLORS,
                         title="Median Core Expenditure per Student by Control",
                         labels={"_eps":"Median Expense/Student ($)","CONTROL_LBL":""})
            fig.update_layout(showlegend=False, height=300, yaxis_title="")
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            st.plotly_chart(fig, use_container_width=True)

        # Total national revenue and expenses
        pub_rev = df[df["CONTROL"]==1]["F1CORREV"].sum(skipna=True)
        pub_exp = df[df["CONTROL"]==1]["F1COREXP"].sum(skipna=True)
        prv_rev = df[df["CONTROL"]==2]["F2CORREV"].sum(skipna=True)
        prv_exp = df[df["CONTROL"]==2]["F2COREXP"].sum(skipna=True)
        fin_summary = pd.DataFrame([
            {"Sector":"Public", "Category":"Core Revenue",    "Amount ($B)": pub_rev/1e9},
            {"Sector":"Public", "Category":"Core Expenses",   "Amount ($B)": pub_exp/1e9},
            {"Sector":"Private NP","Category":"Core Revenue", "Amount ($B)": prv_rev/1e9},
            {"Sector":"Private NP","Category":"Core Expenses","Amount ($B)": prv_exp/1e9},
        ]).dropna()
        if not fin_summary.empty:
            fig = px.bar(fin_summary, x="Sector", y="Amount ($B)", color="Category",
                         barmode="group", title="Total National Core Revenue vs Expenses ($B)",
                         color_discrete_sequence=["#1f77b4","#d62728"])
            fig.update_layout(height=360)
            fig.update_yaxes(tickprefix="$", ticksuffix="B")
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Institution-level Finance Data")
        st.caption("Public institutions use GASB (F1CORREV/F1COREXP); Private NP use FASB (F2CORREV/F2COREXP). Non-applicable cells will be blank.")
        _inst_table(df[[
            "INSTNM", "STABBR", "CONTROL_LBL", "SECTOR_LBL",
            "F1CORREV", "F1COREXP",
            "F2CORREV", "F2COREXP",
        ]].rename(columns={
            "INSTNM": "Institution", "STABBR": "State",
            "CONTROL_LBL": "Control", "SECTOR_LBL": "Sector",
            "F1CORREV": "Core Revenue $ (Public)", "F1COREXP": "Core Expense $ (Public)",
            "F2CORREV": "Core Revenue $ (Priv NP)", "F2COREXP": "Core Expense $ (Priv NP)",
        }), sort_col="Core Revenue $ (Public)")

    with tab9:  # Libraries
        st.caption(f"Academic Library data source: DRVAL{year[:4]} (per-FTE metrics).")
        c1, c2 = st.columns(2)
        with c1:
            lib_exp = df.dropna(subset=["LEXPTOTF"]).groupby("SECTOR_LBL")["LEXPTOTF"].median().reset_index()
            fig = px.bar(lib_exp.sort_values("LEXPTOTF"), x="LEXPTOTF", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Library Expenditures per FTE Student by Sector",
                         labels={"LEXPTOTF":"Median Expend/FTE ($)","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            _add_albion_vline(fig, alb_row, "LEXPTOTF")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            lib_ebooks = df.dropna(subset=["LEBOOKSP"]).groupby("SECTOR_LBL")["LEBOOKSP"].median().reset_index()
            fig = px.bar(lib_ebooks.sort_values("LEBOOKSP"), x="LEBOOKSP", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median E-Books per FTE Student by Sector",
                         labels={"LEBOOKSP":"Median E-Books/FTE","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            lib_books = df.dropna(subset=["LPBOOKSP"]).groupby("CONTROL_LBL")["LPBOOKSP"].median().reset_index()
            fig = px.bar(lib_books.sort_values("LPBOOKSP"), x="LPBOOKSP", y="CONTROL_LBL",
                         orientation="h", color="CONTROL_LBL",
                         color_discrete_map=CONTROL_COLORS,
                         title="Median Physical Books per FTE by Control",
                         labels={"LPBOOKSP":"Physical Books/FTE","CONTROL_LBL":""})
            fig.update_layout(showlegend=False, height=300, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            lib_fte = df.dropna(subset=["LIBLFTE"]).groupby("SECTOR_LBL")["LIBLFTE"].median().reset_index()
            fig = px.bar(lib_fte.sort_values("LIBLFTE"), x="LIBLFTE", y="SECTOR_LBL",
                         orientation="h", color="SECTOR_LBL",
                         color_discrete_map=SECTOR_COLORS,
                         title="Median Library Staff FTE by Sector",
                         labels={"LIBLFTE":"Library Staff FTE","SECTOR_LBL":""})
            fig.update_layout(showlegend=False, height=360, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Institution-level Library Data")
        _inst_table(df[[
            "INSTNM", "STABBR", "SECTOR_LBL",
            "LEXPTOTF", "LEBOOKSP", "LPBOOKSP", "LIBLFTE",
        ]].rename(columns={
            "INSTNM": "Institution", "STABBR": "State", "SECTOR_LBL": "Sector",
            "LEXPTOTF": "Expend/FTE ($)", "LEBOOKSP": "E-Books/FTE",
            "LPBOOKSP": "Physical Books/FTE", "LIBLFTE": "Library Staff FTE",
        }), sort_col="Expend/FTE ($)")

    st.divider()
    st.subheader("Scatter Explorer")
    _scatter_explorer(df)


def _scatter_albion_insight(sc_df, albion, x_col, y_col, z_col,
                            x_var, y_var, z_var, chart_idx: int):
    """Render a data-driven Albion insight block beneath a scatter chart."""
    if albion.empty:
        st.info("Albion College does not have data for all three variables in this chart.")
        return

    ar    = albion.iloc[0]
    alb_x = float(ar[x_col])
    alb_y = float(ar[y_col])
    alb_z_raw = ar[z_col]
    alb_z = float(alb_z_raw) if pd.notna(alb_z_raw) else None
    n     = len(sc_df)

    x_pct = round((sc_df[x_col] < alb_x).sum() / n * 100)
    y_pct = round((sc_df[y_col] < alb_y).sum() / n * 100)
    above_x = alb_x > sc_df[x_col].median()
    above_y = alb_y > sc_df[y_col].median()

    if chart_idx == 0:  # Selectivity → Outcomes
        sel_pct = 100 - x_pct   # % schools less selective (higher accept rate) than Albion
        gr_pct  = y_pct
        if above_x and above_y:
            quad = ("**upper-right — relatively open admissions combined with above-average graduation rates.** "
                    "This is the most valuable position in this chart: Albion admits broadly yet graduates its "
                    "students at competitive rates. This \"high-access, high-success\" profile is a genuine "
                    "strength worth centering in enrollment marketing and institutional storytelling.")
        elif not above_x and above_y:
            quad = ("**upper-left — more selective than average, with above-average graduation rates.** "
                    "This pattern is expected for selective institutions, which benefit from admitting already-prepared "
                    "students. The strategic question is whether Albion's outcomes reflect genuine educational "
                    "value-add or primarily a pre-selected student advantage.")
        elif above_x and not above_y:
            quad = ("**lower-right — less selective than average, with below-median graduation rates.** "
                    "This is the most common pattern among open-access institutions. The strategic opportunity "
                    "is targeted student success investment: structured advising, early-alert systems, and "
                    "proactive financial aid can move Albion up this chart without restricting access.")
        else:
            quad = ("**lower-left — more selective than average, yet graduation rates fall below the median.** "
                    "This is a critical diagnostic: selectivity should predict strong outcomes. "
                    "Underperformance despite selectivity warrants close examination of the post-enrollment "
                    "student experience, academic support, and retention infrastructure.")
        st.markdown(
            f"#### 📍 Albion's Position — {x_var} × {y_var}\n\n"
            f"Albion's **acceptance rate of {alb_x:.1f}%** means {sel_pct}% of the {n:,} plotted schools "
            f"are *less* selective (Albion is more selective than {100 - sel_pct}% of peers). "
            f"Its **graduation rate of {alb_y:.1f}%** ranks at the **{gr_pct}th percentile**, "
            f"outperforming {gr_pct}% of institutions in this view.\n\n"
            f"Albion sits in the {quad}"
        )

    elif chart_idx == 1:  # Cost → Equity
        cost_pct = 100 - x_pct   # % with lower COA (more affordable)
        pell_pct = y_pct
        z_fmt = f"${alb_z:,.0f}" if alb_z else "N/A"
        if above_x and above_y:
            quad = ("**upper-right — high sticker price paired with meaningful Pell enrollment.** "
                    f"This signals intentional commitment to access despite high costs. "
                    f"Albion's average Pell grant of **{z_fmt}** is the key number here: a large grant "
                    f"at high COA can make Albion genuinely affordable for low-income students despite the sticker price.")
        elif above_x and not above_y:
            quad = ("**lower-right — high sticker price with relatively few Pell recipients.** "
                    f"This raises equity access questions: is the institution primarily serving students with financial means? "
                    f"Albion's average Pell grant of **{z_fmt}** shows generosity for enrolled Pell students, "
                    f"but the enrollment share suggests room to expand economic access through targeted recruitment.")
        elif not above_x and above_y:
            quad = ("**upper-left — a relatively affordable COA paired with strong Pell enrollment.** "
                    f"This is a strong access profile. Albion's average Pell grant of **{z_fmt}** complements "
                    f"the lower sticker price to create genuine affordability for low-income students.")
        else:
            quad = ("**lower-left — more affordable institution with below-median Pell enrollment.** "
                    f"Lower cost without commensurate Pell enrollment may reflect a recruitment or outreach gap "
                    f"rather than a mission misalignment. Albion's average Pell grant of **{z_fmt}** "
                    f"indicates support capacity for those who do enroll.")
        st.markdown(
            f"#### 📍 Albion's Position — {x_var} × {y_var}\n\n"
            f"Albion's **in-state COA of ${alb_x:,.0f}** is higher than **{cost_pct}%** of the "
            f"{n:,} institutions in this view ({cost_pct}% are more expensive). "
            f"Its **Pell recipient share of {alb_y:.1f}%** ranks at the **{pell_pct}th percentile** "
            f"for economic access, outperforming {pell_pct}% of institutions.\n\n"
            f"Albion sits in the {quad}"
        )

    elif chart_idx == 2:  # Faculty → Retention
        sfr_pct = 100 - x_pct   # % of schools with LARGER ratio (worse); Albion outperforms
        ret_pct = y_pct
        z_fmt = f"${alb_z:,.0f}" if alb_z else "N/A"
        if not above_x and above_y:
            quad = ("**upper-left — the ideal position: smaller-than-average classes paired with above-average retention.** "
                    f"Albion's average faculty salary of **{z_fmt}** indicates whether this teaching model is "
                    f"sustainably resourced. Small classes backed by competitive faculty pay produce compounding "
                    f"returns on student experience and should be actively protected in budget decisions.")
        elif above_x and above_y:
            quad = ("**upper-right — larger-than-average student:faculty ratio, yet above-average retention.** "
                    f"Albion is retaining students through mechanisms beyond class size — likely strong community "
                    f"culture, advising infrastructure, or financial aid design. This is encouraging, and "
                    f"reducing the ratio further could lift retention even higher.")
        elif not above_x and not above_y:
            quad = ("**lower-left — small classes but below-average retention.** "
                    f"This is a critical diagnostic: if class size is not the constraint, the retention gap "
                    f"likely stems from student services, financial stress, social belonging, or academic preparation. "
                    f"Small classes create conditions for success but do not guarantee them without "
                    f"complementary student support infrastructure.")
        else:
            quad = ("**lower-right — larger-than-average classes combined with below-average retention.** "
                    f"A {alb_x:.0f}:1 ratio may be limiting individualized faculty-student connection. "
                    f"Reducing this ratio is capital-intensive but one of the highest-leverage structural "
                    f"investments for improving first-year persistence.")
        st.markdown(
            f"#### 📍 Albion's Position — {x_var} × {y_var}\n\n"
            f"Albion's **student:faculty ratio of {alb_x:.0f}:1** is lower (smaller classes) than "
            f"**{sfr_pct}%** of the {n:,} institutions plotted. "
            f"Its **first-year retention rate of {alb_y:.1f}%** ranks at the **{ret_pct}th percentile**, "
            f"outperforming {ret_pct}% of institutions.\n\n"
            f"Albion sits in the {quad}"
        )

    elif chart_idx == 3:  # Equity Lens
        pell_enr_pct = x_pct   # % with fewer Pell students
        pell_gr_pct  = y_pct
        if above_x and above_y:
            quad = ("**upper-right — the equity-champion position: enrolling more Pell students than average AND "
                    "graduating them at above-average rates.** Very few institutions achieve both simultaneously. "
                    "This is a genuine institutional differentiator that defines mission, attracts aligned "
                    "faculty and donors, and should be featured prominently in institutional branding.")
        elif above_x and not above_y:
            quad = ("**lower-right — significant Pell enrollment but below-average Pell graduation rates.** "
                    "This is the most actionable finding in this chart: Albion is opening the door to low-income "
                    "students but not yet fully closing the success gap. Evidence-based interventions — "
                    "emergency aid funds, first-generation mentoring, intrusive advising, and summer bridge "
                    "programs — have produced measurable results at comparable institutions.")
        elif not above_x and above_y:
            quad = ("**upper-left — graduating Pell students at high rates, but with fewer Pell recipients than "
                    "the peer median.** The support infrastructure for Pell students is clearly working. "
                    "The strategic opportunity is to expand Pell enrollment — through targeted recruitment, "
                    "increased aid, and First-Gen programming — while maintaining these strong completion rates.")
        else:
            quad = ("**lower-left — below-median Pell enrollment and below-median Pell graduation rates.** "
                    "This represents both access and equity outcome gaps that warrant strategic attention "
                    "across admissions, financial aid design, and student success infrastructure.")
        st.markdown(
            f"#### 📍 Albion's Position — {x_var} × {y_var}\n\n"
            f"Albion's **Pell recipient share of {alb_x:.1f}%** is higher than **{pell_enr_pct}%** of "
            f"the {n:,} institutions in this view (Albion enrolls more Pell students than {pell_enr_pct}% of peers). "
            f"Its **Pell graduation rate of {alb_y:.1f}%** ranks at the **{pell_gr_pct}th percentile**, "
            f"outperforming {pell_gr_pct}% of institutions.\n\n"
            f"Albion sits in the {quad}"
        )


def _scatter_explorer(df: pd.DataFrame):
    """Four curated scatter views displayed as tabs, each with an analyst rationale."""
    tabs = st.tabs([name for name, *_ in SCATTER_SUGGESTIONS])
    for chart_idx, (tab, (name, x_var, y_var, z_var), rationale) in enumerate(
            zip(tabs, SCATTER_SUGGESTIONS, SCATTER_RATIONALE)):
        with tab:
            st.info(rationale)
            x_col = SCATTER_VARS[x_var]
            y_col = SCATTER_VARS[y_var]
            z_col = SCATTER_VARS[z_var]

            sc_df = df.dropna(subset=[x_col, y_col, z_col]).copy()
            sc_df["_zplot"] = sc_df[z_col].clip(lower=0.01)

            if sc_df.empty:
                st.warning("No institutions have data for all three variables in this view.")
                continue

            scatter_kwargs = dict(
                x=x_col, y=y_col,
                size="_zplot", size_max=45,
                color="CONTROL_LBL", color_discrete_map=CONTROL_COLORS,
                hover_name="INSTNM",
                hover_data={"STABBR": True, "SECTOR_LBL": True, "_zplot": False},
                labels={
                    x_col: x_var, y_col: y_var,
                    "CONTROL_LBL": "Control", "SECTOR_LBL": "Sector", "STABBR": "State",
                },
                opacity=0.72,
                title=f"{x_var}  ×  {y_var}  (bubble = {z_var})",
                **_trend_kw(),
            )
            fig = px.scatter(sc_df, **scatter_kwargs)

            _fname = _focus_name()
            albion = sc_df[sc_df["UNITID"] == _focus_uid()]
            if not albion.empty:
                ar = albion.iloc[0]
                fig.add_trace(go.Scatter(
                    x=[ar[x_col]], y=[ar[y_col]],
                    mode="markers+text",
                    marker=dict(symbol="star", size=26, color="#F59E0B",
                                line=dict(color="#1E3A5F", width=2.5)),
                    text=[_fname],
                    textposition="top right",
                    textfont=dict(size=12, color="#1E3A5F", family="Arial Black"),
                    name=_fname,
                    hovertemplate=(
                        f"<b>{_fname}</b><br>{x_var}: %{{x:.1f}}<br>{y_var}: %{{y:.1f}}<extra></extra>"
                    ),
                    showlegend=True,
                ))

            fig.update_layout(height=560, legend=dict(title="Control"))
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"n = **{len(sc_df):,}** institutions with data for all three variables.")
            if _focus_uid() == ALBION_UNITID:
                _scatter_albion_insight(sc_df, albion, x_col, y_col, z_col,
                                        x_var, y_var, z_var, chart_idx)


# ── Page 2: Institution Profile ──────────────────────────────────────────────
def page_profile(df: pd.DataFrame, year: str = "2024-25"):
    h_col, y_col = st.columns([7, 3])
    with h_col:
        st.title("Institution Profile")
    with y_col:
        st.markdown("<div style='padding-top:1.1rem'></div>", unsafe_allow_html=True)
        st.radio("Data Year", ["2024-25", "2023-24", "2022-23"], horizontal=True,
                 key="year_Institution Profile", label_visibility="collapsed")

    names = sorted(df["DISPLAY_NAME"].dropna().unique().tolist())
    # Open on the sidebar Institution of Interest. Resolve it by UNITID against the
    # current year's data so it works even when display names differ across years;
    # a local pick (sel_inst) still overrides. Falls back to the placeholder when
    # no Institution of Interest has been chosen.
    pre = st.session_state.get("sel_inst")
    if not pre or pre not in names:
        _fuid = _focus_uid()
        if _fuid is not None:
            _m = df[df["UNITID"] == _fuid]
            pre = _m.iloc[0]["DISPLAY_NAME"] if not _m.empty else None
    default_idx = (names.index(pre) + 1) if pre and pre in names else 0
    sel = st.selectbox("Search for an institution", ["— select an institution —"] + names,
                       index=default_idx)
    if sel != "— select an institution —":
        st.session_state["sel_inst"] = sel
    if sel == "— select an institution —":
        st.info("Use the search box to find any institution.")
        return

    row = df[df["DISPLAY_NAME"] == sel].iloc[0]
    uid = int(row["UNITID"])
    con = duckdb.connect(DB_PATH, read_only=True)
    yr    = year[:4]   # "2024", "2023", or "2022"
    yr2   = year[2:4]  # "24", "23", or "22"
    sfayr    = f"{int(yr)-1}{yr2}"   # e.g. "2324" for 2024-25, "2223" for 2023-24, "2122" for 2022-23
    yr_prev  = str(int(yr) - 1)      # "2023", "2022", or "2021"
    # Academic year labels for multi-year trend data (AY3=current, AY2=1yr ago, AY1=2yr, AY0=3yr)
    _ay3 = year
    _ay2 = f"{int(yr)-1}-{yr2}"
    _ay1 = f"{int(yr)-2}-{int(yr2)-1:02d}"
    _ay0 = f"{int(yr)-3}-{int(yr2)-2:02d}"

    try:
        # Header
        badges = []
        if row.get("HBCU")    == 1: badges.append("HBCU")
        if row.get("TRIBAL")  == 1: badges.append("Tribal")
        if row.get("MEDICAL") == 1: badges.append("Medical")
        if row.get("LANDGRNT")== 1: badges.append("Land-grant")
        badge_str = "  ".join([f"`{b}`" for b in badges])

        _wv = row.get("WEBADDR", None)
        web = "" if _wv is None or pd.isna(_wv) else str(_wv)
        web_md = f"[{web}](https://{web})" if web and not web.startswith("http") else (f"[{web}]({web})" if web else "")

        st.markdown(f"## {row['INSTNM']}")
        st.markdown(f"{row['CITY']}, {row['STABBR']}  &nbsp;|&nbsp;  {row['SECTOR_LBL']}  &nbsp;|&nbsp;  {web_md}")
        if badge_str:
            st.markdown(badge_str)
        st.caption(f"UNITID: {uid}")

        # Key metrics
        m = st.columns(7)
        m[0].metric("Total Enrollment",    fmt(row.get("ENRTOT"),        "int"))
        m[1].metric("Acceptance Rate",     fmt(row.get("DVADM01"),       "pct"))
        m[2].metric("Grad Rate 150%",      fmt(row.get("GRRTTOT"),       "pct"))
        m[3].metric("8-yr Award Rate",     fmt(row.get("OM1TOTLAWDP8"),  "pct"))
        m[4].metric("In-State COA",        fmt(row.get("CINSON"),        "dollar"))
        m[5].metric(f"Tuition {year}",      fmt(row.get("TUFEYR3"),       "dollar"))
        m[6].metric("Avg Faculty Salary",  fmt(row.get("SALTOTL"),       "dollar"))

        st.divider()

        tabs = st.tabs([
            "Overview", "Enrollment", "Admissions", "Completions",
            "Student Outcomes", "Costs", "Financial Aid", "Finance",
            "Faculty & Staff", "Library"
        ])

        # ── Tab 0: Overview ───────────────────────────────────────────────────
        with tabs[0]:
            # Mission statement
            try:
                msn = con.execute(f"SELECT MISSION, MISSIONURL FROM IC{yr}MISSION WHERE UNITID={uid}").fetchone()
                if msn and msn[0]:
                    with st.expander("Mission Statement"):
                        st.write(msn[0])
                        if msn[1]:
                            st.caption(f"Source: {msn[1]}")
            except Exception:
                pass

            # Contact & identifiers from HD2024
            with st.expander("Contact Information & URLs"):
                cc1, cc2, cc3 = st.columns(3)
                with cc1:
                    st.markdown("**Institutional Contact**")
                    for lbl, key in [("Chief Officer", "CHFNM"), ("Title", "CHFTITLE"),
                                     ("Phone", "GENTELE"), ("ZIP", "ZIP"), ("OPEID", "OPEID")]:
                        val = row.get(key, "")
                        if pd.notna(val) and val:
                            st.write(f"**{lbl}:** {val}")
                with cc2:
                    st.markdown("**URLs**")
                    url_map = [("Website", "WEBADDR"), ("Admissions", "ADMINURL"),
                               ("Financial Aid", "FAIDURL"), ("Application", "APPLURL"),
                               ("Net Price Calc", "NPRICURL")]
                    for lbl, key in url_map:
                        _v = row.get(key, None)
                        u = "" if _v is None or pd.isna(_v) else str(_v)
                        if u and u != "nan":
                            href = u if u.startswith("http") else f"https://{u}"
                            st.markdown(f"**{lbl}:** [{u}]({href})")
                with cc3:
                    st.markdown("**More URLs**")
                    url_map2 = [("Veterans", "VETURL"), ("Disability Services", "DISAURL")]
                    for lbl, key in url_map2:
                        _v = row.get(key, None)
                        u = "" if _v is None or pd.isna(_v) else str(_v)
                        if u and u != "nan":
                            href = u if u.startswith("http") else f"https://{u}"
                            st.markdown(f"**{lbl}:** [{u}]({href})")

            c1, c2 = st.columns(2)
            with c1:
                st.subheader("Classification")
                info = {
                    "Level":              row.get("LEVEL_LBL", ""),
                    "Control":            row.get("CONTROL_LBL", ""),
                    "Sector":             row.get("SECTOR_LBL", ""),
                    "Locale":             row.get("LOCALE_LBL", ""),
                    "Region":             row.get("OBEREG_LBL", ""),
                    "Institution Size":   row.get("INSTSIZE_LBL", ""),
                    "Highest Level":      row.get("HLOFFER_LBL", ""),
                    "Degree-granting":    "Yes" if row.get("DEGGRANT") == 1 else "No",
                    "Carnegie 2025 IC":   row.get("CARNEGIE_LBL", ""),
                    "Carnegie Research":  row.get("CARNEGIERSCH_LBL", ""),
                    "Carnegie Size":      row.get("CARNEGIESIZE_LBL", ""),
                    "Carnegie ALF":       row.get("CARNEGIEALF_LBL", ""),
                    "Land-grant":         yesno(row.get("LANDGRNT")),
                    "UNITID":             uid,
                }
                st.table(pd.DataFrame.from_dict(info, orient="index", columns=["Value"]).astype(str))

            with c2:
                st.subheader("Key Indicators")
                ind = {
                    "Full-time Retention Rate":  fmt(row.get("RET_PCF"),         "pct"),
                    "Part-time Retention Rate":  fmt(row.get("RET_PCP"),         "pct"),
                    "Student-to-Faculty Ratio":  f"{row.get('STUFACR'):.0f}:1" if pd.notna(row.get("STUFACR")) else "N/A",
                    "Pell Grad Rate (150%)":     fmt(row.get("PGGRRTT"),         "pct"),
                    "Transfer-out Rate":         fmt(row.get("TRRTTOT"),         "pct"),
                    "8-yr Award Rate (FT)":      fmt(row.get("OM1TOTLAWDP8"),    "pct"),
                    "8-yr Award Rate (PT)":      fmt(row.get("OM2TOTLAWDP8"),    "pct"),
                    "% Women":                   fmt(row.get("PCTENRW"),         "pct"),
                    "% Exclusively Distance Ed": fmt(row.get("PCTDEEXC"),        "pct"),
                    "% Receiving Any Aid":       fmt(row.get("ANYAIDP"),         "pct"),
                }
                st.table(pd.DataFrame.from_dict(ind, orient="index", columns=["Value"]).astype(str))

            # IC{yr} educational offerings
            try:
                ic = con.execute(f"SELECT * FROM IC{yr} WHERE UNITID={uid}").df()
                if not ic.empty:
                    ic_row = ic.iloc[0]
                    st.subheader(f"Educational Offerings & Services (IC{yr})")
                    c1, c2, c3 = st.columns(3)

                    with c1:
                        st.markdown("**Admission & Calendar**")
                        open_adm = ic_row.get("OPENADMP")
                        open_lbl = "Open admission" if open_adm == 1 else ("Selective admission" if open_adm == 2 else "Not reported")
                        st.write(f"Admission policy: **{open_lbl}**")
                        cal = ic_row.get("CALSYS")
                        st.write(f"Calendar system: **{CALSYS_MAP.get(int(cal), str(cal)) if pd.notna(cal) else 'N/A'}**")
                        disab = ic_row.get("DISABPCT")
                        st.write(f"% Students with disabilities: **{fmt(disab, 'pct')}**")

                        st.markdown("**Programs Offered**")
                        peo = {
                            "Occupational":       ic_row.get("PEO1ISTR"),
                            "Academic":           ic_row.get("PEO2ISTR"),
                            "Continuing/Prof":    ic_row.get("PEO3ISTR"),
                            "Recreational":       ic_row.get("PEO4ISTR"),
                            "Adult basic/remedial":ic_row.get("PEO5ISTR"),
                            "Secondary (HS)":     ic_row.get("PEO6ISTR"),
                            "Prep/preregistered": ic_row.get("PEO7ISTR"),
                        }
                        for lbl, val in peo.items():
                            if pd.notna(val) and int(val) == 1:
                                st.write(f"✓ {lbl}")

                    with c2:
                        st.markdown("**Degrees/Certificates Offered**")
                        levels_offered = {
                            "Certificate < 1 year":     ic_row.get("LEVEL1"),
                            "Certificate 1-2 years":    ic_row.get("LEVEL2"),
                            "Associate's":              ic_row.get("LEVEL3"),
                            "Bachelor's":               ic_row.get("LEVEL4"),
                            "Post-bacc Certificate":    ic_row.get("LEVEL5"),
                            "Master's":                 ic_row.get("LEVEL6"),
                            "Post-master's Certificate":ic_row.get("LEVEL7"),
                            "Doctor's":                 ic_row.get("LEVEL8"),
                            "Post-doctoral":            ic_row.get("LEVEL17"),
                        }
                        for lbl, val in levels_offered.items():
                            if pd.notna(val) and int(val) == 1:
                                st.write(f"✓ {lbl}")

                        st.markdown("**Distance Education**")
                        de_offered = {
                            "Programs fully online":     ic_row.get("DSTNCED1"),
                            "Some online programs":      ic_row.get("DSTNCED2"),
                            "No distance ed programs":   ic_row.get("DSTNCED3"),
                        }
                        for lbl, val in de_offered.items():
                            if pd.notna(val) and int(val) == 1:
                                st.write(f"→ {lbl}")

                    with c3:
                        st.markdown("**Student Services**")
                        svc = {
                            "Remedial services":            ic_row.get("STUSRV2"),
                            "Academic/career counseling":   ic_row.get("STUSRV3"),
                            "Employment services":          ic_row.get("STUSRV4"),
                            "Placement services":           ic_row.get("STUSRV8"),
                            "On-campus daycare":            ic_row.get("STUSRV9"),
                        }
                        for lbl, val in svc.items():
                            if pd.notna(val) and int(val) == 1:
                                st.write(f"✓ {lbl}")

                        st.markdown("**Veterans Services**")
                        vet = {
                            "Yellow Ribbon Program":         ic_row.get("VET1"),
                            "Direct VA billing":             ic_row.get("VET2"),
                            "Student veteran organization":  ic_row.get("VET3"),
                            "Dedicated VA point of contact": ic_row.get("VET4"),
                            "VA academic counseling":        ic_row.get("VET5"),
                        }
                        for lbl, val in vet.items():
                            if pd.notna(val) and int(val) == 1:
                                st.write(f"✓ {lbl}")

                        st.markdown("**Credits Accepted**")
                        cred = {
                            "Dual credit":      ic_row.get("CREDITS2"),
                            "Credit for life":  ic_row.get("CREDITS3"),
                            "AP credit":        ic_row.get("CREDITS4"),
                        }
                        for lbl, val in cred.items():
                            if pd.notna(val) and int(val) == 1:
                                st.write(f"✓ {lbl}")
            except Exception as ex:
                st.info(f"Educational offerings data not available. ({ex})")

            # FLAGS{yr} — response status, fiscal year, tenure system
            st.subheader(f"Survey Response Status & Institutional Flags (FLAGS{yr})")
            try:
                flags = con.execute(f"""
                    SELECT STAT_IC, STAT_EF, STAT_C, STAT_E12, STAT_ADM, STAT_SFA,
                           STAT_GR, STAT_GR2, STAT_OM, STAT_HR, STAT_F, STAT_AL,
                           TENURSYS, FYBEG, FYEND, COHRTSTU
                    FROM FLAGS{yr} WHERE UNITID={uid}
                """).df()
                if not flags.empty:
                    fg = flags.iloc[0]
                    STATUS_LBL = {1:"Submitted",2:"Not submitted",3:"Not applicable",-2:"N/A"}
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        st.markdown("**Survey Response Status**")
                        status_items = [
                            ("Inst. Characteristics", fg.get("STAT_IC")),
                            ("Fall Enrollment",        fg.get("STAT_EF")),
                            ("Completions",            fg.get("STAT_C")),
                            ("12-Month Enrollment",    fg.get("STAT_E12")),
                            ("Admissions",             fg.get("STAT_ADM")),
                            ("Student Fin. Aid",       fg.get("STAT_SFA")),
                            ("Grad Rates",             fg.get("STAT_GR")),
                            ("200% Grad Rates",        fg.get("STAT_GR2")),
                            ("Outcome Measures",       fg.get("STAT_OM")),
                            ("Human Resources",        fg.get("STAT_HR")),
                            ("Finance",                fg.get("STAT_F")),
                            ("Academic Libraries",     fg.get("STAT_AL")),
                        ]
                        for lbl, val in status_items:
                            if pd.notna(val):
                                icon = "✓" if int(val) == 1 else ("–" if int(val) == 3 else "✗")
                                st.write(f"{icon} {lbl}: **{STATUS_LBL.get(int(val), str(val))}**")
                    with c2:
                        st.markdown("**Finance & HR Flags**")
                        ts = fg.get("TENURSYS")
                        st.write(f"Tenure system: **{'Yes' if ts==1 else ('No' if ts==2 else 'N/A')}**")
                        fybeg = fg.get("FYBEG")
                        fyend = fg.get("FYEND")
                        if pd.notna(fybeg):
                            st.write(f"Fiscal year: **{fybeg} – {fyend}**")
                    with c3:
                        cohort = fg.get("COHRTSTU")
                        if pd.notna(cohort):
                            st.metric("Graduation Rate Cohort Size", fmt(cohort, "int"))
                else:
                    st.info(f"FLAGS{yr} data not available.")
            except Exception as ex:
                st.info(f"FLAGS{yr} not available. ({ex})")

        # ── Tab 1: Enrollment ─────────────────────────────────────────────────
        with tabs[1]:
            c1, c2 = st.columns(2)
            with c1:
                st.subheader(f"Race / Ethnicity Composition (Fall {yr})")
                race = {
                    "White":       row.get("PCTENRWH"),
                    "Black / AA":  row.get("PCTENRBK"),
                    "Hispanic":    row.get("PCTENRHS"),
                    "Asian / PI":  row.get("PCTENRAP"),
                    "AI / AN":     row.get("PCTENRAN"),
                    "Two+ Races":  row.get("PCTENR2M"),
                    "Unknown":     row.get("PCTENRUN"),
                    "Nonresident": row.get("PCTENRNR"),
                }
                race_df = pd.DataFrame(
                    [(k, v) for k, v in race.items() if pd.notna(v) and v > 0],
                    columns=["Group", "%"]
                )
                if not race_df.empty:
                    fig = px.pie(race_df, values="%", names="Group", hole=0.4,
                                 title="Race/Ethnicity (% of total enrollment)")
                    fig.update_layout(height=360)
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Race/ethnicity data not available.")

            with c2:
                st.subheader("Enrollment Breakdown")
                bars = [
                    ("Full-time",     row.get("ENRFT")),
                    ("Part-time",     row.get("ENRPT")),
                    ("Undergraduate", row.get("EFUG")),
                    ("Graduate",      row.get("EFGRAD")),
                ]
                bar_df = pd.DataFrame([(k, v) for k, v in bars if pd.notna(v) and v > 0],
                                       columns=["Category", "Count"])
                if not bar_df.empty:
                    fig = px.bar(bar_df, x="Category", y="Count",
                                 color="Category",
                                 color_discrete_sequence=px.colors.qualitative.Set2,
                                 title=f"Fall {yr} Enrollment")
                    fig.update_layout(showlegend=False, height=360)
                    st.plotly_chart(fig, use_container_width=True)

            # 12-month enrollment from EFFY{yr}
            st.subheader(f"12-Month Unduplicated Headcount ({year})")
            try:
                effy = con.execute(f"""
                    SELECT EFFYALEV, EFYTOTLT, EFYTOTLM, EFYTOTLW,
                           EFYAIANT, EFYASIAT, EFYBKAAT, EFYHISPT,
                           EFYNHPIT, EFYWHITT, EFY2MORT, EFYUNKNT, EFYNRALT
                    FROM EFFY{yr}
                    WHERE UNITID={uid} AND EFFYALEV IN (1, 2, 12)
                """).df()
                if not effy.empty:
                    EFFY_LBL = {1: "All students", 2: "Undergraduate", 12: "Graduate"}
                    effy["Level"] = effy["EFFYALEV"].map(EFFY_LBL)
                    tot_row = effy[effy["EFFYALEV"] == 1]
                    if not tot_row.empty:
                        tr = tot_row.iloc[0]
                        ec1, ec2, ec3 = st.columns(3)
                        ec1.metric("12-month Total",        fmt(tr["EFYTOTLT"], "int"))
                        ec2.metric("12-month Male",         fmt(tr["EFYTOTLM"], "int"))
                        ec3.metric("12-month Female",       fmt(tr["EFYTOTLW"], "int"))

                        # Race breakdown for all students
                        effy_race = {
                            "AI/AN":       tr["EFYAIANT"], "Asian":     tr["EFYASIAT"],
                            "Black/AA":    tr["EFYBKAAT"], "Hispanic":  tr["EFYHISPT"],
                            "NHPI":        tr["EFYNHPIT"], "White":     tr["EFYWHITT"],
                            "Two+ races":  tr["EFY2MORT"], "Unknown":   tr["EFYUNKNT"],
                            "Nonresident": tr["EFYNRALT"],
                        }
                        er_df = pd.DataFrame(
                            [(k, float(v)) for k, v in effy_race.items() if pd.notna(v) and float(v) > 0],
                            columns=["Group", "Count"]
                        )
                        if not er_df.empty:
                            fig = px.bar(er_df.sort_values("Count", ascending=False),
                                         x="Group", y="Count",
                                         color_discrete_sequence=["#17becf"],
                                         title="12-Month Headcount by Race/Ethnicity")
                            fig.update_layout(height=300, showlegend=False)
                            st.plotly_chart(fig, use_container_width=True)

                    # Level breakdown
                    lvl_df = effy[effy["EFFYALEV"].isin([1, 2, 12])][["Level", "EFYTOTLT"]].dropna()
                    if not lvl_df.empty:
                        fig = px.bar(lvl_df, x="Level", y="EFYTOTLT",
                                     color="Level",
                                     color_discrete_sequence=px.colors.qualitative.Pastel,
                                     title="12-Month Headcount by Level")
                        fig.update_layout(height=280, showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("12-month headcount data not available.")
            except Exception as ex:
                st.info(f"12-month data not available. ({ex})")

            # Distance education breakdown
            st.subheader(f"Distance Education Mode (Fall {yr})")
            de = {
                "Exclusively distance": row.get("PCTDEEXC"),
                "Some distance":        row.get("PCTDESOM"),
                "No distance":          row.get("PCTDENON"),
            }
            de_df = pd.DataFrame([(k, v) for k, v in de.items() if pd.notna(v)], columns=["Mode", "%"])
            if not de_df.empty:
                fig = px.bar(de_df, x="Mode", y="%", color="Mode",
                             color_discrete_sequence=["#1f77b4", "#ff7f0e", "#2ca02c"])
                fig.update_layout(showlegend=False, height=280, yaxis_range=[0, 100])
                st.plotly_chart(fig, use_container_width=True)

            # 12-month distance ed
            try:
                effy_dist = con.execute(f"""
                    SELECT EFFYDLEV, EFYDETOT, EFYDEEXC, EFYDESOM, EFYDENON
                    FROM EFFY{yr}_DIST WHERE UNITID={uid}
                """).df()
                if not effy_dist.empty:
                    st.subheader("12-Month Headcount by Distance Education Status")
                    st.dataframe(effy_dist.rename(columns={
                        "EFFYDLEV":"Level Code","EFYDETOT":"Total",
                        "EFYDEEXC":"Exclusively DE","EFYDESOM":"Some DE","EFYDENON":"No DE"
                    }), use_container_width=True)
            except Exception:
                pass

            # EF{yr}A_DIST — fall enrollment by distance education status
            st.subheader(f"Fall Enrollment by Distance Education Status (EF{yr}A_DIST)")
            try:
                efa_dist = con.execute(f"""
                    SELECT EFDELEV, EFDETOT, EFDEEXC, EFDESOM, EFDENON,
                           EFDEEX1, EFDEEX2, EFDEEX3, EFDEEX4, EFDEEX5
                    FROM EF{yr}A_DIST WHERE UNITID={uid} AND EFDELEV IN (1,2,3)
                    ORDER BY EFDELEV
                """).df()
                if not efa_dist.empty:
                    EFDELEV_LBL = {
                        1: "All students", 2: "Undergraduate", 3: "Graduate/1st-professional"
                    }
                    efa_dist["Level"] = efa_dist["EFDELEV"].map(EFDELEV_LBL).fillna(efa_dist["EFDELEV"].astype(str))
                    afa_show = efa_dist[["Level", "EFDETOT", "EFDEEXC", "EFDESOM", "EFDENON"]].rename(columns={
                        "EFDETOT": "Total", "EFDEEXC": "Exclusively DE",
                        "EFDESOM": "Some DE", "EFDENON": "No DE"
                    })
                    st.dataframe(afa_show.reset_index(drop=True), use_container_width=True)
                    # Chart: exclusively vs some DE
                    chart_row = efa_dist[efa_dist["EFDELEV"] == 1]
                    if not chart_row.empty:
                        cr = chart_row.iloc[0]
                        de_vals = [
                            ("Exclusively DE", cr.get("EFDEEXC")),
                            ("Some DE",        cr.get("EFDESOM")),
                            ("No DE",          cr.get("EFDENON")),
                        ]
                        de_df = pd.DataFrame([(k, float(v)) for k, v in de_vals if pd.notna(v)],
                                              columns=["Mode", "Count"])
                        if not de_df.empty:
                            fig = px.pie(de_df, values="Count", names="Mode", hole=0.35,
                                         title=f"Fall {yr}: DE Mode (All Students)",
                                         color_discrete_sequence=["#1f77b4","#ff7f0e","#2ca02c"])
                            fig.update_layout(height=300)
                            st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Distance education enrollment data not available.")
            except Exception as ex:
                st.info(f"EF{yr}A_DIST not available. ({ex})")

            # EFFY{yr}_HS — dual enrolled high school students
            st.subheader(f"Dual-enrolled High School Students (EFFY{yr}_HS)")
            try:
                hs = con.execute(f"SELECT * FROM EFFY{yr}_HS WHERE UNITID={uid}").df()
                if not hs.empty:
                    hsr = hs.iloc[0]
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Total HS Students", fmt(hsr.get("EFYTOTLT"), "int"))
                    c2.metric("Male",              fmt(hsr.get("EFYTOTLM"), "int"))
                    c3.metric("Female",            fmt(hsr.get("EFYTOTLW"), "int"))
                    hs_race = {
                        "AI/AN":       hsr.get("EFYAIANT"),
                        "Asian":       hsr.get("EFYASIAT"),
                        "Black/AA":    hsr.get("EFYBKAAT"),
                        "Hispanic":    hsr.get("EFYHISPT"),
                        "NHPI":        hsr.get("EFYNHPIT"),
                        "White":       hsr.get("EFYWHITT"),
                        "Two+ races":  hsr.get("EFY2MORT"),
                        "Unknown":     hsr.get("EFYUNKNT"),
                        "Nonresident": hsr.get("EFYNRALT"),
                    }
                    hs_df = pd.DataFrame(
                        [(k, float(v)) for k, v in hs_race.items() if pd.notna(v) and float(v) > 0],
                        columns=["Race/Ethnicity", "Count"]
                    )
                    if not hs_df.empty:
                        fig = px.bar(hs_df.sort_values("Count", ascending=False),
                                     x="Race/Ethnicity", y="Count",
                                     color_discrete_sequence=["#e377c2"],
                                     title="Dual-enrolled HS Students by Race/Ethnicity")
                        fig.update_layout(height=300, showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("No dual-enrolled HS student data for this institution.")
            except Exception as ex:
                st.info(f"EFFY{yr}_HS not available. ({ex})")

            # EF{yr}A detailed enrollment by level/gender/race
            st.subheader(f"Fall Enrollment by Level & Race/Ethnicity (EF{yr}A)")
            try:
                efa = con.execute(f"""
                    SELECT EFALEVEL, EFTOTLT, EFTOTLM, EFTOTLW,
                           EFAIANT, EFASIAT, EFBKAAT, EFHISPT,
                           EFNHPIT, EFWHITT, EF2MORT, EFUNKNT, EFNRALT
                    FROM EF{yr}A
                    WHERE UNITID={uid} AND EFALEVEL IN (1,2,11,12,21,22)
                    ORDER BY EFALEVEL
                """).df()
                if not efa.empty:
                    EFALEVEL_LBL = {
                        1:"All students (total)",2:"All students (full-time)",
                        11:"Undergraduate (total)",12:"Undergraduate (full-time)",
                        21:"Graduate (total)",22:"Graduate (full-time)",
                    }
                    efa["Level"] = efa["EFALEVEL"].map(EFALEVEL_LBL)
                    show_cols = ["Level","EFTOTLT","EFTOTLM","EFTOTLW",
                                 "EFWHITT","EFBKAAT","EFHISPT","EFASIAT",
                                 "EFAIANT","EF2MORT","EFNRALT"]
                    efa_show = efa[show_cols].rename(columns={
                        "EFTOTLT":"Total","EFTOTLM":"Male","EFTOTLW":"Female",
                        "EFWHITT":"White","EFBKAAT":"Black/AA","EFHISPT":"Hispanic",
                        "EFASIAT":"Asian/PI","EFAIANT":"AI/AN","EF2MORT":"Two+","EFNRALT":"Nonresident"
                    })
                    st.dataframe(efa_show.reset_index(drop=True), use_container_width=True)
                else:
                    st.info("Detailed enrollment data not available.")
            except Exception as ex:
                st.info(f"Detailed enrollment not available. ({ex})")

            # EF{yr}B — enrollment by age category
            st.subheader(f"Fall Enrollment by Age Category (EF{yr}B)")
            EFBAGE_LBL = {
                1:"All ages (total)", 2:"Under 25 (total)",
                3:"Under 18", 4:"18-19", 5:"20-21", 6:"22-24",
                7:"25 and over (total)", 8:"25-29", 9:"30-34",
                10:"35-39", 11:"40-49", 12:"50-64",
                13:"65 and over", 14:"Age unknown",
            }
            try:
                efb = con.execute(f"""
                    SELECT EFBAGE, EFAGE09 AS Total, EFAGE01 AS FTMen, EFAGE02 AS FTWomen,
                           EFAGE03 AS PTMen, EFAGE04 AS PTWomen,
                           EFAGE05 AS FullTime, EFAGE06 AS PartTime,
                           EFAGE07 AS TotalMen, EFAGE08 AS TotalWomen
                    FROM EF{yr}B WHERE UNITID={uid}
                    ORDER BY EFBAGE
                """).df()
                if not efb.empty:
                    efb["Age Group"] = efb["EFBAGE"].map(EFBAGE_LBL).fillna(efb["EFBAGE"].astype(str))
                    # Show detail rows (exclude totals EFBAGE 1,2,7)
                    detail = efb[efb["EFBAGE"].isin([3,4,5,6,8,9,10,11,12,13,14])].copy()
                    if not detail.empty:
                        fig = px.bar(detail, x="Age Group", y="Total",
                                     color_discrete_sequence=["#8c564b"],
                                     title=f"Total Enrollment by Age Group (Fall {yr})")
                        fig.update_layout(height=320, showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)
                    show_cols = ["Age Group","Total","FullTime","PartTime","TotalMen","TotalWomen"]
                    st.dataframe(efb[show_cols].reset_index(drop=True), use_container_width=True)
                else:
                    st.info("Age enrollment data not reported (optional survey, even years only).")
            except Exception as ex:
                st.info(f"Age enrollment not available. ({ex})")

            # EF{yr}C — state of residence of first-time freshmen
            st.subheader(f"State of Origin — First-time Undergraduates (EF{yr}C, Top 15 States)")
            try:
                efc = con.execute(f"""
                    SELECT EFCSTATE, EFRES01, EFRES02
                    FROM EF{yr}C WHERE UNITID={uid} AND EFCSTATE != 99
                    ORDER BY EFRES01 DESC NULLS LAST
                    LIMIT 15
                """).df()
                if not efc.empty:
                    efc = efc.rename(columns={
                        "EFCSTATE":"State FIPS","EFRES01":"1st-time UG Students",
                        "EFRES02":"Recent HS Grads"
                    })
                    c1, c2 = st.columns(2)
                    with c1:
                        st.dataframe(efc.reset_index(drop=True), use_container_width=True)
                    with c2:
                        fig = px.bar(efc.head(10), x="State FIPS", y="1st-time UG Students",
                                     color_discrete_sequence=["#e377c2"],
                                     title="Top 10 States of Origin (1st-time UG)")
                        fig.update_layout(height=320, showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("State of origin data not reported (optional survey, odd years only).")
            except Exception as ex:
                st.info(f"State of origin not available. ({ex})")

            # EF{yr}CP — enrollment by major field and race/ethnicity (4-year only)
            st.subheader(f"Enrollment by Major Field & Race/Ethnicity (EF{yr}CP, Top 15 CIPs)")
            try:
                efcp = con.execute(f"""
                    SELECT CIPCODE, SUM(EFTOTLT) AS Total,
                           SUM(EFWHITT) AS White, SUM(EFBKAAT) AS BlackAA,
                           SUM(EFHISPT) AS Hispanic, SUM(EFASIAT) AS AsianPI,
                           SUM(EFNRALT) AS Nonresident
                    FROM EF{yr}CP
                    WHERE UNITID={uid} AND EFALEVEL=1
                    GROUP BY CIPCODE
                    ORDER BY Total DESC NULLS LAST
                    LIMIT 15
                """).df()
                if not efcp.empty:
                    st.dataframe(efcp.rename(columns={
                        "CIPCODE":"CIP Code","Total":"Total Enrolled",
                        "White":"White","BlackAA":"Black/AA","Hispanic":"Hispanic",
                        "AsianPI":"Asian/PI","Nonresident":"Nonresident"
                    }).reset_index(drop=True), use_container_width=True)
                else:
                    st.info("CIP-level enrollment data not available (4-year institutions only).")
            except Exception as ex:
                st.info(f"CIP enrollment not available. ({ex})")

            # EFIA{yr} — 12-month instructional activity
            st.subheader(f"12-Month Instructional Activity (EFIA{yr})")
            try:
                efia = con.execute(f"""
                    SELECT ACTTYPE, CDACTUA, CNACTUA, CDACTGA,
                           EFTEUG, EFTEGD, FTEUG, FTEGD
                    FROM EFIA{yr} WHERE UNITID={uid}
                """).df()
                if not efia.empty:
                    ACTTYPE_LBL = {1:"Credit hours",2:"Clock hours",3:"Both credit & clock",-2:"N/A"}
                    er = efia.iloc[0]
                    atype = int(er.get("ACTTYPE")) if pd.notna(er.get("ACTTYPE")) else -2
                    st.caption(f"Activity type: **{ACTTYPE_LBL.get(atype, str(atype))}**")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("UG Credit Hours",       fmt(er.get("CDACTUA"), "int"))
                    c2.metric("UG Clock Hours",        fmt(er.get("CNACTUA"), "int"))
                    c3.metric("Grad Credit Hours",     fmt(er.get("CDACTGA"), "int"))
                    c4.metric("Estimated FTE (UG)",    fmt(er.get("EFTEUG"),  "number"))
                    c1, c2 = st.columns(2)
                    c1.metric("Reported FTE UG",       fmt(er.get("FTEUG"),   "number"))
                    c2.metric("Reported FTE Grad",     fmt(er.get("FTEGD"),   "number"))
                else:
                    st.info("12-month instructional activity data not available.")
            except Exception as ex:
                st.info(f"EFIA{yr} not available. ({ex})")

            # EF{yr} — fall enrollment by level (grand total, UG, grad, FT, PT)
            st.subheader(f"Fall Enrollment by Level (EF{yr})")
            EFLEVEL_LBL = {
                10: "All students (total)",
                20: "Undergraduate (total)",
                30: "UG degree/cert-seeking (total)",
                31: "UG first-time",
                34: "UG other degree/cert-seeking",
                35: "UG transfer-ins",
                36: "UG continuing",
                40: "UG non-degree-seeking",
                50: "Graduate",
            }
            try:
                ef = con.execute(f"""
                    SELECT EFLEVEL, EFTOTAL, EFMEN, EFWOM, EFFT, EFFTMEN, EFFTWOM, EFPT, EFPTMEN, EFPTWOM
                    FROM EF{yr} WHERE UNITID={uid}
                    ORDER BY EFLEVEL
                """).df()
                if not ef.empty:
                    ef["Level"] = ef["EFLEVEL"].map(EFLEVEL_LBL).fillna(ef["EFLEVEL"].astype(str))
                    ef_show = ef[["Level","EFTOTAL","EFFT","EFPT","EFMEN","EFWOM"]].rename(columns={
                        "EFTOTAL":"Total","EFFT":"Full-time","EFPT":"Part-time",
                        "EFMEN":"Men","EFWOM":"Women"
                    })
                    st.dataframe(ef_show.reset_index(drop=True), use_container_width=True)

                    # FT vs PT chart for key levels
                    key = ef[ef["EFLEVEL"].isin([10, 20, 50])].copy()
                    if not key.empty:
                        key["Level"] = key["EFLEVEL"].map(EFLEVEL_LBL)
                        melt = key[["Level","EFFT","EFPT"]].melt(
                            id_vars="Level", var_name="Status", value_name="Count"
                        )
                        melt["Status"] = melt["Status"].map({"EFFT":"Full-time","EFPT":"Part-time"})
                        fig = px.bar(melt, x="Level", y="Count", color="Status", barmode="group",
                                     color_discrete_sequence=["#1f77b4","#aec7e8"],
                                     title=f"Full-time vs Part-time by Level (Fall {yr})")
                        fig.update_layout(height=320)
                        st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Fall enrollment by level not available.")
            except Exception as ex:
                st.info(f"EF{yr} not available. ({ex})")

            # EF{yr}D — full retention cohort detail
            st.subheader(f"Retention Rate Cohort Detail (EF{yr}D)")
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**Full-time Cohort (Fall {yr_prev} → Fall {yr})**")
                ft_items = {
                    "Original FT cohort":         row.get("RRFTCT"),
                    "Adjusted FT cohort":         row.get("RRFTCTA"),
                    f"Still enrolled fall {yr}":  row.get("RET_NMF"),
                    "FT retention rate":          fmt(row.get("RET_PCF"), "pct"),
                }
                for lbl, val in ft_items.items():
                    display = val if isinstance(val, str) else fmt(val, "int")
                    st.write(f"**{lbl}:** {display}")
            with c2:
                st.markdown(f"**Part-time Cohort (Fall {yr_prev} → Fall {yr})**")
                pt_items = {
                    "Original PT cohort":         row.get("RRPTCT"),
                    "Adjusted PT cohort":         row.get("RRPTCTA"),
                    f"Still enrolled fall {yr}":  row.get("RET_NMP"),
                    "PT retention rate":          fmt(row.get("RET_PCP"), "pct"),
                }
                for lbl, val in pt_items.items():
                    display = val if isinstance(val, str) else fmt(val, "int")
                    st.write(f"**{lbl}:** {display}")

            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("GRS Cohort Size",                fmt(row.get("GRCOHRT"),   "int"))
            mc2.metric(f"Total Entering UG (fall {yr})", fmt(row.get("UGENTERN"), "int"))
            mc3.metric("GRS Cohort as % of Entering",  fmt(row.get("PGRCOHRT"), "pct"))

        # ── Tab 2: Admissions ─────────────────────────────────────────────────
        with tabs[2]:
            adm = con.execute(f"SELECT * FROM ADM{yr} WHERE UNITID={uid}").df()
            if adm.empty:
                st.info("No admissions data available — this may be an open-admissions institution.")
            else:
                r2 = adm.iloc[0]
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Applicants",   fmt(r2.get("APPLCN"),      "int"))
                c2.metric("Admitted",     fmt(r2.get("ADMSSN"),      "int"))
                c3.metric("Enrolled",     fmt(r2.get("ENRLT"),       "int"))
                c4.metric("Yield Rate",   fmt(row.get("DVADM04"),    "pct"))

                c1, c2 = st.columns(2)
                with c1:
                    st.subheader("SAT Score Ranges")
                    sat = [
                        ("ERW 25th", r2.get("SATVR25")), ("ERW 50th", r2.get("SATVR50")),
                        ("ERW 75th", r2.get("SATVR75")), ("Math 25th", r2.get("SATMT25")),
                        ("Math 50th", r2.get("SATMT50")),("Math 75th", r2.get("SATMT75")),
                    ]
                    sat_df = pd.DataFrame([(k, v) for k, v in sat if pd.notna(v)], columns=["Score", "Value"])
                    if not sat_df.empty:
                        fig = px.bar(sat_df, x="Score", y="Value",
                                     color_discrete_sequence=["#1f77b4"])
                        fig.update_layout(yaxis_range=[200, 800], height=320, showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("SAT scores not reported.")

                with c2:
                    st.subheader("ACT Score Ranges")
                    act = [
                        ("Composite 25th", r2.get("ACTCM25")),
                        ("Composite 50th", r2.get("ACTCM50")),
                        ("Composite 75th", r2.get("ACTCM75")),
                    ]
                    act_df = pd.DataFrame([(k, v) for k, v in act if pd.notna(v)], columns=["Score", "Value"])
                    if not act_df.empty:
                        fig = px.bar(act_df, x="Score", y="Value",
                                     color_discrete_sequence=["#2ca02c"])
                        fig.update_layout(yaxis_range=[1, 36], height=320, showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("ACT scores not reported.")

                sub = [("SAT", r2.get("SATPCT")), ("ACT", r2.get("ACTPCT"))]
                sub_df = pd.DataFrame([(k, v) for k, v in sub if pd.notna(v)], columns=["Test", "%"])
                if not sub_df.empty:
                    st.subheader("Test Score Submission Rates")
                    fig = px.bar(sub_df, x="Test", y="%", color_discrete_sequence=["#9467bd"])
                    fig.update_layout(yaxis_range=[0, 100], height=240, showlegend=False)
                    st.plotly_chart(fig, use_container_width=True)

                # Admission consideration factors (ADMCON1-12)
                ADMCON_LBL = {
                    "ADMCON1": "Secondary school GPA",
                    "ADMCON2": "Secondary school rank",
                    "ADMCON3": "Secondary school record",
                    "ADMCON4": "Completion of college prep program",
                    "ADMCON5": "Recommendations",
                    "ADMCON6": "Formal demonstration of competencies",
                    "ADMCON7": "Admission test scores (SAT/ACT)",
                    "ADMCON8": "TOEFL/other English proficiency test",
                    "ADMCON9": "Other Test (Wonderlic, WISC III, etc.)",
                    "ADMCON10": "Work experience",
                    "ADMCON11": "Extracurricular activities",
                    "ADMCON12": "Portfolio/audition",
                }
                ADMCON_VAL = {1: "Required", 2: "Recommended", 3: "Neither required nor recommended",
                               5: "Considered but not required", -1: "N/A", -2: "Not reported"}
                st.subheader(f"Admission Consideration Factors (ADM{yr})")
                ac_rows = []
                for col, lbl in ADMCON_LBL.items():
                    val = r2.get(col)
                    if pd.notna(val):
                        ac_rows.append({"Factor": lbl, "Status": ADMCON_VAL.get(int(val), str(val))})
                if ac_rows:
                    ac_df = pd.DataFrame(ac_rows)
                    st.dataframe(ac_df, use_container_width=True, hide_index=True)

                # Gender-split applicants/admitted/enrolled (ADM{yr})
                st.subheader("Applicants / Admitted / Enrolled by Gender")
                gender_rows = [
                    ("Men",   r2.get("APPLCNM"),  r2.get("ADMSSNM"),  r2.get("ENRLM")),
                    ("Women", r2.get("APPLCNW"),  r2.get("ADMSSNW"),  r2.get("ENRLW")),
                    ("Total", r2.get("APPLCN"),   r2.get("ADMSSN"),   r2.get("ENRLT")),
                ]
                gdf = pd.DataFrame(gender_rows, columns=["Group","Applicants","Admitted","Enrolled"])
                gdf = gdf.dropna(subset=["Applicants","Admitted"])
                if not gdf.empty:
                    # Add acceptance rate column
                    gdf["Accept %"] = (gdf["Admitted"] / gdf["Applicants"] * 100).round(1)
                    st.dataframe(gdf, use_container_width=True, hide_index=True)

                # DRVADM{yr} gender-split derived rates
                st.subheader(f"Acceptance & Yield Rates by Gender (DRVADM{yr})")
                adm_derived = [
                    ("Accept Rate — Total",     fmt(row.get("DVADM01"), "pct")),
                    ("Accept Rate — Men",        fmt(row.get("DVADM02"), "pct")),
                    ("Accept Rate — Women",      fmt(row.get("DVADM03"), "pct")),
                    ("Yield Rate — Total",       fmt(row.get("DVADM04"), "pct")),
                    ("Yield Rate — Men",         fmt(row.get("DVADM05"), "pct")),
                    ("Yield Rate — Women",       fmt(row.get("DVADM06"), "pct")),
                    ("Yield — FT Men",           fmt(row.get("DVADM07"), "pct")),
                    ("Yield — FT Women",         fmt(row.get("DVADM08"), "pct")),
                    ("Yield — PT Men",           fmt(row.get("DVADM09"), "pct")),
                    ("Yield — PT Women",         fmt(row.get("DVADM10"), "pct")),
                    ("Yield — Full-time Total",  fmt(row.get("DVADM11"), "pct")),
                    ("Yield — Part-time Total",  fmt(row.get("DVADM12"), "pct")),
                ]
                adm_df = pd.DataFrame(adm_derived, columns=["Metric", "Value"])
                adm_df = adm_df[adm_df["Value"] != "N/A"]
                if not adm_df.empty:
                    st.dataframe(adm_df, use_container_width=True, hide_index=True)

                # Admissions requirements from IC{yr}
                try:
                    ic = con.execute(f"""
                        SELECT OPENADMP, DOCPP, DOCPPSP
                        FROM IC{yr} WHERE UNITID={uid}
                    """).df()
                    if not ic.empty:
                        icr = ic.iloc[0]
                        st.subheader(f"Admissions Details (IC{yr})")
                        st.write(f"Open admission: **{'Yes' if icr.get('OPENADMP')==1 else 'No'}**")
                        st.write(f"Offers Doctor's of Pharmacy: **{'Yes' if icr.get('DOCPP')==1 else 'No'}**")
                except Exception:
                    pass

        # ── Tab 3: Completions ────────────────────────────────────────────────
        with tabs[3]:
            deg = [
                ("Doctor's - Research",     row.get("DOCDEGRS")),
                ("Doctor's - Professional", row.get("DOCDEGPP")),
                ("Doctor's - Other",        row.get("DOCDEGOT")),
                ("Master's",                row.get("MASDEG")),
                ("Bachelor's",              row.get("BASDEG")),
                ("Associate's",             row.get("ASCDEG")),
                ("Cert 1-4 years",          row.get("CERT4")),
                ("Cert 1-2 years",          row.get("CERT2")),
                ("Cert ≥12wk, <1yr",        row.get("CERT1B")),
                ("Cert <12wk",              row.get("CERT1A")),
            ]
            deg_df = pd.DataFrame(
                [(k, v) for k, v in deg if pd.notna(v) and v > 0],
                columns=["Award Level", "Count"]
            ).sort_values("Count")
            if not deg_df.empty:
                fig = px.bar(deg_df, x="Count", y="Award Level", orientation="h",
                             color_discrete_sequence=["#17becf"],
                             title=f"Awards Conferred by Level ({year})")
                fig.update_layout(height=420, yaxis_title="")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No completions data.")

            # Top programs from C{yr}_A
            st.subheader("Top 15 Programs (First Major, All Award Levels)")
            try:
                prog = con.execute(f"""
                    SELECT CIPCODE, AWLEVEL, SUM(CTOTALT) AS Total
                    FROM C{yr}_A
                    WHERE UNITID={uid} AND MAJORNUM=1
                    GROUP BY CIPCODE, AWLEVEL
                    ORDER BY Total DESC LIMIT 15
                """).df()
                if not prog.empty:
                    st.dataframe(prog, use_container_width=True)
                else:
                    st.info("No CIP-level data for this institution.")
            except Exception:
                st.info("CIP data not available.")

            # Completions by race/ethnicity from C{yr}_B
            st.subheader(f"Completers by Race/Ethnicity ({year})")
            try:
                cb = con.execute(f"""
                    SELECT CSTOTLT, CSTOTLM, CSTOTLW,
                           CSAIANT, CSASIAT, CSBKAAT, CSHISPT,
                           CSNHPIT, CSWHITT, CS2MORT, CSUNKNT, CSNRALT
                    FROM C{yr}_B WHERE UNITID={uid}
                """).df()
                if not cb.empty:
                    cbr = cb.iloc[0]
                    cb_race = {
                        "AI/AN":       cbr.get("CSAIANT"), "Asian/PI":    cbr.get("CSASIAT"),
                        "Black/AA":    cbr.get("CSBKAAT"), "Hispanic":    cbr.get("CSHISPT"),
                        "NHPI":        cbr.get("CSNHPIT"), "White":       cbr.get("CSWHITT"),
                        "Two+ races":  cbr.get("CS2MORT"), "Unknown":     cbr.get("CSUNKNT"),
                        "Nonresident": cbr.get("CSNRALT"),
                    }
                    cbr_df = pd.DataFrame(
                        [(k, float(v)) for k, v in cb_race.items() if pd.notna(v) and float(v) > 0],
                        columns=["Race/Ethnicity", "Count"]
                    ).sort_values("Count", ascending=False)

                    c1, c2 = st.columns(2)
                    with c1:
                        mc1, mc2 = st.columns(2)
                        mc1.metric("Total Completers", fmt(cbr.get("CSTOTLT"), "int"))
                        mc2.metric("Female", fmt(cbr.get("CSTOTLW"), "int"))
                        if not cbr_df.empty:
                            fig = px.pie(cbr_df, values="Count", names="Race/Ethnicity", hole=0.3,
                                         title="Completers by Race/Ethnicity")
                            st.plotly_chart(fig, use_container_width=True)
                    with c2:
                        st.dataframe(cbr_df.reset_index(drop=True), use_container_width=True)
                else:
                    st.info("No race/ethnicity completions data.")
            except Exception as ex:
                st.info(f"Completions by race not available. ({ex})")

            # Completions by award level, gender, age from C{yr}_C
            st.subheader(f"Completers by Award Level, Gender & Age ({year})")
            try:
                cc = con.execute(f"""
                    SELECT AWLEVELC, CSTOTLT, CSTOTLM, CSTOTLW,
                           CSUND18, CS18_24, CS25_39, CSABV40, CSUNKN
                    FROM C{yr}_C WHERE UNITID={uid}
                    ORDER BY AWLEVELC
                """).df()
                if not cc.empty:
                    st.dataframe(cc.rename(columns={
                        "AWLEVELC":"Award Level Code","CSTOTLT":"Total",
                        "CSTOTLM":"Male","CSTOTLW":"Female",
                        "CSUND18":"< 18","CS18_24":"18-24","CS25_39":"25-39",
                        "CSABV40":"> 40","CSUNKN":"Unknown Age"
                    }), use_container_width=True)
                else:
                    st.info("No completions by award level/age data.")
            except Exception as ex:
                st.info(f"Completions by award level not available. ({ex})")

            # C{yr}DEP — programs offered with distance education options
            st.subheader(f"Programs Offered with Distance Education Options (C{yr}DEP, Top 15 CIPs)")
            try:
                dep = con.execute(f"""
                    SELECT CIPCODE,
                           PTOTAL AS TotalPrograms,
                           PTOTALDE AS TotalWithDE,
                           PBACHL AS Bachelors, PBACHLDE AS BachelorsDE,
                           PMASTR AS Masters, PMASTRDE AS MastersDE,
                           PASSOC AS Associates, PASSOCDE AS AssociatesDE,
                           PDOCRS AS DocResearch, PDOCRSDE AS DocResearchDE
                    FROM C{yr}DEP
                    WHERE UNITID={uid}
                    ORDER BY PTOTALDE DESC NULLS LAST
                    LIMIT 15
                """).df()
                if not dep.empty:
                    st.dataframe(dep.rename(columns={
                        "CIPCODE": "CIP Code",
                        "TotalPrograms": "Total Programs", "TotalWithDE": "Total w/ DE",
                        "Bachelors": "Bach", "BachelorsDE": "Bach DE",
                        "Masters": "Master's", "MastersDE": "Master's DE",
                        "Associates": "Assoc", "AssociatesDE": "Assoc DE",
                        "DocResearch": "Doc-Research", "DocResearchDE": "Doc-Research DE",
                    }), use_container_width=True)
                else:
                    st.info("No distance education program data for this institution.")
            except Exception as ex:
                st.info(f"C{yr}DEP not available. ({ex})")

        # ── Tab 4: Student Outcomes ───────────────────────────────────────────
        with tabs[4]:
            st.subheader(f"Outcome Measures — Cohort entering 2016-17 (DRVOM{yr})")
            st.caption("Rates show % who received an award within 4, 6, or 8 years after entry. OM1=FT first-time; OM2=PT first-time; OM3=FT non-first-time; OM4=PT non-first-time.")

            # Award rate timeline: all four cohorts
            timeline_rows = []
            for group, prefix in [("FT First-time","OM1"), ("PT First-time","OM2"),
                                   ("FT Non-first-time","OM3"), ("PT Non-first-time","OM4")]:
                for yr in ["4","6","8"]:
                    val = row.get(f"{prefix}TOTLAWDP{yr}")
                    if pd.notna(val):
                        timeline_rows.append({"Years": f"{yr} yrs", "Award Rate %": val, "Cohort": group})
            if timeline_rows:
                tdf = pd.DataFrame(timeline_rows)
                fig = px.line(tdf, x="Years", y="Award Rate %", color="Cohort", markers=True,
                              title="Award Rate Progression by Cohort Type")
                fig.update_layout(height=360, yaxis_range=[0, 100])
                st.plotly_chart(fig, use_container_width=True)

            # Detailed table
            om_rows = []
            cohort_labels = [
                ("FT First-time (OM1)", "OM1"),
                ("PT First-time (OM2)", "OM2"),
                ("FT Non-first-time (OM3)", "OM3"),
                ("PT Non-first-time (OM4)", "OM4"),
            ]
            for lbl, prefix in cohort_labels:
                om_rows.append({
                    "Cohort": lbl,
                    "Award 4yr %":  fmt(row.get(f"{prefix}TOTLAWDP4"), "pct"),
                    "Award 6yr %":  fmt(row.get(f"{prefix}TOTLAWDP6"), "pct"),
                    "Award 8yr %":  fmt(row.get(f"{prefix}TOTLAWDP8"), "pct"),
                    "Still Enr 8yr %": fmt(row.get(f"{prefix}TOTLENYP8"), "pct"),
                })
            st.dataframe(pd.DataFrame(om_rows), use_container_width=True, hide_index=True)

            # Pell vs Non-Pell: FT first-time
            st.subheader(f"Pell vs Non-Pell Award Rates — FT First-time (DRVOM{yr})")
            pell_rows = []
            for yr in ["4","6","8"]:
                pell_rows.append({
                    "Years": f"{yr} yrs",
                    "Pell Award %":     fmt(row.get(f"OM1PELLAWDP{yr}"), "pct"),
                    "Non-Pell Award %": fmt(row.get(f"OM1NPELAWDP{yr}"), "pct"),
                })
            pell_df = pd.DataFrame(pell_rows)
            st.dataframe(pell_df, use_container_width=True, hide_index=True)

            # Pell vs Non-Pell chart at 8 years (all cohorts)
            equity_rows = []
            for lbl, p_col, np_col in [
                ("FT first-time",  "OM1PELLAWDP8", "OM1NPELAWDP8"),
                ("PT first-time",  "OM2PELLAWDP8", "OM2NPELAWDP8"),
            ]:
                pv = row.get(p_col); npv = row.get(np_col)
                if pd.notna(pv): equity_rows.append({"Cohort": lbl, "Group": "Pell", "Award Rate %": pv})
                if pd.notna(npv): equity_rows.append({"Cohort": lbl, "Group": "Non-Pell", "Award Rate %": npv})
            if equity_rows:
                eq_df = pd.DataFrame(equity_rows)
                fig = px.bar(eq_df, x="Cohort", y="Award Rate %", color="Group", barmode="group",
                             color_discrete_sequence=["#2ca02c","#ff7f0e"],
                             title="8-Year Award Rate: Pell vs Non-Pell (by Cohort Type)")
                fig.update_layout(yaxis_range=[0, 100], height=320)
                st.plotly_chart(fig, use_container_width=True)

            st.divider()

            # Graduation rates by Pell/SSL status
            st.subheader(f"Graduation Rates: Pell / Subsidized Loan / Neither (GR{yr}_PELL_SSL)")
            try:
                gr_pell = con.execute(f"""
                    SELECT PSGRTYPE, PGADJCT, PGCMTOT,
                           SSADJCT, SSCMTOT, NRADJCT, NRCMTOT,
                           TTADJCT, TTCMTOT
                    FROM GR{yr}_PELL_SSL WHERE UNITID={uid}
                """).df()
                if not gr_pell.empty:
                    for _, gprow in gr_pell.iterrows():
                        ptype = int(gprow["PSGRTYPE"])
                        st.caption(f"Grant type code: {ptype}")
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Pell Cohort",       fmt(gprow.get("PGADJCT"), "int"))
                        c2.metric("Pell Completers",   fmt(gprow.get("PGCMTOT"), "int"))
                        c3.metric("Sub Loan Cohort",   fmt(gprow.get("SSADJCT"), "int"))
                        c4.metric("Sub Loan Compltrs", fmt(gprow.get("SSCMTOT"), "int"))
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Neither Cohort",    fmt(gprow.get("NRADJCT"), "int"))
                        c2.metric("Neither Compltrs",  fmt(gprow.get("NRCMTOT"), "int"))
                        c3.metric("Total Cohort",      fmt(gprow.get("TTADJCT"), "int"))
                        c4.metric("Total Completers",  fmt(gprow.get("TTCMTOT"), "int"))
                        st.divider()
                else:
                    st.info("No Pell/SSL graduation rate data for this institution.")
            except Exception as ex:
                st.info(f"Pell/SSL grad rate data not available. ({ex})")

            # 200% graduation rate
            st.subheader(f"200% Graduation Rate (GR200_{yr2})")
            try:
                gr200 = con.execute(f"SELECT * FROM GR200_{yr2} WHERE UNITID={uid}").df()
                if not gr200.empty:
                    gr200r = gr200.iloc[0]
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("BA Cohort",        fmt(gr200r.get("BAREVCT"), "int"))
                    c2.metric("BA Grad @ 150%",   fmt(gr200r.get("BAGR150"), "int"))
                    c3.metric("BA Grad @ 200%",   fmt(gr200r.get("BAGR200"), "int"))
                    c4.metric("<4yr Grad @ 200%", fmt(gr200r.get("L4GR200"), "int"))

                    rates = [
                        ("150% rate", gr200r.get("BAGR150")),
                        ("200% rate", gr200r.get("BAGR200")),
                    ]
                    rates_df = pd.DataFrame([(k, v) for k, v in rates if pd.notna(v)],
                                             columns=["Threshold", "Completers"])
                    if not rates_df.empty:
                        fig = px.bar(rates_df, x="Threshold", y="Completers",
                                     color="Threshold",
                                     color_discrete_sequence=["#1f77b4","#2ca02c"],
                                     title="Bachelor's Completers at 150% and 200% of Normal Time")
                        fig.update_layout(height=300, showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("200% graduation rate data not available.")
            except Exception as ex:
                st.info(f"200% grad rate not available. ({ex})")

            # Standard graduation rates summary
            st.subheader(f"Standard Graduation Rates (DRVGR{yr})")
            gr_summary = [
                ("Overall 150% Rate",       row.get("GRRTTOT")),
                ("Overall 150% — Men",      row.get("GRRTM")),
                ("Overall 150% — Women",    row.get("GRRTW")),
                ("Bachelor's 4-yr Rate",    row.get("GBA4RTT")),
                ("Bachelor's 5-yr Rate",    row.get("GBA5RTT")),
                ("Bachelor's 6-yr Rate",    row.get("GBA6RTT")),
                ("Bach 6-yr — Men",         row.get("GBA6RTM")),
                ("Bach 6-yr — Women",       row.get("GBA6RTW")),
                ("Transfer-in Grad Rate",   row.get("GBATRRT")),
                ("Pell 150% Rate",          row.get("PGGRRTT")),
                ("Pell Bach 6-yr Rate",     row.get("PGBA6RT")),
                ("Sub-Loan 150% Rate",      row.get("SSGRRTT")),
                ("Sub-Loan Bach 6-yr Rate", row.get("SSBA6RT")),
                ("Neither 150% Rate",       row.get("NRGRRTT")),
                ("Neither Bach 6-yr Rate",  row.get("NRBA6RT")),
                ("Transfer-out Rate",       row.get("TRRTTOT")),
            ]
            grdf = pd.DataFrame([(k, v) for k, v in gr_summary if pd.notna(v)],
                                  columns=["Metric", "Rate (%)"])
            if not grdf.empty:
                fig = px.bar(grdf, x="Metric", y="Rate (%)",
                             color_discrete_sequence=["#9467bd"],
                             title=f"Graduation Rates (DRVGR{yr})")
                fig.update_layout(height=380, showlegend=False, yaxis_range=[0, 100],
                                  xaxis_tickangle=-30)
                st.plotly_chart(fig, use_container_width=True)

            # Race/ethnicity graduation rates from DRVGR2024
            st.subheader(f"Graduation Rates by Race/Ethnicity — 150% (DRVGR{yr})")
            gr_race = [
                ("AI/AN",       row.get("GRRTAN")),
                ("Asian/PI",    row.get("GRRTAP")),
                ("Asian",       row.get("GRRTAS")),
                ("NHPI",        row.get("GRRTNH")),
                ("Black/AA",    row.get("GRRTBK")),
                ("Hispanic",    row.get("GRRTHS")),
                ("White",       row.get("GRRTWH")),
                ("Two+ races",  row.get("GRRT2M")),
                ("Unknown",     row.get("GRRTUN")),
                ("Nonresident", row.get("GRRTNR")),
            ]
            gr_race_df = pd.DataFrame([(k, v) for k, v in gr_race if pd.notna(v) and v > 0],
                                       columns=["Race/Ethnicity", "150% Grad Rate %"])
            if not gr_race_df.empty:
                c1, c2 = st.columns(2)
                with c1:
                    fig = px.bar(gr_race_df.sort_values("150% Grad Rate %", ascending=True),
                                 x="150% Grad Rate %", y="Race/Ethnicity", orientation="h",
                                 color="Race/Ethnicity",
                                 title="150% Grad Rate by Race/Ethnicity",
                                 color_discrete_sequence=px.colors.qualitative.Set3)
                    fig.update_layout(height=380, showlegend=False,
                                      xaxis_range=[0, 100], yaxis_title="")
                    st.plotly_chart(fig, use_container_width=True)
                with c2:
                    # Bachelor's 6-yr rates by race from DRVGR2024
                    gr_race_6yr = [
                        ("AI/AN",       row.get("GBA6RTAN")),
                        ("Asian/PI",    row.get("GBA6RTAP")),
                        ("Asian",       row.get("GBA6RTAS")),
                        ("NHPI",        row.get("GBA6RTNH")),
                        ("Black/AA",    row.get("GBA6RTBK")),
                        ("Hispanic",    row.get("GBA6RTHS")),
                        ("White",       row.get("GBA6RTWH")),
                        ("Two+ races",  row.get("GBA6RT2M")),
                        ("Unknown",     row.get("GBA6RTUN")),
                        ("Nonresident", row.get("GBA6RTNR")),
                    ]
                    gr_6yr_df = pd.DataFrame([(k, v) for k, v in gr_race_6yr if pd.notna(v) and v > 0],
                                              columns=["Race/Ethnicity", "Bach 6-yr Rate %"])
                    if not gr_6yr_df.empty:
                        fig = px.bar(gr_6yr_df.sort_values("Bach 6-yr Rate %", ascending=True),
                                     x="Bach 6-yr Rate %", y="Race/Ethnicity", orientation="h",
                                     color="Race/Ethnicity",
                                     title="Bachelor's 6-yr Rate by Race/Ethnicity",
                                     color_discrete_sequence=px.colors.qualitative.Pastel)
                        fig.update_layout(height=380, showlegend=False,
                                          xaxis_range=[0, 100], yaxis_title="")
                        st.plotly_chart(fig, use_container_width=True)

            # GR{yr} — graduation rates by race/ethnicity (raw table)
            st.subheader(f"Graduation Rate by Race/Ethnicity (GR{yr}, 150% of Normal Time)")
            try:
                # GRTYPE=8: bachelor's adjusted cohort; GRTYPE=9: completers within 150%
                gr_cohort = con.execute(f"""
                    SELECT GRTYPE, GRTOTLT, GRTOTLM, GRTOTLW,
                           GRWHITT, GRBKAAT, GRHISPT, GRASIAT,
                           GRAIANT, GR2MORT, GRUNKNT, GRNRALT
                    FROM GR{yr}
                    WHERE UNITID={uid} AND GRTYPE IN (2, 3, 8, 9)
                    ORDER BY GRTYPE
                """).df()
                if not gr_cohort.empty:
                    GRTYPE_LBL = {
                        2: "All students — adjusted cohort (4-yr)",
                        3: "All students — completers within 150% (4-yr)",
                        8: "Bachelor's — adjusted cohort",
                        9: "Bachelor's — completers within 150%",
                    }
                    gr_cohort["Type"] = gr_cohort["GRTYPE"].map(GRTYPE_LBL)
                    # Show the bachelor's cohort and completers side by side
                    cohort_row = gr_cohort[gr_cohort["GRTYPE"] == 8]
                    complete_row = gr_cohort[gr_cohort["GRTYPE"] == 9]
                    if not cohort_row.empty and not complete_row.empty:
                        cr = cohort_row.iloc[0]
                        co = complete_row.iloc[0]
                        race_groups = {
                            "White":       ("GRWHITT", cr, co),
                            "Black/AA":    ("GRBKAAT", cr, co),
                            "Hispanic":    ("GRHISPT", cr, co),
                            "Asian/PI":    ("GRASIAT", cr, co),
                            "AI/AN":       ("GRAIANT", cr, co),
                            "Two+ races":  ("GR2MORT", cr, co),
                            "Nonresident": ("GRNRALT", cr, co),
                        }
                        race_rows = []
                        for grp, (col, ch, co_) in race_groups.items():
                            coh = ch.get(col)
                            comp = co_.get(col)
                            if pd.notna(coh) and float(coh) > 0 and pd.notna(comp):
                                rate = 100 * float(comp) / float(coh)
                                race_rows.append({"Race/Ethnicity": grp,
                                                  "Cohort": int(coh),
                                                  "Completers": int(comp),
                                                  "Grad Rate %": round(rate, 1)})
                        if race_rows:
                            race_gr_df = pd.DataFrame(race_rows).sort_values("Grad Rate %", ascending=False)
                            c1, c2 = st.columns(2)
                            with c1:
                                st.dataframe(race_gr_df.reset_index(drop=True), use_container_width=True)
                            with c2:
                                fig = px.bar(race_gr_df, x="Race/Ethnicity", y="Grad Rate %",
                                             color="Race/Ethnicity",
                                             title="150% Graduation Rate by Race/Ethnicity",
                                             color_discrete_sequence=px.colors.qualitative.Set2)
                                fig.update_layout(height=360, showlegend=False,
                                                  yaxis_range=[0, 100])
                                st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.dataframe(gr_cohort[["Type","GRTOTLT","GRTOTLM","GRTOTLW"]].rename(
                            columns={"Type":"Cohort Type","GRTOTLT":"Total",
                                     "GRTOTLM":"Men","GRTOTLW":"Women"}
                        ).reset_index(drop=True), use_container_width=True)
                else:
                    st.info(f"GR{yr} race/ethnicity grad rate data not available.")
            except Exception as ex:
                st.info(f"GR{yr} not available. ({ex})")

            # GR{yr}_L2 — graduation rates for less-than-2-year institutions
            st.subheader(f"Graduation Rates — Less-than-2-Year Cohort (GR{yr}_L2)")
            try:
                gr_l2 = con.execute(f"SELECT * FROM GR{yr}_L2 WHERE UNITID={uid}").df()
                if not gr_l2.empty:
                    lr = gr_l2.iloc[0]
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Adjusted Cohort",    fmt(lr.get("LINE_10"), "int"))
                    c2.metric("Completers",         fmt(lr.get("LINE_45"), "int"))
                    c3.metric("Still Enrolled",     fmt(lr.get("LINE_50"), "int"))
                    c4.metric("Transfer-out",       fmt(lr.get("LINE_55"), "int"))
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Pell Cohort",        fmt(lr.get("PGLIN10"), "int"))
                    c2.metric("Pell Completers",    fmt(lr.get("PGLIN45"), "int"))
                    c3.metric("Sub-Loan Cohort",    fmt(lr.get("SSLIN10"), "int"))
                    c4.metric("Sub-Loan Completers",fmt(lr.get("SSLIN45"), "int"))
                else:
                    st.info("Less-than-2-year graduation rate data not applicable for this institution.")
            except Exception as ex:
                st.info(f"GR{yr}_L2 not available. ({ex})")

        # ── Tab 5: Costs ──────────────────────────────────────────────────────
        with tabs[5]:
            st.subheader(f"Cost of Attendance (DRVCOST{yr} & COST1_{yr})")
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("In-State COA",      fmt(row.get("CINSON"),  "dollar"))
            mc2.metric("Out-of-State COA",  fmt(row.get("COTSON"),  "dollar"))
            mc3.metric("In-District COA",   fmt(row.get("CINDON"),  "dollar"))
            mc4.metric(f"Tuition {year}",    fmt(row.get("TUFEYR3"), "dollar"))

            try:
                cost1 = con.execute(f"SELECT * FROM COST1_{yr} WHERE UNITID={uid}").df()
                if not cost1.empty:
                    cr = cost1.iloc[0]

                    # Current year cost components
                    st.subheader(f"{year} Cost Components")
                    cost_items = [
                        ("In-district Tuition+Fees", cr.get("CHG1AY3")),
                        ("In-state Tuition+Fees",    cr.get("CHG2AY3")),
                        ("Out-of-state Tuition+Fees",cr.get("CHG3AY3")),
                        ("Books & Supplies",          cr.get("CHG4AY3")),
                        ("On-campus Room & Board",    cr.get("CHG5AY3")),
                        ("Off-campus Room & Board",   cr.get("CHG7AY3")),
                        ("On-campus Other Expenses",  cr.get("CHG6AY3")),
                        ("Off-campus Other Expenses", cr.get("CHG8AY3")),
                    ]
                    cost_df = pd.DataFrame(
                        [(k, float(v)) for k, v in cost_items if pd.notna(v) and float(v) > 0],
                        columns=["Component", "Amount ($)"]
                    )
                    if not cost_df.empty:
                        fig = px.bar(cost_df, x="Component", y="Amount ($)",
                                     color_discrete_sequence=["#1f77b4"],
                                     title=f"Cost Components {year}")
                        fig.update_layout(height=380, showlegend=False)
                        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                        st.plotly_chart(fig, use_container_width=True)

                    # Tuition trend (in-state, 4 years)
                    st.subheader("Published In-State Tuition & Fees Trend")
                    trend = [
                        (_ay0, cr.get("CHG2AY0")),
                        (_ay1, cr.get("CHG2AY1")),
                        (_ay2, cr.get("CHG2AY2")),
                        (_ay3, cr.get("CHG2AY3")),
                    ]
                    trend_df = pd.DataFrame(
                        [(yr, float(v)) for yr, v in trend if pd.notna(v)],
                        columns=["Year", "Tuition & Fees ($)"]
                    )
                    if len(trend_df) > 1:
                        fig = px.line(trend_df, x="Year", y="Tuition & Fees ($)",
                                      markers=True, title="In-State Tuition & Fees (4-Year Trend)")
                        fig.update_layout(height=320)
                        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                        st.plotly_chart(fig, use_container_width=True)

                    # Out-of-state trend
                    oos_trend = [
                        (_ay0, cr.get("CHG3AY0")),
                        (_ay1, cr.get("CHG3AY1")),
                        (_ay2, cr.get("CHG3AY2")),
                        (_ay3, cr.get("CHG3AY3")),
                    ]
                    oos_df = pd.DataFrame(
                        [(yr, float(v)) for yr, v in oos_trend if pd.notna(v)],
                        columns=["Year", "Tuition & Fees ($)"]
                    )
                    if len(oos_df) > 1:
                        fig = px.line(oos_df, x="Year", y="Tuition & Fees ($)",
                                      markers=True, title="Out-of-State Tuition & Fees (4-Year Trend)",
                                      color_discrete_sequence=["#d62728"])
                        fig.update_layout(height=320)
                        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                        st.plotly_chart(fig, use_container_width=True)

                    # Room and board
                    c1, c2 = st.columns(2)
                    c1.metric("Room Charge",  fmt(cr.get("ROOMAMT"),  "dollar"))
                    c2.metric("Board Charge", fmt(cr.get("BOARDAMT"), "dollar"))
                else:
                    st.info(f"Detailed cost data (COST1_{yr}) not available.")
            except Exception as ex:
                st.info(f"Cost detail not available. ({ex})")

            # Net price by income bracket
            st.subheader(f"Average Net Price by Income Bracket (COST2_{yr}_NETPRICE)")
            try:
                np_data = con.execute(f"""
                    SELECT NPIST2, NPIS412, NPIS422, NPIS432, NPIS442, NPIS452
                    FROM COST2_{yr}_NETPRICE WHERE UNITID={uid}
                """).df()
                if not np_data.empty:
                    npr = np_data.iloc[0]
                    overall_np = npr.get("NPIST2")
                    if pd.notna(overall_np):
                        st.metric("Overall Avg Net Price (grant/scholarship recipients)", fmt(overall_np, "dollar"))
                    income_items = [
                        ("$0–30,000",       npr.get("NPIS412")),
                        ("$30,001–48,000",  npr.get("NPIS422")),
                        ("$48,001–75,000",  npr.get("NPIS432")),
                        ("$75,001–110,000", npr.get("NPIS442")),
                        ("Over $110,000",   npr.get("NPIS452")),
                    ]
                    np_df = pd.DataFrame(
                        [(k, float(v)) for k, v in income_items if pd.notna(v)],
                        columns=["Family Income", "Avg Net Price ($)"]
                    )
                    if not np_df.empty:
                        fig = px.bar(np_df, x="Family Income", y="Avg Net Price ($)",
                                     color="Family Income",
                                     color_discrete_sequence=px.colors.sequential.Blues_r[:5],
                                     title="Average Net Price by Family Income (In-State, Title IV Recipients)")
                        fig.update_layout(height=360, showlegend=False)
                        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("Net price by income bracket not available.")
                else:
                    st.info("Net price data not available.")
            except Exception as ex:
                st.info(f"Net price by income not available. ({ex})")

        # ── Tab 6: Financial Aid ──────────────────────────────────────────────
        with tabs[6]:
            c1, c2, c3 = st.columns(3)
            c1.metric("% Receiving Any Aid",    fmt(row.get("ANYAIDP"),  "pct"))
            c2.metric("% Receiving Pell Grant", fmt(row.get("PGRNT_P"), "pct"))
            c3.metric("Avg Pell Grant",         fmt(row.get("PGRNT_A"), "dollar"))

            aid_items = [
                ("Any Aid",          row.get("ANYAIDP")),
                ("Any Grant",        row.get("AGRNT_P")),
                ("Pell Grant",       row.get("PGRNT_P")),
                ("Federal Grant",    row.get("FGRNT_P")),
                ("State Grant",      row.get("SGRNT_P")),
                ("Institutional Aid",row.get("IGRNT_P")),
                ("Student Loans",    row.get("LOAN_P")),
            ]
            pct_df = pd.DataFrame(
                [(k, v) for k, v in aid_items if pd.notna(v)],
                columns=["Aid Type", "% Receiving"]
            )
            if not pct_df.empty:
                fig = px.bar(pct_df, x="Aid Type", y="% Receiving",
                             color_discrete_sequence=["#1f77b4"],
                             title="% of Full-time First-time Students Receiving Aid")
                fig.update_layout(yaxis_range=[0, 100], height=320, showlegend=False)
                st.plotly_chart(fig, use_container_width=True)

            avg_items = [
                ("Any Grant",     row.get("AGRNT_A")),
                ("Pell Grant",    row.get("PGRNT_A")),
                ("Student Loans", row.get("LOAN_A")),
            ]
            avg_df = pd.DataFrame(
                [(k, v) for k, v in avg_items if pd.notna(v)],
                columns=["Aid Type", "Avg Amount ($)"]
            )
            if not avg_df.empty:
                fig = px.bar(avg_df, x="Aid Type", y="Avg Amount ($)",
                             color_discrete_sequence=["#ff7f0e"],
                             title="Average Aid Amounts")
                fig.update_layout(height=280, showlegend=False)
                fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                st.plotly_chart(fig, use_container_width=True)

            coa = row.get("CINSON"); avg_grnt = row.get("AGRNT_A")
            if pd.notna(coa) and pd.notna(avg_grnt):
                st.metric("Estimated Net Price (In-State COA − Avg Grant)", f"${coa - avg_grnt:,.0f}")

            # COST2_{yr}_FINANCIALAID — FA amounts for FTFT students
            st.subheader(f"Financial Aid for Full-time First-time Students (COST2_{yr}_FINANCIALAID)")
            try:
                fa2 = con.execute(f"""
                    SELECT GISTN2, GISTT2, GISTA2,
                           GISTN1, GISTT1, GISTA1,
                           GISTN0, GISTT0, GISTA0
                    FROM COST2_{yr}_FINANCIALAID WHERE UNITID={uid}
                """).df()
                if not fa2.empty:
                    far = fa2.iloc[0]
                    fa_trend = [
                        (_ay0, far.get("GISTN0"), far.get("GISTA0")),
                        (_ay1, far.get("GISTN1"), far.get("GISTA1")),
                        (_ay2, far.get("GISTN2"), far.get("GISTA2")),
                    ]
                    fa_df = pd.DataFrame(
                        [(yr, float(n) if pd.notna(n) else None, float(a) if pd.notna(a) else None)
                         for yr, n, a in fa_trend],
                        columns=["Year", "# Receiving Grant Aid", "Avg Grant Amount ($)"]
                    ).dropna(subset=["Avg Grant Amount ($)"])
                    if not fa_df.empty:
                        c1, c2 = st.columns(2)
                        with c1:
                            fig = px.bar(fa_df, x="Year", y="# Receiving Grant Aid",
                                         title="# FTFT Students Receiving Grant Aid (3-year trend)",
                                         color_discrete_sequence=["#2ca02c"])
                            fig.update_layout(height=300)
                            st.plotly_chart(fig, use_container_width=True)
                        with c2:
                            fig = px.line(fa_df, x="Year", y="Avg Grant Amount ($)",
                                          markers=True,
                                          title="Avg Grant Aid Amount (3-year trend)",
                                          color_discrete_sequence=["#d62728"])
                            fig.update_layout(height=300)
                            fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                            st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("COST2 financial aid data not available.")
            except Exception as ex:
                st.info(f"COST2 financial aid not available. ({ex})")

            # Military / Veterans Benefits (SFAV{sfayr})
            st.subheader(f"Military Servicemembers & Veterans Benefits (SFAV{sfayr})")
            try:
                sfav = con.execute(f"""
                    SELECT PARTVT, PO9, DOD,
                           PO9_N, PO9_T, PO9_A,
                           DOD_N, DOD_T, DOD_A
                    FROM SFAV{sfayr} WHERE UNITID={uid}
                """).df()
                if not sfav.empty:
                    sv = sfav.iloc[0]
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Participates in VA benefits",   "Yes" if sv.get("PARTVT")==1 else "No/N/A")
                    c2.metric("Post-9/11 GI Bill recipients",  fmt(sv.get("PO9_N"), "int"))
                    c3.metric("DoD Tuition Assist recipients", fmt(sv.get("DOD_N"),  "int"))
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Avg Post-9/11 GI Bill amount",  fmt(sv.get("PO9_A"),  "dollar"))
                    c2.metric("Total Post-9/11 GI Bill paid",  fmt(sv.get("PO9_T"),  "dollar"))
                    c3.metric("Avg DoD TA amount",             fmt(sv.get("DOD_A"),  "dollar"))
                else:
                    st.info("Veterans benefits data not available.")
            except Exception as ex:
                st.info(f"Veterans data not available. ({ex})")

        # ── Tab 7: Finance ────────────────────────────────────────────────────
        with tabs[7]:
            ctrl = row.get("CONTROL")
            prefix = "F1" if ctrl == 1 else ("F2" if ctrl == 2 else "F3")
            acctg  = "GASB (Public)" if ctrl == 1 else ("FASB (Private NP)" if ctrl == 2 else "For-profit")
            st.caption(f"Accounting standard: **{acctg}**")

            rev_cols = {
                "F1": {"F1TUFEPC":"Tuition & Fees","F1STAPPC":"State Approp.",
                       "F1LCAPPC":"Local Approp.","F1GVGCPC":"Govt Grants","F1PGGCPC":"Private Gifts",
                       "F1INVRPC":"Investment","F1OTRVPC":"Other"},
                "F2": {"F2TUFEPC":"Tuition & Fees","F2GVGCPC":"Govt Grants",
                       "F2PGGCPC":"Private Gifts/Contracts","F2INVRPC":"Investment","F2OTRVPC":"Other"},
                "F3": {"F3TUFEPC":"Tuition & Fees","F3SSEAPC":"Educational Activities",
                       "F3GVGCPC":"Govt Grants","F3OTRVPC":"Other"},
            }
            exp_cols = {
                f"{prefix}INSTPC":"Instruction", f"{prefix}RSRCPC":"Research",
                f"{prefix}PBSVPC":"Public Service", f"{prefix}ACSPPC":"Academic Support",
                f"{prefix}STSVPC":"Student Services", f"{prefix}INSUPC":"Institutional Support",
                f"{prefix}OTEXPC":"Other",
            }

            try:
                all_fcols = list(rev_cols[prefix].keys()) + list(exp_cols.keys()) + \
                            [f"{prefix}CORREV", f"{prefix}COREXP", f"{prefix}ENDMFT"]
                fin = con.execute(
                    f"SELECT {','.join(all_fcols)} FROM DRVF{yr} WHERE UNITID={uid}"
                ).df()

                if not fin.empty:
                    fr = fin.iloc[0]
                    c1, c2 = st.columns(2)

                    with c1:
                        rev_data = [(lbl, fr.get(col)) for col, lbl in rev_cols[prefix].items()
                                    if pd.notna(fr.get(col)) and fr.get(col, 0) > 0]
                        if rev_data:
                            rdf = pd.DataFrame(rev_data, columns=["Source", "%"])
                            fig = px.pie(rdf, values="%", names="Source", hole=0.35,
                                         title="Revenue Sources (% of total)")
                            fig.update_layout(height=360)
                            st.plotly_chart(fig, use_container_width=True)

                    with c2:
                        exp_data = [(lbl, fr.get(col)) for col, lbl in exp_cols.items()
                                    if pd.notna(fr.get(col)) and fr.get(col, 0) > 0]
                        if exp_data:
                            edf = pd.DataFrame(exp_data, columns=["Category", "%"])
                            fig = px.pie(edf, values="%", names="Category", hole=0.35,
                                         title="Expense Categories (% of total)")
                            fig.update_layout(height=360)
                            st.plotly_chart(fig, use_container_width=True)

                    mc1, mc2, mc3 = st.columns(3)
                    mc1.metric("Core Revenues",  fmt(fr.get(f"{prefix}CORREV"), "dollar"))
                    mc2.metric("Core Expenses",  fmt(fr.get(f"{prefix}COREXP"), "dollar"))
                    mc3.metric("Endowment/FTE",  fmt(fr.get(f"{prefix}ENDMFT"), "dollar"))
                else:
                    st.info("Finance data not available for this institution.")
            except Exception as ex:
                st.warning(f"Finance query error: {ex}")

        # ── Tab 8: Faculty & Staff ────────────────────────────────────────────
        with tabs[8]:
            ARANK_LBL = {
                1:"Professor", 2:"Assoc Professor", 3:"Asst Professor",
                4:"Instructor", 5:"Lecturer", 6:"No Academic Rank", 7:"All Ranks",
            }
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total FTE Staff",       fmt(row.get("SFTETOTL"), "int"))
            c2.metric("Instructional FTE",     fmt(row.get("SFTEINST"), "int"))
            c3.metric("Avg Faculty Salary",    fmt(row.get("SALTOTL"),  "dollar"))
            c4.metric("Student-Faculty Ratio", f"{row.get('STUFACR'):.0f}:1" if pd.notna(row.get("STUFACR")) else "N/A")

            st.divider()

            # FTE by occupational category
            fte_items = [
                ("Instruction",                row.get("SFTEINST")),
                ("Research",                   row.get("SFTERSRC")),
                ("Public Service",             row.get("SFTEPBSV")),
                ("Management",                 row.get("SFTEMNGM")),
                ("Business & Financial Ops",   row.get("SFTEBFO")),
                ("Computer, Eng & Science",    row.get("SFTECES")),
                ("Community Svc, Legal, Arts", row.get("SFTECLAM")),
                ("Healthcare",                 row.get("SFTEHLTH")),
                ("Librarians & Curators",      row.get("SFTELCA")),
                ("Student & Academic Affairs", row.get("SFTEOTIS")),
                ("Office & Admin Support",     row.get("SFTEOFAS")),
                ("Service",                    row.get("SFTESRVC")),
                ("Natural Res, Constr & Maint",row.get("SFTENRCM")),
                ("Production & Transport",     row.get("SFTEPTMM")),
                ("Sales & Related",            row.get("SFTESALE")),
            ]
            fte_df = pd.DataFrame(
                [(k, float(v)) for k, v in fte_items if pd.notna(v) and float(v) > 0],
                columns=["Occupational Category", "FTE"]
            ).sort_values("FTE", ascending=True)

            if not fte_df.empty:
                fig = px.bar(fte_df, x="FTE", y="Occupational Category", orientation="h",
                             color_discrete_sequence=["#1f77b4"],
                             title=f"FTE Staff by Occupational Category (DRVHR{yr})")
                fig.update_layout(height=420, yaxis_title="", showlegend=False)
                st.plotly_chart(fig, use_container_width=True)

            # EAP{yr} — FT vs PT head counts
            st.subheader(f"Staff Counts: Full-time vs Part-time (EAP{yr})")
            try:
                eap = con.execute(f"""
                    SELECT OCCUPCAT, EAPFT, EAPPT, EAPTOT
                    FROM EAP{yr}
                    WHERE UNITID={uid} AND FACSTAT=0
                    ORDER BY EAPTOT DESC NULLS LAST
                """).df()
                if not eap.empty:
                    eap["Category"] = eap["OCCUPCAT"].map(OCC_LABELS).fillna(eap["OCCUPCAT"].astype(str))
                    eap = eap.rename(columns={"EAPFT":"Full-time","EAPPT":"Part-time","EAPTOT":"Total"})
                    st.dataframe(eap[["Category","Full-time","Part-time","Total"]].reset_index(drop=True),
                                 use_container_width=True)
                else:
                    st.info("No EAP staff count data for this institution.")
            except Exception as ex:
                st.info(f"Staff count data not available. ({ex})")

            # S{yr}_SIS — FT instructional staff by tenure status
            st.subheader(f"Full-time Instructional Staff by Tenure Status (S{yr}_SIS)")
            try:
                sis = con.execute(f"""
                    SELECT FACSTAT, SISTOTL, SISPROF, SISASCP, SISASTP, SISINST, SISLECT, SISNORK
                    FROM S{yr}_SIS WHERE UNITID={uid}
                    ORDER BY FACSTAT
                """).df()
                if not sis.empty:
                    sis["Status"] = sis["FACSTAT"].map(FACSTAT_MAP).fillna(sis["FACSTAT"].astype(str))
                    sis = sis.rename(columns={
                        "SISTOTL":"Total","SISPROF":"Professor","SISASCP":"Assoc Prof",
                        "SISASTP":"Asst Prof","SISINST":"Instructor",
                        "SISLECT":"Lecturer","SISNORK":"No Rank"
                    })
                    st.dataframe(sis[["Status","Total","Professor","Assoc Prof","Asst Prof",
                                       "Instructor","Lecturer","No Rank"]].reset_index(drop=True),
                                 use_container_width=True)

                    # Tenure bar chart for FACSTAT=0 (total)
                    total_row = sis[sis["FACSTAT"] == 0]
                    if not total_row.empty:
                        tr = total_row.iloc[0]
                        rank_vals = [
                            ("Professor", tr.get("Professor")), ("Assoc Prof", tr.get("Assoc Prof")),
                            ("Asst Prof", tr.get("Asst Prof")), ("Instructor", tr.get("Instructor")),
                            ("Lecturer", tr.get("Lecturer")),   ("No Rank", tr.get("No Rank")),
                        ]
                        rank_df = pd.DataFrame([(k, v) for k, v in rank_vals if pd.notna(v) and float(v) > 0],
                                                columns=["Rank", "Count"])
                        if not rank_df.empty:
                            fig = px.bar(rank_df, x="Rank", y="Count",
                                         color_discrete_sequence=["#9467bd"],
                                         title="FT Instructional Staff by Academic Rank (All Tenure Statuses)")
                            fig.update_layout(height=320, showlegend=False)
                            st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("No tenure status data for this institution.")
            except Exception as ex:
                st.info(f"Tenure status data not available. ({ex})")

            # S{yr}_IS — FT instructional staff by race/ethnicity
            st.subheader(f"Full-time Instructional Staff by Race/Ethnicity (S{yr}_IS)")
            try:
                sis_race = con.execute(f"""
                    SELECT FACSTAT, ARANK,
                           HRTOTLT, HRTOTLM, HRTOTLW,
                           HRAIANT, HRASIAT, HRBKAAT, HRHISPT,
                           HRNHPIT, HRWHITT, HR2MORT, HRUNKNT, HRNRALT
                    FROM S{yr}_IS
                    WHERE UNITID={uid} AND SISCAT=1 AND FACSTAT=0 AND ARANK=0
                    LIMIT 1
                """).df()
                if not sis_race.empty:
                    sr = sis_race.iloc[0]
                    race_instruct = {
                        "AI/AN":       sr.get("HRAIANT"),
                        "Asian/PI":    sr.get("HRASIAT"),
                        "Black/AA":    sr.get("HRBKAAT"),
                        "Hispanic":    sr.get("HRHISPT"),
                        "NHPI":        sr.get("HRNHPIT"),
                        "White":       sr.get("HRWHITT"),
                        "Two+ races":  sr.get("HR2MORT"),
                        "Unknown":     sr.get("HRUNKNT"),
                        "Nonresident": sr.get("HRNRALT"),
                    }
                    c1, c2 = st.columns(2)
                    with c1:
                        st.metric("Total FT Instructional Staff", fmt(sr.get("HRTOTLT"), "int"))
                        st.metric("Male",   fmt(sr.get("HRTOTLM"), "int"))
                        st.metric("Female", fmt(sr.get("HRTOTLW"), "int"))
                    with c2:
                        race_is_df = pd.DataFrame(
                            [(k, float(v)) for k, v in race_instruct.items() if pd.notna(v) and float(v) > 0],
                            columns=["Race/Ethnicity", "Count"]
                        )
                        if not race_is_df.empty:
                            fig = px.pie(race_is_df, values="Count", names="Race/Ethnicity", hole=0.35,
                                         title="FT Instructional Staff by Race/Ethnicity",
                                         color_discrete_sequence=px.colors.qualitative.Set2)
                            fig.update_layout(height=320)
                            st.plotly_chart(fig, use_container_width=True)

                    # Breakdown by rank (SISCAT 101-106 = professor through other)
                    sis_rank = con.execute(f"""
                        SELECT ARANK, HRTOTLT, HRWHITT, HRBKAAT, HRHISPT, HRASIAT, HRAIANT, HR2MORT, HRNRALT
                        FROM S{yr}_IS
                        WHERE UNITID={uid} AND SISCAT BETWEEN 101 AND 106 AND FACSTAT=10 AND ARANK IN (1,2,3,4,5,6)
                        ORDER BY ARANK
                    """).df()
                    if not sis_rank.empty:
                        sis_rank["Rank"] = sis_rank["ARANK"].map(ARANK_LBL).fillna(sis_rank["ARANK"].astype(str))
                        sis_rank = sis_rank.rename(columns={
                            "HRTOTLT": "Total", "HRWHITT": "White", "HRBKAAT": "Black/AA",
                            "HRHISPT": "Hispanic", "HRASIAT": "Asian/PI",
                            "HRAIANT": "AI/AN", "HR2MORT": "Two+", "HRNRALT": "Nonresident"
                        })
                        st.dataframe(sis_rank[["Rank","Total","White","Black/AA","Hispanic",
                                                "Asian/PI","AI/AN","Two+","Nonresident"]].reset_index(drop=True),
                                     use_container_width=True)
                else:
                    st.info("No instructional staff race/ethnicity data for this institution.")
            except Exception as ex:
                st.info(f"S{yr}_IS not available. ({ex})")

            # S{yr}_NH — New hires
            st.subheader(f"New Hires (S{yr}_NH)")
            try:
                nh = con.execute(f"""
                    SELECT OCCUPCAT, HRTOTLT, HRWHITT, HRBKAAT, HRHISPT, HRASIAT,
                           HRAIANT, HR2MORT, HRUNKNT, HRNRALT, HRTOTLM, HRTOTLW
                    FROM S{yr}_NH
                    WHERE UNITID={uid} AND FACSTAT=0 AND SGTYPE=1
                    ORDER BY HRTOTLT DESC NULLS LAST
                """).df()
                if not nh.empty:
                    nh["Category"] = nh["OCCUPCAT"].map(OCC_LABELS).fillna(nh["OCCUPCAT"].astype(str))
                    nh = nh.rename(columns={
                        "HRTOTLT":"Total","HRWHITT":"White","HRBKAAT":"Black/AA",
                        "HRHISPT":"Hispanic","HRASIAT":"Asian/PI","HRAIANT":"AI/AN",
                        "HR2MORT":"Two+","HRUNKNT":"Unknown","HRNRALT":"Nonresident",
                        "HRTOTLM":"Male","HRTOTLW":"Female"
                    })
                    st.dataframe(nh[["Category","Total","Male","Female",
                                      "White","Black/AA","Hispanic","Asian/PI","AI/AN",
                                      "Two+","Unknown","Nonresident"]].reset_index(drop=True),
                                 use_container_width=True)
                else:
                    st.info("No new hire data for this institution.")
            except Exception as ex:
                st.info(f"New hire data not available. ({ex})")

            # Salary by academic rank
            st.subheader(f"Average 9-month Salary by Academic Rank (DRVHR{yr})")
            sal_items = [
                ("All Ranks",       row.get("SALTOTL")),
                ("Professor",       row.get("SALPROF")),
                ("Assoc Professor", row.get("SALASSC")),
                ("Asst Professor",  row.get("SALASST")),
                ("Instructor",      row.get("SALINST")),
                ("Lecturer",        row.get("SALLECT")),
                ("No Rank",         row.get("SALNRNK")),
            ]
            sal_df = pd.DataFrame(
                [(k, v) for k, v in sal_items if pd.notna(v) and v > 0],
                columns=["Rank", "Avg 9-month Salary"]
            )
            if not sal_df.empty:
                fig = px.bar(sal_df, x="Rank", y="Avg 9-month Salary",
                             color_discrete_sequence=["#ff7f0e"],
                             title="Average 9-month Salary by Academic Rank")
                fig.update_layout(height=360, showlegend=False)
                fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                st.plotly_chart(fig, use_container_width=True)
            else:
                lvl = row.get("ICLEVEL")
                if lvl == 3:
                    st.info("Salary data is not collected for less-than-2-year institutions.")
                else:
                    st.info(f"This institution did not report instructional salary data for {year}.")

            # SAL{yr}_IS — detailed salary outlays for FT instructional staff by rank
            st.subheader(f"Salary Outlays — Full-time Instructional Staff by Rank (SAL{yr}_IS)")
            try:
                sal_is = con.execute(f"""
                    SELECT ARANK, SAINSTT AS TotalStaff, SA_9MCT AS On9moContract,
                           SAOUTLT AS SalaryOutlays, SAEQ9AT AS AvgSalary9mo
                    FROM SAL{yr}_IS WHERE UNITID={uid}
                    ORDER BY ARANK
                """).df()
                if not sal_is.empty:
                    sal_is["Rank"] = sal_is["ARANK"].map(ARANK_LBL).fillna(sal_is["ARANK"].astype(str))
                    st.dataframe(sal_is[["Rank","TotalStaff","On9moContract","SalaryOutlays","AvgSalary9mo"]].rename(columns={
                        "TotalStaff":"# Staff","On9moContract":"# On 9-mo Contract",
                        "SalaryOutlays":"Total Salary Outlays ($)","AvgSalary9mo":"Avg 9-mo Salary ($)"
                    }).reset_index(drop=True), use_container_width=True)

                    # Chart avg salary by rank (exclude ARANK=7 = all ranks total)
                    chart_sal = sal_is[sal_is["ARANK"].isin([1,2,3,4,5,6])].dropna(subset=["AvgSalary9mo"])
                    if not chart_sal.empty:
                        fig = px.bar(chart_sal, x="Rank", y="AvgSalary9mo",
                                     color_discrete_sequence=["#ff7f0e"],
                                     title=f"Average 9-month Salary by Rank (SAL{yr}_IS)")
                        fig.update_layout(height=320, showlegend=False)
                        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                        st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Instructional salary outlay data not available.")
            except Exception as ex:
                st.info(f"SAL{yr}_IS not available. ({ex})")

            # SAL{yr}_NIS — salary outlays for FT non-instructional staff
            st.subheader(f"Salary Outlays — Full-time Non-instructional Staff (SAL{yr}_NIS)")
            NIS_CATS = [
                ("All Non-instructional", "SANIN01","SANIT01"),
                ("Research",              "SANIN02","SANIT02"),
                ("Public Service",        "SANIN03","SANIT03"),
                ("Library & Academic Affairs","SANIN04","SANIT04"),
                ("Management",            "SANIN05","SANIT05"),
                ("Business & Financial",  "SANIN06","SANIT06"),
                ("Computer, Eng & Sci",   "SANIN07","SANIT07"),
                ("Community Svc, Arts",   "SANIN08","SANIT08"),
                ("Healthcare",            "SANIN09","SANIT09"),
                ("Service",               "SANIN10","SANIT10"),
                ("Sales & Related",       "SANIN11","SANIT11"),
                ("Office & Admin",        "SANIN12","SANIT12"),
                ("Natural Res & Constr",  "SANIN13","SANIT13"),
                ("Production & Transport","SANIN14","SANIT14"),
            ]
            try:
                all_nis_cols = ",".join([f"{n},{t}" for _, n, t in NIS_CATS])
                nis = con.execute(f"SELECT {all_nis_cols} FROM SAL{yr}_NIS WHERE UNITID={uid}").df()
                if not nis.empty:
                    nisr = nis.iloc[0]
                    nis_rows = []
                    for lbl, ncol, tcol in NIS_CATS:
                        n_val = nisr.get(ncol)
                        t_val = nisr.get(tcol)
                        if pd.notna(n_val) and float(n_val) > 0:
                            avg = float(t_val) / float(n_val) if pd.notna(t_val) and float(n_val) > 0 else None
                            nis_rows.append({
                                "Category": lbl,
                                "# Staff": int(n_val),
                                "Total Outlays ($)": int(t_val) if pd.notna(t_val) else None,
                                "Avg Salary ($)": int(avg) if avg else None,
                            })
                    if nis_rows:
                        nis_df = pd.DataFrame(nis_rows)
                        st.dataframe(nis_df, use_container_width=True)
                        non_total = nis_df[nis_df["Category"] != "All Non-instructional"]
                        if not non_total.empty:
                            fig = px.bar(non_total.sort_values("Avg Salary ($)", ascending=False),
                                         x="Category", y="Avg Salary ($)",
                                         color_discrete_sequence=["#2ca02c"],
                                         title="Avg Salary by Occupational Category (Non-instructional FT Staff)")
                            fig.update_layout(height=360, showlegend=False, xaxis_tickangle=-30)
                            fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                            st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("No non-instructional salary data for this institution.")
                else:
                    st.info(f"SAL{yr}_NIS data not available.")
            except Exception as ex:
                st.info(f"SAL{yr}_NIS not available. ({ex})")

            # S{yr}_OC — staff by race/ethnicity and gender (diversity)
            st.subheader(f"Staff Diversity by Race/Ethnicity (S{yr}_OC)")
            try:
                soc = con.execute(f"""
                    SELECT OCCUPCAT, HRTOTLT, HRWHITT, HRBKAAT, HRHISPT,
                           HRASIAT, HRAIANT, HR2MORT, HRUNKNT, HRNRALT,
                           HRTOTLM, HRTOTLW
                    FROM S{yr}_OC
                    WHERE UNITID={uid} AND FTPT=1 AND STAFFCAT=1100
                    LIMIT 1
                """).df()
                if not soc.empty:
                    sr = soc.iloc[0]
                    race_div = {
                        "White":       sr.get("HRWHITT"),
                        "Black/AA":    sr.get("HRBKAAT"),
                        "Hispanic":    sr.get("HRHISPT"),
                        "Asian/PI":    sr.get("HRASIAT"),
                        "AI/AN":       sr.get("HRAIANT"),
                        "Two+ races":  sr.get("HR2MORT"),
                        "Unknown":     sr.get("HRUNKNT"),
                        "Nonresident": sr.get("HRNRALT"),
                    }
                    c1, c2 = st.columns(2)
                    with c1:
                        div_df = pd.DataFrame(
                            [(k, float(v)) for k, v in race_div.items() if pd.notna(v) and float(v) > 0],
                            columns=["Race/Ethnicity", "Count"]
                        )
                        if not div_df.empty:
                            fig = px.pie(div_df, values="Count", names="Race/Ethnicity", hole=0.35,
                                         title="Full-time Staff by Race/Ethnicity")
                            fig.update_layout(height=360)
                            st.plotly_chart(fig, use_container_width=True)
                    with c2:
                        total = sr.get("HRTOTLT")
                        male  = sr.get("HRTOTLM")
                        female= sr.get("HRTOTLW")
                        st.metric("Total Full-time Staff", fmt(total, "int"))
                        st.metric("Male",   fmt(male,   "int"))
                        st.metric("Female", fmt(female, "int"))
                        if pd.notna(total) and float(total) > 0:
                            for lbl, val in race_div.items():
                                if pd.notna(val) and float(val) > 0:
                                    pct = 100 * float(val) / float(total)
                                    st.write(f"  {lbl}: **{pct:.1f}%**")

                    # All occupational categories diversity breakdown
                    soc_all = con.execute(f"""
                        SELECT OCCUPCAT, HRTOTLT, HRWHITT, HRBKAAT, HRHISPT, HRASIAT, HRAIANT
                        FROM S{yr}_OC
                        WHERE UNITID={uid} AND FTPT=1 AND STAFFCAT NOT IN (1100)
                        AND OCCUPCAT IN (100,200,300,400,500,600,700,800,900,1000,1100,1200,1300,1400)
                        ORDER BY HRTOTLT DESC NULLS LAST
                    """).df()
                    if not soc_all.empty:
                        soc_all["Category"] = soc_all["OCCUPCAT"].map(OCC_LABELS).fillna(soc_all["OCCUPCAT"].astype(str))
                        soc_all = soc_all.rename(columns={
                            "HRTOTLT":"Total","HRWHITT":"White","HRBKAAT":"Black/AA",
                            "HRHISPT":"Hispanic","HRASIAT":"Asian/PI","HRAIANT":"AI/AN"
                        })
                        st.dataframe(soc_all[["Category","Total","White","Black/AA","Hispanic","Asian/PI","AI/AN"]].reset_index(drop=True),
                                     use_container_width=True)
                else:
                    st.info("Staff diversity data not available.")
            except Exception as ex:
                st.info(f"S{yr}_OC not available. ({ex})")

        # ── Tab 9: Library ────────────────────────────────────────────────────
        with tabs[9]:
            st.subheader(f"Academic Library Data (AL{yr} & DRVAL{yr})")
            try:
                al = con.execute(f"SELECT * FROM AL{yr} WHERE UNITID={uid}").df()
                if not al.empty:
                    alr = al.iloc[0]
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Total Collections",      fmt(alr.get("LTCLLCT"),  "int"))
                    c2.metric("E-Books",                fmt(alr.get("LEBOOKS"),  "int"))
                    c3.metric("E-Databases",            fmt(alr.get("LEDATAB"),  "int"))
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Physical Books",         fmt(alr.get("LPBOOKS"),  "int"))
                    c2.metric("Total FTE Staff",        fmt(alr.get("LSTOTAL"),  "number"))
                    c3.metric("Librarians FTE",         fmt(alr.get("LSLIBRN"),  "number"))
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Total Expenditures",     fmt(alr.get("LEXPTOT"),  "dollar"))
                    c2.metric("ILL Loans Given",        fmt(alr.get("LILLDYN"),  "int"))
                    c3.metric("ILL Loans Received",     fmt(alr.get("LILSYN"),   "int"))

                    # Collections breakdown
                    coll_items = [
                        ("Physical books",    alr.get("LPBOOKS")),
                        ("E-books",           alr.get("LEBOOKS")),
                        ("Physical media",    alr.get("LPMEDIA")),
                        ("E-media",           alr.get("LEMEDIA")),
                        ("Physical serials",  alr.get("LPSERIA")),
                        ("E-serials",         alr.get("LESERIA")),
                        ("E-databases",       alr.get("LEDATAB")),
                    ]
                    coll_df = pd.DataFrame(
                        [(k, float(v)) for k, v in coll_items if pd.notna(v) and float(v) > 0],
                        columns=["Type", "Count"]
                    ).sort_values("Count", ascending=False)
                    if not coll_df.empty:
                        fig = px.bar(coll_df, x="Type", y="Count",
                                     color_discrete_sequence=["#17becf"],
                                     title="Library Collections by Format")
                        fig.update_layout(height=320, showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)

                    # Expenditure breakdown
                    exp_items = [
                        ("Salaries & Wages",    alr.get("LSALWAG")),
                        ("Materials & Services",alr.get("LEXMSBB")),
                        ("Computer Software",   alr.get("LEXMSCS")),
                        ("Other Materials",     alr.get("LEXMSOT")),
                        ("Operations",          alr.get("LEXOMTL")),
                    ]
                    exp_df = pd.DataFrame(
                        [(k, float(v)) for k, v in exp_items if pd.notna(v) and float(v) > 0],
                        columns=["Category", "Amount ($)"]
                    )
                    if not exp_df.empty:
                        fig = px.pie(exp_df, values="Amount ($)", names="Category", hole=0.35,
                                     title="Library Expenditure Breakdown")
                        fig.update_layout(height=320)
                        st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("No academic library data for this institution.")
            except Exception as ex:
                st.info(f"Library data not available. ({ex})")

            # DRVAL{yr} — per-FTE derived metrics
            st.subheader(f"Library Per-FTE Metrics (DRVAL{yr})")
            try:
                drval = con.execute(f"SELECT * FROM DRVAL{yr} WHERE UNITID={uid}").df()
                if not drval.empty:
                    dvr = drval.iloc[0]
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Physical Books per FTE",      fmt(dvr.get("LPBOOKSP"),  "number"))
                    c2.metric("E-Books per FTE",             fmt(dvr.get("LEBOOKSP"),  "number"))
                    c3.metric("Library Expenditures per FTE",fmt(dvr.get("LEXPTOTF"),  "dollar"))
                    c4.metric("Library Staff FTE",           fmt(dvr.get("LTOTLFTE"),  "number"))
                else:
                    st.info("No derived library metrics available.")
            except Exception as ex:
                st.info(f"Derived library metrics not available. ({ex})")

    finally:
        con.close()


# ── Page 3: Compare Institutions ─────────────────────────────────────────────
def _render_comparison(cmp: pd.DataFrame, year: str = "2024-25"):
    """Side-by-side comparison (table + bar charts + scatter) for a set of institutions.

    The focus institution (Institution of Interest) is highlighted throughout.
    """
    metrics = {
        "Total Enrollment":      ("ENRTOT",          "int"),
        "FTE Enrollment":        ("FTE",              "int"),
        "Acceptance Rate %":     ("DVADM01",          "pct"),
        "Grad Rate 150% %":      ("GRRTTOT",          "pct"),
        "Bach 6-yr Rate %":      ("GBA6RTT",          "pct"),
        "Pell Grad Rate %":      ("PGGRRTT",          "pct"),
        "Transfer-out Rate %":   ("TRRTTOT",          "pct"),
        "FT Award Rate 8yr %":   ("OM1TOTLAWDP8",     "pct"),
        "PT Award Rate 8yr %":   ("OM2TOTLAWDP8",     "pct"),
        "Pell Award Rate 8yr %": ("OM1PELLAWDP8",     "pct"),
        "In-State COA":          ("CINSON",           "dollar"),
        "Out-of-State COA":      ("COTSON",           "dollar"),
        f"Tuition {year}":        ("TUFEYR3",          "dollar"),
        "% Receiving Any Aid":   ("ANYAIDP",          "pct"),
        "% Receiving Pell":      ("PGRNT_P",          "pct"),
        "Avg Pell Grant":        ("PGRNT_A",          "dollar"),
        "Avg Any Grant":         ("AGRNT_A",          "dollar"),
        "% Student Loans":       ("LOAN_P",           "pct"),
        "Full-time Retention":   ("RET_PCF",          "pct"),
        "Student-Faculty Ratio": ("STUFACR",          "number"),
        "Avg Faculty Salary":    ("SALTOTL",          "dollar"),
        "Total FTE Staff":       ("SFTETOTL",         "int"),
        "Instructional FTE":     ("SFTEINST",         "int"),
        "Library Expend/FTE":    ("LEXPTOTF",         "dollar"),
        "Bachelor's Awarded":    ("BASDEG",           "int"),
        "Master's Awarded":      ("MASDEG",           "int"),
        "Doctoral Awarded":      ("DOCDEGRS",         "int"),
        "12-mo Unduplicated":    ("EF12UNDUP",        "int"),
        "12-mo UG (unduplicated)":("EF12UNDUPUG",    "int"),
    }

    st.subheader("Side-by-side Comparison")
    tbl_rows = []
    for lbl, (col, style) in metrics.items():
        row_d = {"Metric": lbl}
        for _, r in cmp.iterrows():
            row_d[r["INSTNM"]] = fmt(r.get(col), style)
        tbl_rows.append(row_d)

    cmp_tbl = pd.DataFrame(tbl_rows).set_index("Metric")
    _alb_col = next((c for c in cmp_tbl.columns if c == _focus_name()), None)

    def _highlight_alb_col(df):
        styles = pd.DataFrame("", index=df.index, columns=df.columns)
        if _alb_col and _alb_col in styles.columns:
            styles[_alb_col] = "background-color:#FDE68A;color:#78350F;font-weight:bold"
        return styles

    st.dataframe(
        cmp_tbl.style.apply(_highlight_alb_col, axis=None),
        use_container_width=True,
        height=700,
    )

    chart_defs = [
        ("Total Enrollment",        "ENRTOT",         None),
        ("Acceptance Rate (%)",     "DVADM01",        "%"),
        ("Grad Rate 150% (%)",      "GRRTTOT",        "%"),
        ("Retention Rate FT (%)",   "RET_PCF",        "%"),
        ("8-yr Award Rate (%)",     "OM1TOTLAWDP8",   "%"),
        ("In-State COA ($)",        "CINSON",         "$"),
        (f"Tuition {year} ($)",     "TUFEYR3",        "$"),
        ("% Receiving Pell",        "PGRNT_P",        "%"),
        ("Avg Faculty Salary ($)",  "SALTOTL",        "$"),
        ("Student-Faculty Ratio",   "STUFACR",        None),
    ]

    # For large sets (>10) switch from vertical bars to horizontal for readability
    many = len(cmp) > 10
    chart_h = max(380, len(cmp) * 28) if many else 360

    st.subheader("Charts")
    for i in range(0, len(chart_defs), 2):
        cols = st.columns(2)
        for j, (lbl, col, fmt_sym) in enumerate(chart_defs[i : i + 2]):
            with cols[j]:
                cdf = cmp[["INSTNM", col]].dropna().sort_values(col, ascending=False)
                if not cdf.empty:
                    # Color: amber for the focus institution, steel-blue for everyone else
                    _fname = _focus_name()
                    _cmap = {
                        n: ("#F59E0B" if str(n) == _fname else "#5B9BD5")
                        for n in cdf["INSTNM"]
                    }
                    if many:
                        fig = px.bar(cdf, x=col, y="INSTNM", orientation="h",
                                     color="INSTNM", color_discrete_map=_cmap,
                                     title=lbl, labels={"INSTNM": "", col: lbl})
                        fig.update_layout(showlegend=False, height=chart_h,
                                          yaxis_title="", yaxis=dict(autorange="reversed"))
                    else:
                        fig = px.bar(cdf, x="INSTNM", y=col,
                                     color="INSTNM", color_discrete_map=_cmap,
                                     title=lbl, labels={"INSTNM": "", col: lbl})
                        fig.update_layout(showlegend=False, height=360, xaxis_tickangle=-20)
                    # Bold amber outline on the focus institution's bar
                    for trace in fig.data:
                        if str(getattr(trace, "name", "")) == _fname:
                            trace.marker.line = dict(color="#92400E", width=2.5)
                    if fmt_sym == "$":
                        if many:
                            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
                        else:
                            fig.update_yaxes(tickprefix="$", tickformat=",.0f")
                    elif fmt_sym == "%":
                        if many:
                            fig.update_xaxes(ticksuffix="%")
                        else:
                            fig.update_yaxes(ticksuffix="%")
                    st.plotly_chart(fig, use_container_width=True)

    # ── Scatter Explorer (compare page) ──────────────────────────────────────
    st.divider()
    st.subheader("Scatter Explorer — Selected Institutions")

    cmp_sc_tabs = st.tabs([name for name, *_ in SCATTER_SUGGESTIONS])
    for sc_tab, (name, x_var, y_var, z_var), rationale in zip(cmp_sc_tabs, SCATTER_SUGGESTIONS, SCATTER_RATIONALE):
        with sc_tab:
            st.info(rationale)
            cx_col = SCATTER_VARS[x_var]
            cy_col = SCATTER_VARS[y_var]
            cz_col = SCATTER_VARS[z_var]

            csc_df = cmp.dropna(subset=[cx_col, cy_col, cz_col]).copy()
            csc_df["_zplot"] = csc_df[cz_col].clip(lower=0.01)

            if csc_df.empty:
                st.warning("No selected institutions have data for all three variables in this view.")
                continue

            fig = px.scatter(
                csc_df, x=cx_col, y=cy_col,
                size="_zplot", size_max=50,
                color="INSTNM",
                hover_name="INSTNM",
                hover_data={"STABBR": True, "_zplot": False},
                labels={cx_col: x_var, cy_col: y_var, "INSTNM": "Institution"},
                opacity=0.85,
                title=f"{x_var}  ×  {y_var}  (bubble = {z_var})",
            )

            _fname = _focus_name()
            albion_c = csc_df[csc_df["UNITID"] == _focus_uid()]
            if not albion_c.empty:
                ar = albion_c.iloc[0]
                fig.add_trace(go.Scatter(
                    x=[ar[cx_col]], y=[ar[cy_col]],
                    mode="markers+text",
                    marker=dict(symbol="star", size=28, color="#F59E0B",
                                line=dict(color="#1E3A5F", width=2.5)),
                    text=[_fname],
                    textposition="top right",
                    textfont=dict(size=12, color="#1E3A5F", family="Arial Black"),
                    name=f"{_fname} ★",
                    hovertemplate=(
                        f"<b>{_fname}</b><br>{x_var}: %{{x:.1f}}<br>{y_var}: %{{y:.1f}}<extra></extra>"
                    ),
                    showlegend=True,
                ))

            fig.update_layout(height=520, legend=dict(title="Institution"))
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"n = **{len(csc_df)}** of {len(cmp)} institutions have data for all three variables.")


# ── Page 5: Year-over-Year Trends ────────────────────────────────────────────
def page_trends():
    trend_df = load_trends()
    YEARS = sorted(trend_df["YEAR"].unique().tolist())

    st.title(f"Year-over-Year Trends — {YEARS[0]} → {YEARS[-1]}")
    st.caption(f"Covers {len(YEARS)} IPEDS reporting years: {', '.join(YEARS)}.")
    METRICS = [
        ("Grad Rate 150% (%)",    "GRRTTOT",      "{:.1f}%"),
        ("FT Retention Rate (%)", "RET_PCF",       "{:.1f}%"),
        ("Acceptance Rate (%)",   "DVADM01",       "{:.1f}%"),
        ("% Receiving Pell",      "PGRNT_P",       "{:.1f}%"),
        ("In-State COA ($)",      "CINSON",        "${:,.0f}"),
        ("Avg Faculty Salary ($)","SALTOTL",        "${:,.0f}"),
        ("Total Enrollment",      "ENRTOT",        "{:,.0f}"),
        ("8-yr Award Rate (%)",   "OM1TOTLAWDP8",  "{:.1f}%"),
        ("Student:Faculty Ratio", "STUFACR",       "{:.1f}"),
    ]

    # ── TABS ──────────────────────────────────────────────────────────────────
    _foc_uid  = _focus_uid()
    _foc_name = _focus_name() or "Institution of Interest"
    t1, t2 = st.tabs(["📊 National Trends", f"🎓 {_foc_name} Trends"])

    # ════════════════════════════════════════════════════════════════════════
    # TAB 1 — National Trends
    # ════════════════════════════════════════════════════════════════════════
    with t1:
        _yr_newest = YEARS[-1]
        _yr_oldest = YEARS[0]
        st.subheader(f"National Medians — {_yr_oldest} → {_yr_newest}")
        st.caption("All active institutions reporting each metric.")

        med_rows = []
        for lbl, col, _ in METRICS:
            for yr in YEARS:
                sub = trend_df[(trend_df["YEAR"] == yr) & trend_df[col].notna()]
                med_rows.append({"Metric": lbl, "Year": yr, "Median": sub[col].median()})
        med_piv = pd.DataFrame(med_rows).pivot(index="Metric", columns="Year", values="Median")
        _yr_prev2 = YEARS[-2] if len(YEARS) >= 2 else None
        if _yr_prev2 and _yr_newest in med_piv.columns and _yr_prev2 in med_piv.columns:
            med_piv["Change"] = med_piv[_yr_newest] - med_piv[_yr_prev2]
            med_piv["Chg %"]  = (med_piv["Change"] / med_piv[_yr_prev2].abs() * 100).round(1)

        def _cc(v):
            if pd.isna(v): return ""
            return "color:#059669;font-weight:bold" if v > 0 else "color:#DC2626;font-weight:bold"

        _yr_fmt = {yr: (lambda v: f"{v:,.1f}" if pd.notna(v) else "—") for yr in YEARS}
        _yr_fmt.update({"Change": lambda v: f"{v:+,.1f}" if pd.notna(v) else "—",
                         "Chg %":  lambda v: f"{v:+.1f}%" if pd.notna(v) else "—"})
        st.dataframe(
            med_piv.style
                .format(_yr_fmt)
                .map(_cc, subset=["Change"] if "Change" in med_piv.columns else []),
            use_container_width=True,
        )

        # Bar charts
        st.divider()
        bc1, bc2 = st.columns(2)
        for col_w, col_id, title in [
            (bc1, "GRRTTOT", "Median Grad Rate 150% by Control"),
            (bc2, "RET_PCF",  "Median FT Retention Rate by Control"),
        ]:
            df_b = (trend_df.dropna(subset=[col_id])
                    .groupby(["YEAR","CONTROL_LBL"])[col_id].median().reset_index())
            df_b.columns = ["Year","Control","Value"]
            fig = px.bar(df_b, x="Control", y="Value", color="Year", barmode="group",
                         title=title, color_discrete_sequence=["#60A5FA","#1D4ED8"])
            fig.update_layout(height=340, legend_title="", xaxis_title="", yaxis_title="")
            col_w.plotly_chart(fig, use_container_width=True)

        bc3, bc4 = st.columns(2)
        for col_w, col_id, title, agg in [
            (bc3, "ENRTOT", "Total Enrollment by Control",      "sum"),
            (bc4, "CINSON", "Median In-State COA by Control",   "median"),
        ]:
            df_b = (trend_df.dropna(subset=[col_id])
                    .groupby(["YEAR","CONTROL_LBL"])[col_id]
                    .agg(agg).reset_index())
            df_b.columns = ["Year","Control","Value"]
            fig = px.bar(df_b, x="Control", y="Value", color="Year", barmode="group",
                         title=title, color_discrete_sequence=["#60A5FA","#1D4ED8"])
            fig.update_layout(height=340, legend_title="", xaxis_title="", yaxis_title="")
            col_w.plotly_chart(fig, use_container_width=True)

        # Institution search
        st.divider()
        st.subheader("Institution Lookup")
        st.caption(f"Search any institution to see its {YEARS[0]} → {YEARS[-1]} metrics.")
        all_names = sorted(trend_df["INSTNM"].dropna().unique())
        _lk_typed = st.text_input(
            "Type to narrow the list", key="yt_inst_filter",
            placeholder="Type a name to narrow…",
        )
        _lk_filt = [n for n in all_names if _lk_typed.lower() in n.lower()] if _lk_typed else all_names
        if not _lk_filt:
            st.warning("No institution matches that search.")
            _lk_filt = all_names
        sch = st.selectbox("Search institution", ["— select —"] + _lk_filt, key="yt_inst_search")
        if sch and sch != "— select —":
            _render_inst_pivot(trend_df[trend_df["INSTNM"] == sch].sort_values("YEAR"))

    # ════════════════════════════════════════════════════════════════════════
    # TAB 2 — Institution of Interest Trends (focus vs. national & control medians)
    # ════════════════════════════════════════════════════════════════════════
    with t2:
        if not _foc_uid:
            st.info("👈 Pick an **Institution of Interest** in the sidebar to see its "
                    "year-over-year trends versus the national and same-control medians.")
        else:
            foc_df = trend_df[trend_df["UNITID"] == _foc_uid].sort_values("YEAR")
            if foc_df.empty:
                st.warning(f"{_foc_name} was not found in the multi-year trend data (METRICS_LONG).")
            else:
                _ctrl_lbl  = str(foc_df.iloc[-1].get("CONTROL_LBL") or "")
                _t2_newest = YEARS[-1]
                _t2_prev   = YEARS[-2] if len(YEARS) >= 2 else None
                st.subheader(f"{_foc_name} — {YEARS[0]} → {_t2_newest}")
                st.caption("Each metric shown per year, with the latest-year change in the Δ column. "
                           f"Charts compare {_foc_name} to the national median"
                           + (f" and the {_ctrl_lbl} median." if _ctrl_lbl else "."))

                # Change table — the focus institution's own values per year + Δ
                tbl_rows = []
                for lbl, col, fmt_str in METRICS:
                    row = {"Metric": lbl}
                    for yr in YEARS:
                        fv = foc_df[foc_df["YEAR"] == yr][col].values
                        row[str(yr)] = fmt_str.format(float(fv[0])) if len(fv) and pd.notna(fv[0]) else "—"
                    try:
                        if _t2_prev:
                            _a = float(foc_df[foc_df["YEAR"] == _t2_newest][col].values[0])
                            _b = float(foc_df[foc_df["YEAR"] == _t2_prev][col].values[0])
                            row["Δ (latest)"] = f"{_a - _b:+.1f}"
                        else:
                            row["Δ (latest)"] = "—"
                    except Exception:
                        row["Δ (latest)"] = "—"
                    tbl_rows.append(row)
                st.dataframe(pd.DataFrame(tbl_rows).set_index("Metric"), use_container_width=True)

                # Line charts — focus vs national median vs same-control median
                st.divider()
                st.subheader(f"Trend Charts — {_foc_name} vs. Medians")
                chart_m = [(l, c, f) for l, c, f in METRICS
                           if c in ["GRRTTOT", "RET_PCF", "ENRTOT", "CINSON", "SALTOTL", "PGRNT_P"]]
                ccols = st.columns(2)
                for i, (lbl, col, fmt_str) in enumerate(chart_m):
                    foc_pts  = [float(v[0]) if len(v := foc_df[foc_df["YEAR"] == yr][col].values) and pd.notna(v[0]) else None
                                for yr in YEARS]
                    nat_pts  = [float(s.median()) if not (s := trend_df[trend_df["YEAR"] == yr][col].dropna()).empty else None
                                for yr in YEARS]
                    ctrl_pts = [float(s.median()) if _ctrl_lbl and not (
                                    s := trend_df[(trend_df["YEAR"] == yr) & (trend_df["CONTROL_LBL"] == _ctrl_lbl)][col].dropna()
                                ).empty else None for yr in YEARS]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=YEARS, y=nat_pts, mode="lines+markers", name="National median",
                        line=dict(color="#6B7280", width=2, dash="dash"), marker=dict(size=8),
                    ))
                    if _ctrl_lbl:
                        fig.add_trace(go.Scatter(
                            x=YEARS, y=ctrl_pts, mode="lines+markers", name=f"{_ctrl_lbl} median",
                            line=dict(color="#2563EB", width=2, dash="dot"), marker=dict(size=7),
                        ))
                    fig.add_trace(go.Scatter(
                        x=YEARS, y=foc_pts, mode="lines+markers+text", name=_foc_name,
                        line=dict(color="#F59E0B", width=3),
                        marker=dict(size=12, symbol="star", color="#F59E0B", line=dict(color="#92400E", width=2)),
                        text=[fmt_str.format(v) if v is not None else "" for v in foc_pts],
                        textposition="top center",
                    ))
                    fig.update_layout(title=lbl, height=300, legend=dict(orientation="h", y=-0.25),
                                      margin=dict(l=0, r=0, t=40, b=0), yaxis_title=lbl)
                    ccols[i % 2].plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Strategic Analysis — generic, works for ANY chosen College of Interest
# ══════════════════════════════════════════════════════════════════════════════
_ACCENT_LINE = "#D97706"
_ACCENT_STAR = "#F59E0B"
_ACCENT_TEXT = "#92400E"
_ACCENT_BG   = "rgba(253,230,138,0.92)"


def recommend_peers(df: pd.DataFrame, focus_uid: int, focus: pd.Series, k: int = 15) -> list:
    """Return DISPLAY_NAMEs of the k institutions most similar to the focus.

    Similarity = same control & level, then nearest on standardized enrollment,
    cost of attendance, 6-yr grad rate and faculty salary (whichever are present),
    with a small bonus for matching sector. A sensible starting comparison set
    that the user is free to edit.
    """
    cand = df[df["UNITID"] != focus_uid].copy()
    if pd.notna(focus.get("CONTROL")):
        cand = cand[cand["CONTROL"] == focus.get("CONTROL")]
    if "ICLEVEL" in cand.columns and pd.notna(focus.get("ICLEVEL")):
        cand = cand[cand["ICLEVEL"] == focus.get("ICLEVEL")]
    if cand.empty:
        return []

    dist = pd.Series(0.0, index=cand.index)
    used = 0
    for col in ["ENRTOT", "CINSON", "GBA6RTT", "SALTOTL"]:
        if col not in cand.columns:
            continue
        fv = focus.get(col)
        if fv is None or pd.isna(fv):
            continue
        s  = pd.to_numeric(cand[col], errors="coerce")
        sd = s.std(skipna=True)
        if not sd or pd.isna(sd):
            continue
        d = ((s - float(fv)) / sd).abs()
        dist = dist.add(d.fillna(d.max() if pd.notna(d.max()) else 0.0), fill_value=0.0)
        used += 1
    if used == 0:
        s = pd.to_numeric(cand.get("ENRTOT"), errors="coerce")
        base = float(focus.get("ENRTOT") or (s.median() if pd.notna(s.median()) else 0))
        dist = (s - base).abs().fillna(s.max())

    # Reward same-sector matches (smaller distance is better)
    if "SECTOR" in cand.columns and pd.notna(focus.get("SECTOR")):
        dist = dist - (cand["SECTOR"] == focus.get("SECTOR")).astype(float) * 0.5

    cand = cand.assign(_dist=dist).sort_values("_dist")
    return cand["DISPLAY_NAME"].dropna().head(k).tolist()


def page_strategic(df: pd.DataFrame, focus_uid: int, focus_name: str, year: str = "2024-25"):
    h_col, y_col = st.columns([7, 3])
    with h_col:
        st.title(f"{focus_name} — Strategic Performance Analysis")
    with y_col:
        st.markdown("<div style='padding-top:1.1rem'></div>", unsafe_allow_html=True)
        st.radio("Data Year", ["2024-25", "2023-24", "2022-23"], horizontal=True,
                 key="year_Institution of Interest Analysis", label_visibility="collapsed")

    foc_all = df[df["UNITID"] == focus_uid]
    if foc_all.empty:
        st.error(f"{focus_name} not found in the {year} dataset. "
                 f"It may not have reported in this year — try another data year.")
        return
    focus = foc_all.iloc[0]

    mark = focus_name if len(focus_name) <= 22 else focus_name[:21] + "…"

    # ── Peer set: free selection with smart recommendations ──────────────────
    st.markdown(f"**Choose which colleges to compare {focus_name} against:**")
    all_names = sorted(df[df["UNITID"] != focus_uid]["DISPLAY_NAME"].dropna().unique().tolist())
    rec_names = [n for n in recommend_peers(df, focus_uid, focus, k=15) if n in all_names]

    # Changing the focus institution resets the peer set to its recommendations.
    if st.session_state.get("strat_peer_focus") != focus_uid:
        st.session_state["strat_peer_focus"]  = focus_uid
        st.session_state["strat_peer_multi"]  = rec_names
        st.session_state["strat_peer_loaded"] = list(rec_names)
        st.session_state["strat_peer_label"]  = "Recommended similar colleges"

    if st.button("🎯 Load recommended similar colleges", use_container_width=True,
                 help="Same control & level as the focus college; closest on enrollment, "
                      "cost, graduation rate and faculty salary."):
        st.session_state["strat_peer_multi"]  = rec_names
        st.session_state["strat_peer_loaded"] = list(rec_names)
        st.session_state["strat_peer_label"]  = "Recommended similar colleges"
        st.rerun()

    peer_names = st.multiselect(
        "Peer colleges — type to search, then add or remove any institution freely",
        options=all_names,
        key="strat_peer_multi",
    )

    with st.expander("💡 Why these recommendations?", expanded=False):
        st.markdown(
            f"The recommended set shares the same **control** (public / private) and **level** as "
            f"**{focus_name}**, then picks the closest matches on **enrollment size, cost of attendance, "
            f"6-year graduation rate, and average faculty salary** (with a nudge toward the same sector). "
            f"It is only a starting point — add or remove any college above to build your own comparison set."
        )
        if rec_names:
            st.caption("Recommended: " + ", ".join(n.split(" (")[0] for n in rec_names))

    if len(peer_names) < 2:
        st.warning("Select at least 2 peer colleges to build the analysis — "
                   "click **🎯 Load recommended similar colleges** above for a quick start.")
        return

    peers = df[df["DISPLAY_NAME"].isin(peer_names) & (df["UNITID"] != focus_uid)].copy()

    loaded = st.session_state.get("strat_peer_loaded")
    if loaded is not None and set(peer_names) == set(loaded):
        peer_label = st.session_state.get("strat_peer_label", "Selected peer set")
    else:
        peer_label = "Custom peer set"

    n_peers = len(peers)
    st.caption(
        f"Comparing **{focus_name}** against **{n_peers:,} colleges** — *{peer_label}*."
        + (" Percentile rankings may be approximate with fewer than 10 peers." if n_peers < 10 else "")
    )
    st.divider()

    focus_display = str(focus.get("DISPLAY_NAME") or focus_name)
    tab_a, tab_c = st.tabs(["📊 Benchmark Analysis", "📋 Side-by-Side Comparison"])
    with tab_c:
        st.caption("Exact reported numbers for the Institution of Interest and the peers selected "
                   "above, side by side — with bar charts and a scatter explorer.")
        cmp = df[df["DISPLAY_NAME"].isin([focus_display] + peer_names)].copy()
        if len(cmp) < 2:
            st.info("Add at least one peer above to build the side-by-side comparison.")
        else:
            _render_comparison(cmp, year)
    with tab_a:
        _render_benchmark(focus, focus_name, peers, peer_label, n_peers, mark, year)


def _render_benchmark(focus, focus_name, peers, peer_label, n_peers, mark, year):
    def _val(col):
        v = focus.get(col)
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else v

    def _pct(series: pd.Series, val, higher_is_better: bool = True):
        """Percentile rank where 100 = best (regardless of direction)."""
        if val is None:
            return None
        clean = series.dropna().values
        if len(clean) == 0:
            return None
        below = float((clean < val).sum()) / len(clean) * 100
        return round(below if higher_is_better else 100 - below)

    def _badge(pct):
        if pct is None:
            return "⚪ No data"
        if pct >= 75:
            return "🟢 Strength"
        if pct >= 40:
            return "🟡 Average"
        return "🔴 Needs attention"

    def _fmt_pct(v):
        return f"{v:.1f}%" if v is not None else "—"

    def _fmt_dollar(v):
        return f"${v:,.0f}" if v is not None else "—"

    def _fmt_num(v, decimals=1):
        if v is None:
            return "—"
        return f"{v:.{decimals}f}"

    def _star(fig, x, y, xfmt, yfmt):
        """Add the focus-institution star marker to a scatter figure.

        Returns a caption string when the institution can't be plotted (so the
        caller can explain its absence), else None.
        """
        if x is None or y is None:
            return (f"ℹ️ {focus_name} isn't marked on this chart — it has no reported value "
                    f"for one or both axes in {year}.")
        fig.add_trace(go.Scatter(
            x=[x], y=[y], mode="markers+text",
            marker=dict(symbol="star", size=28, color=_ACCENT_STAR, line=dict(color="#1E3A5F", width=2.5)),
            text=[focus_name], textposition="top right",
            textfont=dict(size=12, color="#1E3A5F", family="Arial Black"),
            name=focus_name, showlegend=True,
            hovertemplate=f"<b>{focus_name}</b><br>{xfmt}<br>{yfmt}<extra></extra>",
        ))
        return None

    def _vline(fig, x, series):
        """Add focus ◆ line + peer-median line to a distribution figure."""
        fig.add_vline(x=x, line_color=_ACCENT_LINE, line_width=3, line_dash="dot",
                      annotation_text=f"◆ {mark}", annotation_position="top right",
                      annotation_font_color=_ACCENT_TEXT, annotation_font_size=12,
                      annotation_bgcolor=_ACCENT_BG)
        fig.add_vline(x=float(series.median()), line_color="#6B7280", line_width=1.5, line_dash="dash",
                      annotation_text="Peer median", annotation_position="top left",
                      annotation_font_color="#374151", annotation_font_size=10)

    # ── Snapshot metrics ──────────────────────────────────────────────────────
    st.subheader("Institution Snapshot")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Enrollment",     f"{int(_val('ENRTOT')):,}"        if _val("ENRTOT") else "—")
    c2.metric("Acceptance Rate",      _fmt_pct(_val("DVADM01")))
    c3.metric("6-yr Grad Rate",       _fmt_pct(_val("GBA6RTT")))
    c4.metric("FT Retention Rate",    _fmt_pct(_val("RET_PCF")))
    c5.metric("In-State COA",         _fmt_dollar(_val("CINSON")))
    c6.metric("Student:Faculty Ratio",_fmt_num(_val("STUFACR"), 0) + ":1" if _val("STUFACR") else "—")
    st.divider()

    # ── Performance scorecard ─────────────────────────────────────────────────
    VAL_COL = "Value"
    st.subheader("Performance Scorecard")
    with st.expander("How to read this table", expanded=False):
        st.markdown(
            f"""
**What this table shows**

Each row is one institutional metric — grouped by theme (Admissions, Student Success, Equity & Access, Costs & Aid,
Faculty & Staff, Library). For every metric you see three things:

| Column | Meaning |
|---|---|
| **{VAL_COL}** | {focus_name}'s actual reported figure for the {year} reporting cycle |
| **vs … (pct)** | {focus_name}'s *percentile rank* within the selected peer group — the share of peer institutions it **outperforms** on this metric |
| **Rating** | A traffic-light badge based on that percentile |

**Reading the percentile**

A percentile of **82** means {focus_name} performs better than 82% of the institutions in the selected peer group on
that metric. Percentiles are always "higher = better": for metrics where lower is better (e.g., acceptance rate,
student:faculty ratio, cost of attendance, loan reliance) the scale is automatically flipped so that a more-selective,
cheaper, or smaller-class-size result still earns a higher number.

**Rating badges**

| Badge | Percentile range | Interpretation |
|---|---|---|
| 🟢 Strength | ≥ 75th percentile | Top quarter of the peer group — a demonstrated competitive advantage |
| 🟡 Average | 40th – 74th percentile | Performs at or near the peer median — monitor for drift |
| 🔴 Needs attention | < 40th percentile | Bottom quarter — a priority area for strategic investment |
| ⚪ No data | — | IPEDS did not report a value, or too few peers have data |

**Understanding "pp" (percentage points)**

Some values are shown as **+3.5 pp** or **−10.0 pp**. "pp" stands for *percentage points* — the arithmetic difference
between two percentages, not a ratio. The **Pell vs Overall Gap** metric uses this unit: it measures whether Pell
recipients (low-income, federally-aided students) graduate at the same rate as the broader student body. A gap near
**0 pp** signals strong equity; a **negative** gap means Pell students graduate at a lower rate. The percentile is
scored so a smaller gap earns a higher rank.

**Choosing a peer group**

Use the selector above to switch peer groups at any time, or change the Institution of Interest in the sidebar.
            """
        )
    st.caption(
        f"Percentile = share of **{peer_label}** institutions that {focus_name} **outperforms** on each metric "
        f"(100 = best in group). n = {n_peers:,} peer institutions."
    )

    SCORECARD = [
        ("Admissions",       "Acceptance Rate",          "DVADM01", False, _fmt_pct),
        ("Admissions",       "Yield Rate",               "DVADM04", True,  _fmt_pct),
        ("Student Success",  "FT Retention Rate",        "RET_PCF", True,  _fmt_pct),
        ("Student Success",  "PT Retention Rate",        "RET_PCP", True,  _fmt_pct),
        ("Student Success",  "Grad Rate 150%",           "GRRTTOT", True,  _fmt_pct),
        ("Student Success",  "Bach 4-yr Grad Rate",      "GBA4RTT", True,  _fmt_pct),
        ("Student Success",  "Bach 6-yr Grad Rate",      "GBA6RTT", True,  _fmt_pct),
        ("Student Success",  "Transfer-out Rate",        "TRRTTOT", False, _fmt_pct),
        ("Equity & Access",  "Pell Recipient Share",     "PGRNT_P", True,  _fmt_pct),
        ("Equity & Access",  "Pell Grad Rate",           "PGGRRTT", True,  _fmt_pct),
        ("Equity & Access",  "Pell vs Overall Gap",      None,      None,  None),
        ("Equity & Access",  "% Women Enrolled",         "PCTENRW", True,  _fmt_pct),
        ("Equity & Access",  "% URM Students",           None,      None,  None),
        ("Costs & Aid",      "In-State COA",             "CINSON",  False, _fmt_dollar),
        ("Costs & Aid",      "Avg Pell Grant",           "PGRNT_A", True,  _fmt_dollar),
        ("Costs & Aid",      "Avg Any Grant",            "AGRNT_A", True,  _fmt_dollar),
        ("Costs & Aid",      "% Receiving Loans",        "LOAN_P",  False, _fmt_pct),
        ("Faculty & Staff",  "Avg Faculty Salary",       "SALTOTL", True,  _fmt_dollar),
        ("Faculty & Staff",  "Student:Faculty Ratio",    "STUFACR", False, _fmt_num),
        ("Faculty & Staff",  "Total FTE Staff",          "SFTETOTL",True,  _fmt_num),
        ("Library",          "Library Expend/FTE",       "LEXPTOTF",True,  _fmt_dollar),
        ("Library",          "E-Books per FTE",          "LEBOOKSP",True,  _fmt_num),
        ("Library",          "Physical Books per FTE",   "LPBOOKSP",True,  _fmt_num),
    ]

    pct_col    = f"vs {peer_label[:30]} (pct)"
    rating_col = "Rating"

    rows = []
    for cat, name, col, hib, fmt_fn in SCORECARD:
        if col is None:
            if name == "Pell vs Overall Gap":
                v_pell    = _val("PGGRRTT")
                v_overall = _val("GRRTTOT")
                if v_pell is not None and v_overall is not None:
                    gap_val    = v_pell - v_overall
                    gap_series = (peers["PGGRRTT"] - peers["GRRTTOT"]).dropna()
                    pct_peer   = _pct(gap_series, gap_val, higher_is_better=True)
                    rows.append({"Category": cat, "Metric": name,
                                 VAL_COL: f"{gap_val:+.1f} pp",
                                 pct_col: pct_peer, "n with data": len(gap_series),
                                 rating_col: _badge(pct_peer)})
            elif name == "% URM Students":
                urm_cols = ["PCTENRBK","PCTENRHS","PCTENRAN","PCTENR2M"]
                foc_urm  = sum((_val(c) or 0) for c in urm_cols)
                peer_urm = peers[urm_cols].sum(axis=1, skipna=True)
                pct_peer = _pct(peer_urm.dropna(), foc_urm, True)
                rows.append({"Category": cat, "Metric": name,
                             VAL_COL: f"{foc_urm:.1f}%",
                             pct_col: pct_peer, "n with data": len(peer_urm.dropna()),
                             rating_col: _badge(pct_peer)})
            continue
        v        = _val(col)
        peer_ser = peers[col].dropna()
        pct_peer = _pct(peer_ser, v, hib)
        rows.append({"Category": cat, "Metric": name,
                     VAL_COL: fmt_fn(v),
                     pct_col: pct_peer, "n with data": len(peer_ser),
                     rating_col: _badge(pct_peer)})

    sc_df = pd.DataFrame(rows)

    def _color_row(r):
        rating = r.get(rating_col, "")
        if "Strength" in str(rating):
            return ["background-color:#D1FAE5;color:#064E3B;font-weight:bold"] * len(r)
        if "Needs" in str(rating):
            return ["background-color:#FEE2E2;color:#7F1D1D;font-weight:bold"] * len(r)
        return [""] * len(r)

    st.dataframe(sc_df.style.apply(_color_row, axis=1), use_container_width=True, height=660)
    st.caption("🟢 Top 25% of peer group · 🟡 Middle 50% · 🔴 Bottom 25% · ⚪ No data available")
    st.divider()

    # ── Strengths / priorities ────────────────────────────────────────────────
    strengths  = sc_df[sc_df[rating_col].str.contains("Strength", na=False)]["Metric"].tolist()
    needs_attn = sc_df[sc_df[rating_col].str.contains("Needs",    na=False)]["Metric"].tolist()

    st.subheader(f"Strengths — Where {focus_name} Leads This Peer Group")
    if strengths:
        st.success(f"Top quartile vs. **{peer_label}** on: **{', '.join(strengths)}**")
    else:
        st.info(f"No metrics in the top quartile vs. {peer_label} in this dataset.")

    st.subheader("Strategic Priorities — Areas for Improvement")
    if needs_attn:
        st.error(f"Bottom quartile vs. **{peer_label}** on: **{', '.join(needs_attn)}**")
    else:
        st.success(f"No metrics in the bottom quartile vs. {peer_label}. Strong overall performance!")
    st.divider()

    # ── Deep-dive analysis sections ───────────────────────────────────────────
    st.subheader("Deep-Dive Analysis by Domain")
    d1, d2, d3, d4 = st.tabs(["Student Success", "Equity & Access", "Costs & Value", "Faculty & Resources"])

    # ── Tab: Student Success ──────────────────────────────────────────────────
    with d1:
        f_ret  = _val("RET_PCF")
        f_gr6  = _val("GBA6RTT")
        f_gr15 = _val("GRRTTOT")
        f_om8  = _val("OM1TOTLAWDP8")

        peer_ret_med  = peers["RET_PCF"].median(skipna=True)
        peer_gr6_med  = peers["GBA6RTT"].median(skipna=True)
        peer_gr15_med = peers["GRRTTOT"].median(skipna=True)
        peer_om8_med  = peers["OM1TOTLAWDP8"].median(skipna=True)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("FT Retention", _fmt_pct(f_ret),
                  delta=f"{f_ret - peer_ret_med:+.1f}pp vs peer median" if f_ret and not pd.isna(peer_ret_med) else None)
        c2.metric("6-yr Grad Rate", _fmt_pct(f_gr6),
                  delta=f"{f_gr6 - peer_gr6_med:+.1f}pp vs peer median" if f_gr6 and not pd.isna(peer_gr6_med) else None)
        c3.metric("Grad Rate 150%", _fmt_pct(f_gr15),
                  delta=f"{f_gr15 - peer_gr15_med:+.1f}pp vs peer median" if f_gr15 and not pd.isna(peer_gr15_med) else None)
        c4.metric("8-yr Award Rate", _fmt_pct(f_om8),
                  delta=f"{f_om8 - peer_om8_med:+.1f}pp vs peer median" if f_om8 and not pd.isna(peer_om8_med) else None)

        gr_hist = peers["GBA6RTT"].dropna()
        if not gr_hist.empty and f_gr6:
            fig = px.histogram(gr_hist, x="GBA6RTT", nbins=max(10, min(40, len(gr_hist)//2)),
                               title=f"6-Year Bachelor's Grad Rate — {peer_label}",
                               labels={"GBA6RTT": "6-yr Grad Rate (%)"})
            fig.update_layout(height=340, showlegend=False)
            _vline(fig, f_gr6, gr_hist)
            st.plotly_chart(fig, use_container_width=True)

        sg = peers.dropna(subset=["RET_PCF", "GBA6RTT"]).copy()
        if not sg.empty:
            fig = px.scatter(sg, x="RET_PCF", y="GBA6RTT",
                             opacity=0.55, hover_name="INSTNM",
                             color="CONTROL_LBL", color_discrete_map=CONTROL_COLORS,
                             labels={"RET_PCF": "FT Retention Rate (%)", "GBA6RTT": "6-yr Grad Rate (%)",
                                     "CONTROL_LBL": "Control"},
                             title=f"Retention vs. 6-yr Grad Rate — {peer_label}",
                             **_trend_kw())
            _note = _star(fig, f_ret, f_gr6, "Retention: %{x:.1f}%", "6-yr GR: %{y:.1f}%")
            fig.update_layout(height=420)
            st.plotly_chart(fig, use_container_width=True)
            if _note:
                st.caption(_note)

        pct_gr6 = _pct(peers["GBA6RTT"], f_gr6, True)
        pct_ret = _pct(peers["RET_PCF"], f_ret, True)
        n_gr6   = len(peers["GBA6RTT"].dropna())
        q75_gr6 = peers["GBA6RTT"].quantile(0.75)
        lines = []
        if pct_gr6 is not None:
            lines.append(
                f"- {focus_name}'s **6-year graduation rate ({_fmt_pct(f_gr6)})** ranks at the "
                f"**{pct_gr6}th percentile** among {n_gr6:,} institutions in *{peer_label}*. "
                + ("This is a genuine strength — fewer than 25% of peers graduate students at a higher rate."
                   if pct_gr6 >= 75 else
                   f"There is meaningful room to improve: closing the gap to the 75th percentile threshold "
                   f"({_fmt_pct(q75_gr6)}) is a high-leverage opportunity."
                   if pct_gr6 < 50 else
                   f"Performance is solid but the top quartile threshold ({_fmt_pct(q75_gr6)}) is within reach.")
            )
        if pct_ret is not None:
            lines.append(
                f"- **First-year retention ({_fmt_pct(f_ret)})** is at the "
                f"**{pct_ret}th percentile** vs {peer_label}. "
                + ("Retention is a strong signal of first-year student experience and a leading indicator "
                   "for eventual graduation." if pct_ret >= 60 else
                   "First-year retention is the earliest leverage point for graduation outcomes. "
                   "Institutions that close this gap typically invest in structured first-year advising, "
                   "peer mentorship, and early-alert systems.")
            )
        if lines:
            st.markdown("\n".join(lines))

    # ── Tab: Equity & Access ──────────────────────────────────────────────────
    with d2:
        f_pell_pct   = _val("PGRNT_P")
        f_pell_gr    = _val("PGGRRTT")
        f_overall_gr = _val("GRRTTOT")
        pell_gap     = (f_pell_gr - f_overall_gr) if (f_pell_gr and f_overall_gr) else None
        urm_val      = sum((_val(c) or 0) for c in ["PCTENRBK","PCTENRHS","PCTENRAN","PCTENR2M"])

        peer_pell_pct_med = peers["PGRNT_P"].median(skipna=True)
        peer_pell_gr_med  = peers["PGGRRTT"].median(skipna=True)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("% Pell Recipients", _fmt_pct(f_pell_pct),
                  delta=f"{f_pell_pct - peer_pell_pct_med:+.1f}pp vs peer median" if f_pell_pct and not pd.isna(peer_pell_pct_med) else None)
        c2.metric("Pell Grad Rate",    _fmt_pct(f_pell_gr),
                  delta=f"{f_pell_gr - peer_pell_gr_med:+.1f}pp vs peer median" if f_pell_gr and not pd.isna(peer_pell_gr_med) else None)
        c3.metric("Pell vs Overall Gap", f"{pell_gap:+.1f}pp" if pell_gap is not None else "—",
                  help="Negative = Pell students graduate at lower rate than overall. Smaller gap = better equity.")
        c4.metric("% URM Students", f"{urm_val:.1f}%")

        eq_df = peers.dropna(subset=["PGRNT_P","PGGRRTT"]).copy()
        if not eq_df.empty:
            fig = px.scatter(eq_df, x="PGRNT_P", y="PGGRRTT",
                             opacity=0.55, hover_name="INSTNM",
                             color_discrete_sequence=["#059669"],
                             labels={"PGRNT_P": "% Pell Recipients", "PGGRRTT": "Pell Grad Rate (%)"},
                             title=f"Pell Enrollment vs. Pell Graduation Rate — {peer_label}",
                             **_trend_kw())
            _note = _star(fig, f_pell_pct, f_pell_gr, "% Pell: %{x:.1f}%", "Pell GR: %{y:.1f}%")
            fig.update_layout(height=420)
            st.plotly_chart(fig, use_container_width=True)
            if _note:
                st.caption(_note)

        pell_hist = peers["PGGRRTT"].dropna()
        if not pell_hist.empty and f_pell_gr:
            fig = px.histogram(pell_hist, x="PGGRRTT", nbins=max(10, min(40, len(pell_hist)//2)),
                               title=f"Pell Graduation Rate Distribution — {peer_label}",
                               labels={"PGGRRTT": "Pell Grad Rate (%)"})
            fig.update_layout(height=300, showlegend=False)
            _vline(fig, f_pell_gr, pell_hist)
            st.plotly_chart(fig, use_container_width=True)

        pct_pell_gr  = _pct(peers["PGGRRTT"], f_pell_gr, True)
        pct_pell_pct = _pct(peers["PGRNT_P"], f_pell_pct, True)
        lines = []
        if pct_pell_pct is not None:
            lines.append(
                f"- {focus_name} enrolls **{_fmt_pct(f_pell_pct)} Pell recipients**, placing it at the "
                f"**{pct_pell_pct}th percentile** for economic access within *{peer_label}*. "
                + ("This signals a meaningful commitment to first-generation and low-income students."
                   if pct_pell_pct >= 60 else
                   "There may be room to strengthen recruitment pipelines for first-generation and low-income students.")
            )
        if pct_pell_gr is not None:
            lines.append(
                f"- The **Pell graduation rate ({_fmt_pct(f_pell_gr)})** ranks at the "
                f"**{pct_pell_gr}th percentile** vs {peer_label}. "
                + ("This is a strong equity outcome — Pell students graduate at a high rate relative to peers."
                   if pct_pell_gr >= 65 else
                   "The equity graduation gap is an area warranting investment. High-impact practices — "
                   "intrusive advising, emergency aid funds, cohort-based learning communities — have "
                   "demonstrated evidence of closing Pell graduation gaps at peer institutions.")
            )
        if pell_gap is not None:
            lines.append(
                f"- The **Pell gap is {pell_gap:+.1f} percentage points** "
                f"(Pell grad rate minus overall grad rate). "
                + ("A gap this small signals that low-income students are not systematically disadvantaged in completion outcomes."
                   if pell_gap > -5 else
                   "A gap of this magnitude is a structural equity risk: students who rely on Pell Grants are "
                   "completing at meaningfully lower rates than their peers, even at the same institution.")
            )
        if lines:
            st.markdown("\n".join(lines))

    # ── Tab: Costs & Value ────────────────────────────────────────────────────
    with d3:
        f_coa     = _val("CINSON")
        f_tui     = _val("TUFEYR3")
        f_grant_a = _val("AGRNT_A")
        f_loan_p  = _val("LOAN_P")

        peer_coa_med   = peers["CINSON"].median(skipna=True)
        peer_grant_med = peers["AGRNT_A"].median(skipna=True)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("In-State COA", _fmt_dollar(f_coa),
                  delta=f"{(f_coa or 0) - peer_coa_med:+,.0f} vs peer median" if f_coa and not pd.isna(peer_coa_med) else None)
        c2.metric(f"Tuition {year}", _fmt_dollar(f_tui))
        c3.metric("Avg Any Grant",   _fmt_dollar(f_grant_a),
                  delta=f"{(f_grant_a or 0) - peer_grant_med:+,.0f} vs peer median" if f_grant_a and not pd.isna(peer_grant_med) else None)
        c4.metric("% Taking Loans",  _fmt_pct(f_loan_p))

        coa_hist = peers["CINSON"].dropna()
        if not coa_hist.empty and f_coa:
            fig = px.histogram(coa_hist, x="CINSON", nbins=max(10, min(40, len(coa_hist)//2)),
                               title=f"In-State COA Distribution — {peer_label}",
                               labels={"CINSON": "In-State COA ($)"})
            fig.update_layout(height=300, showlegend=False)
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            _vline(fig, f_coa, coa_hist)
            st.plotly_chart(fig, use_container_width=True)

        vdf = peers.dropna(subset=["CINSON","AGRNT_A"]).copy()
        if not vdf.empty:
            fig = px.scatter(vdf, x="CINSON", y="AGRNT_A",
                             opacity=0.55, hover_name="INSTNM",
                             color_discrete_sequence=["#2563EB"],
                             labels={"CINSON": "In-State COA ($)", "AGRNT_A": "Avg Grant Aid ($)"},
                             title=f"Cost vs. Grant Aid Generosity — {peer_label}",
                             **_trend_kw())
            _note = _star(fig, f_coa, f_grant_a, "COA: $%{x:,.0f}", "Avg Grant: $%{y:,.0f}")
            fig.update_layout(height=400)
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            fig.update_yaxes(tickprefix="$", tickformat=",.0f")
            st.plotly_chart(fig, use_container_width=True)
            if _note:
                st.caption(_note)
            st.caption(
                "Institutions **above the trend line** offer more generous grant aid than peers at the same sticker price — "
                "a key signal of 'hidden value.' Institutions **below the line** may appear affordable but under-fund students."
            )

        pct_coa   = _pct(peers["CINSON"], f_coa, False)
        pct_grant = _pct(peers["AGRNT_A"], f_grant_a, True)
        pct_loan  = _pct(peers["LOAN_P"], f_loan_p, False)
        lines = []
        if pct_coa is not None:
            lines.append(
                f"- {focus_name}'s **in-state COA ({_fmt_dollar(f_coa)})** ranks at the **{pct_coa}th percentile** "
                f"for affordability within *{peer_label}* (100 = most affordable). "
                + ("It is among the more affordable colleges in this peer group."
                   if pct_coa >= 60 else
                   "The sticker price is above the peer median. The critical question is whether grant aid brings the net price down to a competitive level.")
            )
        if pct_grant is not None:
            lines.append(
                f"- **Average grant aid ({_fmt_dollar(f_grant_a)})** ranks at the **{pct_grant}th percentile** "
                f"vs {peer_label}. "
                + ("Grant generosity is a genuine competitive differentiator — it can offset a higher sticker price and is a powerful enrollment lever."
                   if pct_grant >= 65 else
                   "Grant aid falls below the peer median. If the sticker price is also above average, the combination creates a net-price disadvantage in recruitment.")
            )
        if pct_loan is not None:
            lines.append(
                f"- **{_fmt_pct(f_loan_p)} of students take out loans**, ranking at the **{pct_loan}th percentile** "
                f"for low loan reliance vs {peer_label} (100 = fewest students borrowing). "
                + ("A lower loan rate suggests that grant aid and family resources are adequate for most students."
                   if pct_loan >= 55 else
                   "A higher-than-average loan rate may signal that aid packages are not keeping up with costs, creating financial stress that can impair retention.")
            )
        if lines:
            st.markdown("\n".join(lines))

    # ── Tab: Faculty & Resources ──────────────────────────────────────────────
    with d4:
        f_sal   = _val("SALTOTL")
        f_sfr   = _val("STUFACR")
        f_libx  = _val("LEXPTOTF")
        f_ebook = _val("LEBOOKSP")

        peer_sal_med  = peers["SALTOTL"].median(skipna=True)
        peer_sfr_med  = peers["STUFACR"].median(skipna=True)
        peer_libx_med = peers["LEXPTOTF"].median(skipna=True)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Avg Faculty Salary", _fmt_dollar(f_sal),
                  delta=f"{(f_sal or 0) - peer_sal_med:+,.0f} vs peer median" if f_sal and not pd.isna(peer_sal_med) else None)
        c2.metric("Student:Faculty Ratio", _fmt_num(f_sfr, 0) + ":1" if f_sfr else "—",
                  delta=f"{(f_sfr or 0) - peer_sfr_med:+.1f} vs peer median (lower is better)" if f_sfr and not pd.isna(peer_sfr_med) else None)
        c3.metric("Library Expend/FTE", _fmt_dollar(f_libx),
                  delta=f"{(f_libx or 0) - peer_libx_med:+,.0f} vs peer median" if f_libx and not pd.isna(peer_libx_med) else None)
        c4.metric("E-Books per FTE", _fmt_num(f_ebook))

        fdf = peers.dropna(subset=["SALTOTL","STUFACR"]).copy()
        if not fdf.empty:
            fig = px.scatter(fdf, x="STUFACR", y="SALTOTL",
                             opacity=0.55, hover_name="INSTNM",
                             color_discrete_sequence=["#7C3AED"],
                             labels={"STUFACR": "Student:Faculty Ratio", "SALTOTL": "Avg Faculty Salary ($)"},
                             title=f"Class Size vs. Faculty Compensation — {peer_label}")
            _note = _star(fig, f_sfr, f_sal, "S:F Ratio: %{x:.0f}", "Salary: $%{y:,.0f}")
            fig.update_layout(height=400)
            fig.update_yaxes(tickprefix="$", tickformat=",.0f")
            st.plotly_chart(fig, use_container_width=True)
            if _note:
                st.caption(_note)

        lib_hist = peers["LEXPTOTF"].dropna()
        if not lib_hist.empty and f_libx:
            fig = px.histogram(lib_hist, x="LEXPTOTF", nbins=max(10, min(40, len(lib_hist)//2)),
                               title=f"Library Expenditures per FTE — {peer_label}",
                               labels={"LEXPTOTF": "Library Expend/FTE ($)"})
            fig.update_layout(height=300, showlegend=False)
            fig.update_xaxes(tickprefix="$", tickformat=",.0f")
            _vline(fig, f_libx, lib_hist)
            st.plotly_chart(fig, use_container_width=True)

        pct_sal  = _pct(peers["SALTOTL"], f_sal, True)
        pct_sfr  = _pct(peers["STUFACR"], f_sfr, False)
        pct_libx = _pct(peers["LEXPTOTF"], f_libx, True)
        lines = []
        if pct_sfr is not None:
            lines.append(
                f"- {focus_name}'s **student-to-faculty ratio ({_fmt_num(f_sfr, 0)}:1)** ranks at the "
                f"**{pct_sfr}th percentile** for small class size within *{peer_label}*. "
                + ("A low ratio is a structural teaching advantage — it enables the faculty-student mentorship that larger institutions cannot easily replicate."
                   if pct_sfr >= 60 else
                   "The ratio is above the peer median, which may dilute the individualized attention that small institutions rely on as a value proposition.")
            )
        if pct_sal is not None:
            lines.append(
                f"- **Average faculty salary ({_fmt_dollar(f_sal)})** ranks at the **{pct_sal}th percentile** "
                f"vs {peer_label}. "
                + ("Competitive compensation helps attract and retain high-quality instructors — a direct input to academic quality."
                   if pct_sal >= 55 else
                   "Faculty compensation below the peer median creates recruitment risk; salary gaps compound over time as top candidates choose better-compensated alternatives.")
            )
        if pct_libx is not None:
            lines.append(
                f"- **Library expenditures per FTE student ({_fmt_dollar(f_libx)})** rank at the **{pct_libx}th percentile** "
                f"vs {peer_label}. "
                + ("Strong library investment per student signals serious commitment to research infrastructure for undergraduates."
                   if pct_libx >= 60 else
                   "Library resource investment per student is below the peer median — a direct enabler of undergraduate research and scholarship.")
            )
        if lines:
            st.markdown("\n".join(lines))

    st.divider()
    # ── Summary recommendation box ────────────────────────────────────────────
    st.subheader("Summary: Recommended Priority Actions")
    st.caption(f"Based on comparison against **{peer_label}** ({n_peers:,} institutions).")
    if needs_attn:
        for m in needs_attn:
            match = sc_df[sc_df["Metric"] == m]
            if not match.empty:
                row = match.iloc[0]
                st.markdown(
                    f"**{m}** — {focus_name} is at the **{row[pct_col]}th percentile** "
                    f"vs {peer_label}. Current value: **{row[VAL_COL]}**."
                )
    else:
        st.success(
            f"{focus_name} performs in the top 40% of **{peer_label}** on all tracked metrics. "
            "The strategic focus should be on sustaining current strengths while targeting the "
            "gap between average and top-quartile performance."
        )


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    _ensure_db()   # no-op locally if DB exists; downloads from Google Drive on Cloud

    # ── Global font-size enlargement ─────────────────────────────────────────
    st.markdown("""
    <style>
        /* Base prose & markdown */
        .stMarkdown p, .stMarkdown li, .stMarkdown td, .stMarkdown th { font-size:1.05rem !important; line-height:1.75 !important; }
        /* Headers */
        h1  { font-size:2.1rem  !important; }
        h2  { font-size:1.65rem !important; }
        h3  { font-size:1.35rem !important; }
        h4  { font-size:1.15rem !important; }
        /* Captions */
        [data-testid="stCaptionContainer"] p { font-size:0.95rem !important; }
        /* Metrics */
        [data-testid="stMetricLabel"] > div  { font-size:0.62rem !important; }
        [data-testid="stMetricValue"] > div  { font-size:0.75rem !important; }
        [data-testid="stMetricDelta"] > div  { font-size:0.58rem !important; }
        /* Tab labels */
        .stTabs [data-baseweb="tab"] { font-size:1rem !important; padding:8px 16px !important; }
        /* Selectbox / multiselect / radio labels */
        .stSelectbox > label, .stMultiSelect > label,
        .stCheckbox > label, .stRadio > label { font-size:1.05rem !important; }
        /* Sidebar radio items */
        .stSidebar .stRadio label span { font-size:1.05rem !important; }
        /* Alert / info / success / warning boxes */
        .stAlert p { font-size:1rem !important; }
        /* Dataframe text */
        .stDataFrame iframe { font-size:14px !important; }
        /* Expander label */
        .stExpander summary p { font-size:1.05rem !important; }
        /* General widget labels */
        label[data-baseweb] { font-size:1.05rem !important; }
    </style>
    """, unsafe_allow_html=True)

    if "page" not in st.session_state:
        st.session_state["page"] = "Institution of Interest Analysis"
    if "sel_inst" not in st.session_state:
        st.session_state["sel_inst"] = None

    st.sidebar.title("🎓 IPEDS Dashboard")
    pages = ["Institution of Interest Analysis", "National Overview", "Institution Profile",
             "Year-over-Year"]
    page_idx = pages.index(st.session_state["page"]) if st.session_state["page"] in pages else 0
    page = st.sidebar.radio(
        "Navigate",
        pages,
        index=page_idx,
        label_visibility="collapsed",
    )
    if page != st.session_state["page"]:
        st.session_state["page"] = page
    st.sidebar.divider()

    # ── Global "Institution of Interest" (drives the Analysis page + highlights) ──
    _PLACEHOLDER = "— select an institution —"
    _name_df = (load_master("2024-25")[["UNITID", "INSTNM", "DISPLAY_NAME"]]
                .dropna(subset=["DISPLAY_NAME"]).drop_duplicates("UNITID").sort_values("DISPLAY_NAME"))
    _display_names = _name_df["DISPLAY_NAME"].tolist()

    st.sidebar.markdown("**🎯 Institution of Interest**")
    _typed = st.sidebar.text_input(
        "Type to narrow", key="focus_filter",
        placeholder="Type a name or state to narrow…", label_visibility="collapsed")
    _filt = [n for n in _display_names if _typed.lower() in n.lower()] if _typed else list(_display_names)
    _prev = st.session_state.get("focus_display")
    if _prev and _prev not in _filt and _prev in _display_names:
        _filt = [_prev] + _filt
    _options = [_PLACEHOLDER] + _filt
    _idx = _options.index(_prev) if _prev in _options else 0
    _focus_sel = st.sidebar.selectbox(
        "Institution of Interest", _options, index=_idx, label_visibility="collapsed",
        help="Pick any institution; the Analysis, Overview, Profile, Compare and Year-over-Year "
             "pages center on it. Leave unset to browse without a focus.",
    )
    if _focus_sel == _PLACEHOLDER:
        st.session_state["focus_display"] = None
        st.session_state["focus_unitid"]  = None
        st.session_state["focus_name"]    = None
    else:
        if _focus_sel != st.session_state.get("focus_display"):
            st.session_state["sel_inst"] = _focus_sel   # sync Institution Profile selector
        st.session_state["focus_display"] = _focus_sel
        _r = _name_df[_name_df["DISPLAY_NAME"] == _focus_sel].iloc[0]
        st.session_state["focus_unitid"] = int(_r["UNITID"])
        st.session_state["focus_name"]   = str(_r["INSTNM"])
    st.sidebar.divider()

    # ── Determine selected data year for this page ────────────────────────────
    _YEAR_PAGES = {"Institution of Interest Analysis", "National Overview",
                   "Institution Profile"}
    if page in _YEAR_PAGES:
        year = st.session_state.get(f"year_{page}", "2024-25")
    else:
        year = "2024-25"

    # ── Load data (year-parameterized; cached per year) ───────────────────────
    if page != "Year-over-Year":
        df = load_master(year)

    # ── Apply sidebar filters ─────────────────────────────────────────────────
    if page == "National Overview":
        df_filtered, sel_groups = apply_filters(df.copy())
    elif page != "Year-over-Year":
        df_filtered = df
        sel_groups = []

    # ── Route to page ─────────────────────────────────────────────────────────
    if page == "Institution of Interest Analysis":
        _fuid = st.session_state.get("focus_unitid")
        if not _fuid:
            st.title("Institution of Interest — Strategic Performance Analysis")
            st.info("👈 Pick an **Institution of Interest** in the sidebar to begin — "
                    "then choose or accept the recommended peer institutions to compare against.")
        else:
            page_strategic(df, _fuid, st.session_state.get("focus_name") or "", year)
    elif page == "National Overview":
        page_overview(df_filtered, sel_groups, year)
    elif page == "Institution Profile":
        page_profile(df_filtered, year)
    else:
        page_trends()

    # ── Sidebar footer ────────────────────────────────────────────────────────
    st.sidebar.divider()

    # DB status diagnostic
    if os.path.exists(DB_PATH):
        size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
        try:
            _con = duckdb.connect(DB_PATH, read_only=True)
            yr_rows = _con.execute(
                "SELECT YEAR, COUNT(*) AS N FROM METRICS_LONG GROUP BY YEAR ORDER BY YEAR"
            ).fetchall()
            _con.close()
            yr_info = "  \n".join(f"  {y}: {n:,}" for y, n in yr_rows)
            st.sidebar.caption(f"**DB:** {size_mb:.0f} MB  \n**Years in DB:**  \n{yr_info}")
        except Exception:
            st.sidebar.caption(f"**DB:** {size_mb:.0f} MB (single-year — needs refresh)")
    else:
        st.sidebar.caption("**DB:** not found")

    # Refresh only deletes the on-disk DB when it can be re-downloaded (Cloud has a
    # GDRIVE_DB_ID secret). Locally there is no remote source, so deleting it would be
    # unrecoverable — there we just clear the cache and re-read the existing file.
    try:
        _has_remote = bool(st.secrets.get("GDRIVE_DB_ID", None))
    except Exception:
        _has_remote = False
    if st.sidebar.button("🔄 Refresh Data", use_container_width=True,
                         help=("Re-downloads the database from Google Drive." if _has_remote
                               else "Clears the cache and re-reads the local database (the local "
                                    "file is NOT deleted).")):
        st.cache_data.clear()
        if _has_remote and os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        st.rerun()
    st.sidebar.markdown(
        "<div style='font-size:0.82rem;color:#6B7280;text-align:center;line-height:1.5;padding-top:6px;'>"
        "IPEDS Interactive Dashboard<br>Built by Nan Dong, dongnan2017@gmail.com"
        "</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
