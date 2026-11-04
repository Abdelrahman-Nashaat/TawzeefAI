"""Local Streamlit chat and HR metrics, backed by the existing FastAPI server."""

import os

import altair as alt
import pandas as pd
import streamlit as st

from streamlit_candidate import render_candidate
from streamlit_client import ApiError, ApiUnavailable, LocalApi
from streamlit_hr import render_hr_review
from streamlit_hr_notifications import render_hr_cancellations, render_hr_notifications

st.set_page_config(page_title="TawzeefAI local demo", layout="wide")
st.title("TawzeefAI")
st.caption("Local demonstration with synthetic data. HR makes all hiring decisions.")


def clear_session():
    client = st.session_state.pop("local_api", None)
    if client:
        client.close()
    for key in ("conversation_id", "question", "current_user", "candidate_message", "hr_review_preview", "hr_review_message", "hr_notifications_before", "hr_cancellations_before", "hr_slot_proposal", "hr_lifecycle_message", "graph_screen_review", "graph_screen_notice", "graph_reject_review", "graph_reject_notice", "graph_reject_clear_reason"):
        st.session_state.pop(key, None)
    for key in list(st.session_state):
        if key.startswith(('graph_reject_reason_', 'graph_reject_confirm_')):
            st.session_state.pop(key, None)


def local_api():
    if "local_api" not in st.session_state:
        try:
            port = int(os.environ.get("TAWZEEF_STREAMLIT_API_PORT", "8000"))
            st.session_state["local_api"] = LocalApi(port)
        except ValueError:
            st.error("The local API port is invalid.")
            st.stop()
    return st.session_state["local_api"]


def call(method, path, *, optional=False, **kwargs):
    try:
        return local_api().request(method, path, **kwargs)
    except ApiError as exc:
        if exc.status in (401, 403):
            clear_session()
            st.error("Your session is unavailable or access has changed. Sign in again.")
            st.stop()
        if optional:
            st.warning("This panel is unavailable. Refresh and try again.")
            return None
        st.error(f"The local API rejected the request ({exc.status}). Reload and try again.")
        st.stop()
    except ApiUnavailable:
        if optional:
            st.warning("This panel is unavailable. Refresh and try again.")
            return None
        st.error("Cannot reach the local API. Start the FastAPI demo and try again.")
        st.stop()


if "current_user" not in st.session_state:
    if st.session_state.pop("logout_uncertain", False):
        st.warning("Server sign-out could not be confirmed. The local session was cleared.")
    with st.form("login", clear_on_submit=True):
        username = st.text_input("Username", max_chars=128)
        password = st.text_input("Password", type="password", max_chars=256)
        login = st.form_submit_button("Sign in")
    if login:
        try:
            user = local_api().request("POST", "/api/auth/login", json={"username": username, "password": password})
        except ApiError:
            st.error("Sign-in failed. Check your credentials.")
        except ApiUnavailable:
            st.error("Cannot reach the local API. Start the FastAPI demo and try again.")
        else:
            st.session_state["current_user"] = user
            st.rerun()
    st.stop()

# Always ask the API; an old Streamlit session is not proof of current access.
user = call("GET", "/api/auth/me")
st.session_state["current_user"] = user
st.sidebar.text(f"Signed in as {user['display_name']} ({user['role']})")
if st.sidebar.button("Sign out"):
    try:
        local_api().request("POST", "/api/auth/logout")
    except (ApiError, ApiUnavailable):
        st.session_state["logout_uncertain"] = True
    clear_session()
    st.rerun()

if user["role"] == "candidate":
    render_candidate(call, local_api, clear_session)

if user["role"] == "hr":
    st.header("Recruitment dashboard")
    metrics = call("GET", "/api/dashboard")
    a, b, c = st.columns(3)
    a.metric("Applications", metrics["applications_total"])
    b.metric("Screening events", metrics["screening_events"])
    average = metrics["time_to_hire"]["average_days"]
    c.metric("Average days to hire", "Unavailable" if average is None else str(average))
    st.caption(metrics["time_to_hire"]["definition"])
    stages = metrics["recorded_milestones"]
    if stages:
        chart_data = pd.DataFrame(stages)
        chart = alt.Chart(chart_data).mark_bar().encode(
            x=alt.X("count:Q", title="Applications"),
            y=alt.Y("stage:N", title=None, sort=chart_data["stage"].tolist()),
            tooltip=["stage:N", "count:Q"],
        )
        st.altair_chart(chart, width="stretch")
        st.table(chart_data)
    st.caption("Distinct retained applications with recorded events; later reversals do not erase history. These are not conversion rates.")
    render_hr_notifications(call)
    render_hr_cancellations(call)
    render_hr_review(call, local_api, clear_session)

st.header("Knowledge conversation")
st.caption("Questions and authorized excerpts may be sent to the configured model. Use synthetic data and verify sources.")
conversations = call("GET", "/api/conversations")
if st.button("New conversation"):
    created = call("POST", "/api/conversations", json={"title": "Streamlit conversation"})
    st.session_state["conversation_id"] = created["id"]
    st.rerun()
ids = [item["id"] for item in conversations]
selected = st.session_state.get("conversation_id")
if selected not in ids:
    selected = ids[0] if ids else None
if ids:
    labels = {item["id"]: f"{item['title']} (#{item['id']})" for item in conversations}
    selected = st.selectbox("Conversation", ids, index=ids.index(selected), format_func=lambda value: labels[value])
    st.session_state["conversation_id"] = selected
    conversation = call("GET", f"/api/conversations/{selected}")
    for message in conversation["messages"]:
        with st.chat_message("user"):
            st.text(message["question"])
        with st.chat_message("assistant"):
            st.text(message["answer"])
            if message.get("next_step"):
                st.info(message["next_step"])
            if message.get("notice"):
                st.caption(message["notice"])
            for source in message.get("sources", []):
                st.text(f"{source['title']} | {source['id']} | page {source.get('page') or 'not available'}")
                st.text(source["text"])
                if source.get("extraction") == "ocr":
                    st.caption("OCR text: verify against the original page.")
    mode = st.selectbox("Search mode", ("general", "policy"), format_func=lambda value: value.title())
    with st.form("ask", clear_on_submit=True):
        question = st.text_area("Question", max_chars=2000, key="question")
        submit = st.form_submit_button("Ask")
    if submit:
        if not question.strip():
            st.error("Enter a question.")
        else:
            call("POST", f"/api/conversations/{selected}/messages", json={"question": question, "mode": mode})
            st.rerun()
else:
    st.info("Create a conversation to ask a question.")

st.caption("Use the main local interface for advanced HR and administrative controls.")
