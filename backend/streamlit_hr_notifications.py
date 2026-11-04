"""Local submission notifications for the current HR account."""

import streamlit as st


def render_hr_notifications(call):
    st.subheader("Submission notifications")
    before = st.session_state.get("hr_notifications_before")
    params = {"before": before} if before is not None else {}
    data = call("GET", "/api/hr/notifications", optional=True, params=params)
    if data is None:
        return
    st.caption(data["notice"])
    if not data["items"]:
        st.info("No notifications on this page.")
    for item in data["items"]:
        st.text(f"Application #{item['application_id']} | submitted job {item['submitted_job_id']} | current job {item['current_job_id']} | status {item['current_status']} | {'read' if item['is_read'] else 'unread'}")
        if not item["is_read"] and st.button("Mark read", key=f"notification_{item['id']}"):
            result = call("POST", f"/api/hr/notifications/{item['id']}/read", optional=True)
            if result is not None:
                st.rerun()
    if data["next_before"] is not None and st.button("Older notifications"):
        st.session_state["hr_notifications_before"] = data["next_before"]
        st.rerun()
    if before is not None and st.button("Newest notifications"):
        st.session_state.pop("hr_notifications_before", None)
        st.rerun()


def render_hr_cancellations(call):
    """Show per-HR local interview cancellation notices."""
    st.subheader("Interview cancellation notices")
    before = st.session_state.get("hr_cancellations_before")
    params = {"before": before} if before is not None else {}
    data = call("GET", "/api/hr/interview-cancellations", optional=True, params=params)
    if data is None:
        return
    st.caption(data["notice"])
    if not data["items"]:
        st.info("No interview cancellations on this page.")
    for item in data["items"]:
        st.text(f"Application #{item['application_id']} | Interview #{item['interview_id']} | {item['start_at']} – {item['end_at']} | {'read' if item['is_read'] else 'unread'}")
        if not item['is_read'] and st.button("Mark cancellation read", key=f"cancellation_{item['id']}"):
            result = call("POST", f"/api/hr/interview-cancellations/{item['id']}/read", optional=True)
            if result is not None:
                st.rerun()
    if data['next_before'] is not None and st.button("Older cancellations"):
        st.session_state['hr_cancellations_before'] = data['next_before']
        st.rerun()
    if before is not None and st.button("Newest cancellations"):
        st.session_state.pop('hr_cancellations_before', None)
        st.rerun()
