"""HR review in Streamlit, backed by local authorization-checked endpoints."""

import streamlit as st

from streamlit_client import ApiError, ApiUnavailable
from streamlit_graph_rejection import render_graph_rejection
from streamlit_graph_screen import render_graph_screen
from streamlit_hr_lifecycle import render_hr_lifecycle


def _discard_preview():
    st.session_state.pop("hr_review_preview", None)


def _decision(api, application_id, preview, decision, note, clear_session):
    try:
        return api.request("POST", f"/api/applications/{application_id}/decision", json={
            "decision": decision, "note": note, "review_reference": preview["reference"],
        })
    except ApiError as exc:
        _discard_preview()
        if exc.status in (401, 403):
            clear_session()
            st.error("Your session or access has changed. Sign in again.")
            st.stop()
        if exc.status == 409:
            st.session_state["hr_review_message"] = "Review expired or changed. Open the application again before deciding."
        elif exc.status >= 500:
            st.session_state["hr_review_message"] = "The decision could not be confirmed. Refresh and check the application before trying again."
        else:
            st.session_state["hr_review_message"] = "The decision was not accepted. Review the application before trying again."
        st.rerun()
    except ApiUnavailable:
        _discard_preview()
        st.session_state["hr_review_message"] = "The decision could not be confirmed. Refresh and check the application before trying again."
        st.rerun()


def render_hr_review(call, local_api, clear_session):
    st.header("HR application review")
    message = st.session_state.pop("hr_review_message", None)
    if message:
        st.info(message)
    jobs = call("GET", "/api/jobs", optional=True)
    if jobs is None:
        return
    if not jobs:
        st.info("No jobs are available.")
        return
    job_names = {job["id"]: job["title"] for job in jobs}
    job_id = st.selectbox("Review job", list(job_names), key="hr_review_job",
                          format_func=lambda value: job_names[value])
    if st.session_state.get("hr_queue_job") != job_id:
        st.session_state["hr_queue_job"] = job_id
        st.session_state["hr_queue_after"] = 0
        _discard_preview()
    after = st.session_state.get("hr_queue_after", 0)
    queue = call("GET", f"/api/jobs/{job_id}/provisional-queue", optional=True,
                 params={"after": after, "limit": 20})
    if queue is None:
        return
    st.caption("Every application needs human review. Provisional keyword results are not verified qualifications.")
    if st.button("Show reviewed assessment comparison"):
        comparison = call("GET", f"/api/jobs/{job_id}/rubric/comparison", optional=True)
        if comparison is not None:
            st.caption(comparison["notice"])
            for item in comparison["applications"]:
                bounds = item["bounds"]
                value = (f"{bounds['minimum']}–{bounds['maximum']}; coverage {bounds['coverage_percent']}%"
                         if bounds else item["status"])
                st.text(f"Application #{item['application_id']}: {value}")
            for pair in comparison["comparisons"]:
                relation = {"left_higher": "left range higher", "right_higher": "right range higher",
                            "inconclusive": "overlapping or touching ranges; inconclusive"}.get(pair["relation"], "inconclusive")
                st.text(f"#{pair['left_application_id']} vs #{pair['right_application_id']}: {relation}")
    items = queue["items"]
    if not items:
        st.info("No applications on this page.")
    else:
        by_id = {item["application_id"]: item for item in items}
        selected = st.selectbox("Review application", list(by_id), key="hr_review_application",
                                format_func=lambda value: f"Application #{value} — {by_id[value]['application_status']}")
        item = by_id[selected]
        hire_ready = render_hr_lifecycle(call, local_api, clear_session, item)
        render_graph_screen(local_api(), clear_session, selected, job_id)
        render_graph_rejection(local_api(), clear_session, selected, job_id)
        st.text(f"Provisional status: {item['recommendation']}")
        preview = st.session_state.get("hr_review_preview")
        if preview and (preview["application_id"] != selected or preview["job_id"] != job_id):
            _discard_preview()
            preview = None
        if st.button("Open masked review"):
            _discard_preview()
            candidate = call("POST", f"/api/applications/{selected}/hr-review-preview", optional=True)
            if candidate is not None:
                if candidate["application_id"] != selected or candidate["job_id"] != job_id:
                    st.error("Application changed. Reload the queue before review.")
                else:
                    st.session_state["hr_review_preview"] = candidate
                    st.rerun()
        if preview:
            st.text(f"Reviewed job: {preview['job']['title']}")
            st.text("Required skills: " + ", ".join(preview['job']['required_skills']))
            st.text("Preferred skills: " + ", ".join(preview['job']['preferred_skills']))
            st.caption(preview["notice"])
            st.caption(f"Mask review: {preview['source_status']}. Preview expires in 10 minutes; reload after changes.")
            st.text(preview["source"])
            choices = {"submitted": ["advance", "reject"], "advanced": ["reject"], "offered": ["reject"]}.get(preview["status"], []).copy()
            if preview["status"] == "offered" and hire_ready:
                choices.insert(0, "hire")
                st.caption("The current offer is accepted. Hiring remains an explicit reviewed decision.")
            if choices:
                with st.form(f"hr_decision_{selected}"):
                    decision = st.radio("Decision", choices, index=None, format_func=lambda value: value.title())
                    note = st.text_area("Reason (required for rejection)", max_chars=1000)
                    submit = st.form_submit_button("Record reviewed decision")
                if submit:
                    if decision is None:
                        st.error("Choose a decision.")
                    elif decision == "reject" and not note.strip():
                        st.error("Enter a reason before rejecting the application.")
                    elif preview["application_id"] == selected and preview["job_id"] == job_id:
                        result = _decision(local_api(), selected, preview, decision, note, clear_session)
                        if result is not None:
                            _discard_preview()
                            st.session_state["hr_review_message"] = "Decision recorded locally. No message was sent."
                            st.rerun()
            else:
                st.info("No decision is available for this application at its current stage.")
    if after and st.button("First page of applications"):
        st.session_state["hr_queue_after"] = 0
        _discard_preview()
        st.rerun()
    if queue.get("next_after") is not None and st.button("Next page of applications"):
        st.session_state["hr_queue_after"] = queue["next_after"]
        _discard_preview()
        st.rerun()
