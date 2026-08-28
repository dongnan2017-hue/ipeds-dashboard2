"""Shared-password gate for the whole app.

Called once from app.py before any page runs, so neither view - nor any of the
data behind them - is reachable without the password. The password lives in
Streamlit secrets, never in the repository:

  local           .streamlit/secrets.toml   (gitignored)
  Streamlit Cloud App menu -> Settings -> Secrets

This is a shared door key, not per-person identity. Pair it with the viewer
allowlist in Streamlit Cloud if you need to know who is looking.
"""

from __future__ import annotations

import hmac

import streamlit as st

BRAND = "#5a2d82"


def _expected_password() -> str | None:
    """The configured password, or None if secrets are missing entirely."""
    try:
        return st.secrets["app_password"]
    except Exception:
        return None


def require_password() -> None:
    """Show a lock screen and halt unless the visitor has already authenticated."""
    if st.session_state.get("authenticated"):
        return

    expected = _expected_password()
    if not expected:
        st.error(
            "No password is configured, so the app will not open. Add "
            "`app_password` under Settings → Secrets (or to a local "
            "`.streamlit/secrets.toml`) and reload.",
            icon=":material/lock:",
        )
        st.stop()

    st.markdown(
        f"""
        <div style="max-width:420px;margin:14vh auto 0">
          <div style="font-family:ui-monospace,monospace;font-size:11px;letter-spacing:.14em;
                      text-transform:uppercase;color:{BRAND}">IPEDS 2024-25</div>
          <h1 style="font-size:30px;letter-spacing:-.02em;margin:8px 0 6px">Dashboard</h1>
          <p style="color:#52514e;margin:0 0 18px">
            Enter the password to continue.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    left, mid, right = st.columns([1, 2, 1])
    with mid:
        with st.form("password_gate", clear_on_submit=True):
            entered = st.text_input("Password", type="password",
                                    label_visibility="collapsed",
                                    placeholder="Password")
            submitted = st.form_submit_button("Enter", width="stretch")

        if submitted:
            # constant-time compare, so a wrong guess leaks nothing by timing
            if hmac.compare_digest(entered.strip(), str(expected)):
                st.session_state["authenticated"] = True
                st.rerun()
            st.error("That password is not right.", icon=":material/error:")

    st.stop()
