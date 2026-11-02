"""Candidate-facing Streamlit workflows backed by the local API."""

import streamlit as st

from streamlit_client import ApiError, ApiUnavailable

MAX_RESUME_BYTES = 5 * 1024 * 1024


def _write(api, path, *, conflict_message=None, uncertain_message=None, on_auth_failure=None, **kwargs):
    """Submit once; never repeat an uncertain write automatically."""
    try:
        return api.request("POST", path, **kwargs)
    except ApiError as exc:
        if exc.status >= 500:
            st.warning(uncertain_message or "The result could not be confirmed. Refresh and check your applications or interviews before trying again.")
        elif exc.status == 409:
            st.error(conflict_message or "The application or slot changed. Refresh the page and review its current state.")
        elif exc.status in (401, 403):
            if on_auth_failure is not None:
                on_auth_failure()
                st.error("Your session or access has changed. Sign in again.")
                st.stop()
            st.error("Your session or access has changed. Sign out and sign in again.")
        else:
            st.error(f"The request was rejected ({exc.status}). Check your input and try again.")
    except ApiUnavailable:
        st.warning(uncertain_message or "The result could not be confirmed. Refresh and check your applications or interviews before trying again.")
    return None


def submit_application(api, job_id, filename, content, ocr, ocr_language, on_auth_failure=None):
    return _write(api, "/api/applications", on_auth_failure=on_auth_failure,
                  params={"job_id": job_id, "filename": filename,
                          "ocr": str(ocr).lower(), "ocr_language": ocr_language},
                  content=content, headers={"Content-Type": "application/octet-stream"})


def render_candidate(call, local_api, clear_session):
    st.header("Candidate applications")
    message = st.session_state.pop("candidate_message", None)
    if message:
        st.success(message)
    jobs = call("GET", "/api/jobs")
    job_names = {job["id"]: job["title"] for job in jobs}
    if jobs:
        with st.form("candidate_apply", clear_on_submit=True):
            job_id = st.selectbox("Position", list(job_names), format_func=lambda value: job_names[value])
            resume = st.file_uploader("Resume (PDF or DOCX, maximum 5 MiB)", type=["pdf", "docx"], max_upload_size=5)
            ocr = st.checkbox("Use OCR for a scanned PDF (requires OCR configured on the API)")
            ocr_language = st.selectbox("OCR language", ["eng", "eng+ara"],
                                        format_func=lambda value: "English" if value == "eng" else "English and Arabic")
            submitted = st.form_submit_button("Submit application")
        if submitted:
            if resume is None:
                st.error("Choose a resume before submitting.")
            elif resume.size > MAX_RESUME_BYTES:
                st.error("The resume exceeds the 5 MiB limit.")
            elif ocr and not resume.name.lower().endswith(".pdf"):
                st.error("OCR is available only for PDF files.")
            else:
                result = submit_application(local_api(), job_id, resume.name, resume.getvalue(), ocr, ocr_language, clear_session)
                if result is not None:
                    st.session_state["candidate_message"] = "Application submitted."
                    st.rerun()
    else:
        st.info("No positions are currently available.")

    applications = call("GET", "/api/applications")
    if not applications:
        st.info("You have not submitted an application yet.")
        return
    by_id = {item["id"]: item for item in applications}
    selected = st.selectbox("Your application", list(by_id), key="candidate_application",
                            format_func=lambda value: f"{job_names.get(by_id[value]['job_id'], by_id[value]['job_id'])} (#{value})")
    application = by_id[selected]
    progress = call("GET", f"/api/workflows/{selected}/progress")
    st.text(f"Current stage: {progress['stage'].replace('_', ' ').title()}")
    st.text(f"Submitted: {application['created_at']}")

    with st.expander("Current keyword preview"):
        st.caption("Preview supports resumes up to 4,000 characters and jobs with up to 20 skills of 80 characters each.")
        if st.button("Load current keyword preview"):
            preview = call("GET", f"/api/applications/{selected}/candidate-screening", optional=True)
            if preview is not None:
                st.caption(preview["notice"])
                st.text(f"Required-skill mentions: {preview['score']}%")
                for label, field in (("Required", "required"), ("Preferred", "preferred")):
                    st.text(label)
                    for item in preview[field]:
                        st.text(f"  {item['skill']}: {'mentioned' if item['mentioned'] else 'not detected'}")

    with st.expander("Assessment shared by HR"):
        assessment = call("GET", f"/api/applications/{selected}/candidate-assessment", optional=True)
        if assessment is None:
            pass
        elif assessment["status"] != "current":
            st.info("No current assessment has been shared.")
        else:
            st.caption(assessment["notice"])
            bounds = assessment["bounds"]
            st.text(f"Score range: {bounds['minimum']}–{bounds['maximum']} | Coverage: {bounds['coverage_percent']}%")
            for item in assessment["criteria"]:
                st.text(f"{item['category']} — {item['criterion']}: {item['status']}")
                for excerpt in item["evidence"]:
                    st.text(excerpt)

    render_offers(call, local_api, clear_session, selected)

    st.subheader("Interviews")
    interviews = call("GET", "/api/interviews")
    own_interviews = [item for item in interviews if item["application_id"] == selected]
    if own_interviews:
        for interview in own_interviews:
            st.text(f"{interview['start_at']} – {interview['end_at']} | {interview['interviewer']} | {interview['status']}")
    else:
        st.info("No interviews have been recorded for this application.")
    if application["status"] == "advanced":
        slots = call("GET", "/api/slots", params={"application_id": selected})
        if slots:
            choices = {slot["id"]: slot for slot in slots}
            with st.form("candidate_booking"):
                slot_id = st.selectbox("Available interview time", list(choices),
                    format_func=lambda value: f"{choices[value]['start_at']} – {choices[value]['end_at']} | {choices[value]['interviewer']}")
                book = st.form_submit_button("Book selected interview")
            if book:
                result = _write(local_api(), f"/api/slots/{slot_id}/book", on_auth_failure=clear_session, json={"application_id": selected})
                if result is not None:
                    st.session_state["candidate_message"] = "Interview booked in the local system."
                    st.rerun()
        else:
            st.info("There are no available interview times for this application right now.")
    else:
        st.caption("Interview booking becomes available if HR advances your application.")


def render_offers(call, local_api, clear_session, application_id):
    """Display only the selected application's offers and submit one explicit response."""
    st.subheader("Job offers")
    offers = call("GET", f"/api/applications/{application_id}/offers", optional=True)
    if offers is None:
        return
    if not isinstance(offers, list) or any(offer.get("application_id") != application_id for offer in offers):
        st.error("The offer list could not be verified. Refresh and try again.")
        return
    if not offers:
        st.info("No offer has been presented for this application.")
        return
    for offer in offers:
        st.text(f"Offer #{offer['id']} | Status: {offer['status']}")
        st.text(offer['details'])
        st.caption(f"Presented: {offer['created_at']}")
        if offer['status'] == "presented":
            with st.form(f"offer_response_{offer['id']}"):
                decision = st.radio("Your response", ["Accept", "Decline"], index=None,
                                    key=f"offer_decision_{offer['id']}")
                submit = st.form_submit_button("Submit offer response")
            if submit:
                if decision is None:
                    st.error("Select a response before submitting.")
                    continue
                result = _write(local_api(), f"/api/offers/{offer['id']}/respond",
                    on_auth_failure=clear_session,
                    conflict_message="This offer is no longer awaiting a response. Refresh and review its current status.",
                    uncertain_message="The response could not be confirmed. Refresh and check the offer status before trying again.",
                    json={"decision": "accepted" if decision == "Accept" else "declined"})
                if result is not None:
                    st.session_state["candidate_message"] = "Your response was recorded in the local system."
                    st.rerun()
