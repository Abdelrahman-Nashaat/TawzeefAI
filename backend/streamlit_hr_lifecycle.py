"""Explicit local scheduling and offer actions for the selected HR application."""

from datetime import UTC, datetime

import streamlit as st

from streamlit_client import ApiError, ApiUnavailable


def _action(api, path, clear_session, *, json=None, params=None):
    """Submit exactly once and require the user to check state after uncertainty."""
    try:
        return api.request("POST", path, json=json, params=params)
    except ApiError as exc:
        if exc.status in (401, 403):
            clear_session()
            st.error("Your session or access has changed. Sign in again.")
            st.stop()
        if exc.status == 409:
            st.error("The application, slot, or interview changed. Reload and review the current state.")
        elif exc.status >= 500:
            st.warning("The result could not be confirmed. Reload and check the current state before trying again.")
        else:
            st.error("The action was not accepted. Check the inputs and current application state.")
    except ApiUnavailable:
        st.warning("The result could not be confirmed. Reload and check the current state before trying again.")
    return None


def _offset_time(value):
    try:
        result = datetime.fromisoformat(value.strip())
        if result.tzinfo is None or result.utcoffset() is None:
            return None
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _refresh(message):
    st.session_state["hr_lifecycle_message"] = message
    st.rerun()


def render_hr_lifecycle(call, local_api, clear_session, application):
    application_id = application["application_id"]
    status = application["application_status"]
    st.subheader("Local interviews and offers")
    st.caption("Slots are reserved for this application. No email or external calendar event is sent.")
    message = st.session_state.pop("hr_lifecycle_message", None)
    if message:
        st.success(message)

    proposal = st.session_state.get("hr_slot_proposal")
    if proposal and proposal["application_id"] != application_id:
        st.session_state.pop("hr_slot_proposal", None)
        proposal = None
    if status == "advanced":
        with st.form(f"hr_slot_{application_id}"):
            start = st.text_input("Slot start with UTC offset", placeholder="2027-01-31T10:00:00+02:00")
            end = st.text_input("Slot end with UTC offset", placeholder="2027-01-31T10:30:00+02:00")
            interviewer = st.text_input("Interviewer", max_chars=120)
            prepare = st.form_submit_button("Preview private slot")
        if prepare:
            start_time, end_time = _offset_time(start), _offset_time(end)
            if not start_time or not end_time or end_time <= start_time or not interviewer.strip():
                st.error("Enter valid start and end times with UTC offsets and an interviewer.")
            else:
                st.session_state.pop("hr_slot_proposal", None)
                result = _action(local_api(), "/api/slots/batches/preview", clear_session, json={"items": [{
                    "application_id": application_id, "start_at": start_time.isoformat(),
                    "end_at": end_time.isoformat(), "interviewer": interviewer.strip(),
                }]})
                if result is not None:
                    items = result.get("items", [])
                    if len(items) != 1 or items[0].get("application_id") != application_id:
                        st.error("The slot preview could not be verified. Reload before trying again.")
                    else:
                        st.session_state["hr_slot_proposal"] = {
                            "application_id": application_id, "proposal_id": result["proposal_id"],
                            "fingerprint": result["fingerprint"], "item": items[0],
                        }
                        st.rerun()
        if proposal:
            slot = proposal["item"]
            st.text(f"Preview: {slot['start_at']} to {slot['end_at']} | {slot['interviewer']} | application #{application_id}")
            st.caption("Review the time and interviewer before releasing the slot to this candidate.")
            if st.button("Release private slot", key=f"hr_release_{application_id}"):
                st.session_state.pop("hr_slot_proposal", None)
                result = _action(local_api(), f"/api/slots/batches/{proposal['proposal_id']}/commit", clear_session,
                                 json={"fingerprint": proposal["fingerprint"]})
                if result is not None:
                    _refresh("Private slot released locally. The candidate can choose it.")
    else:
        st.session_state.pop("hr_slot_proposal", None)
        st.caption("A private slot can be prepared after HR advances the application.")

    slots = call("GET", "/api/slots", optional=True, params={"target_application_id": application_id})
    if slots is not None:
        st.caption("Private slots for this application")
        if not slots:
            st.info("No private slots have been recorded for this application.")
        for slot in slots:
            if slot.get("target_application_id") != application_id:
                st.error("A private slot could not be verified. Reload the page.")
                break
            start_time = _offset_time(slot["start_at"])
            state = ("withdrawn" if slot["withdrawn"] else "booked" if slot["booked"] else
                     "time unavailable" if start_time is None else "expired" if start_time <= datetime.now(UTC) else "unbooked")
            st.text(f"Slot #{slot['id']}: {slot['start_at']} to {slot['end_at']} | {slot['interviewer']} | {state}")
            if status == "advanced" and state == "unbooked":
                with st.form(f"hr_withdraw_{slot['id']}"):
                    confirmed = st.checkbox("Confirm withdrawal of this private slot", key=f"hr_withdraw_confirm_{slot['id']}")
                    withdraw = st.form_submit_button("Withdraw private slot")
                if withdraw:
                    if not confirmed:
                        st.error("Confirm the slot withdrawal first.")
                    else:
                        result = _action(local_api(), f"/api/slots/{slot['id']}/withdraw", clear_session,
                                         params={"expected_application_id": application_id})
                        if result is not None:
                            _refresh("Private slot withdrawn locally. No message was sent.")

    interviews = call("GET", "/api/interviews", optional=True)
    own = None if interviews is None else [item for item in interviews if item["application_id"] == application_id]
    if own is not None:
        if not own:
            st.info("No interviews have been recorded for this application.")
        for interview in own:
            st.text(f"Interview #{interview['id']}: {interview['start_at']} to {interview['end_at']} | {interview['interviewer']} | {interview['status']} | {'completed' if interview['completed'] else 'not confirmed completed'}")
            end_time = _offset_time(interview['end_at'])
            can_complete = (status in ("submitted", "advanced") and interview['status'] == "scheduled"
                            and not interview['completed'] and end_time and end_time <= datetime.now(UTC))
            if can_complete and st.button("Confirm interview took place", key=f"hr_complete_{interview['id']}"):
                result = _action(local_api(), f"/api/interviews/{interview['id']}/complete", clear_session)
                if result is not None:
                    _refresh("Interview completion recorded locally.")

    now = datetime.now(UTC)
    active = own is None or any(item["status"] == "scheduled" and
                                (_offset_time(item["end_at"]) is None or _offset_time(item["end_at"]) > now)
                                for item in own)
    offers = call("GET", f"/api/applications/{application_id}/offers", optional=True)
    if offers is not None:
        if not offers:
            st.info("No offers have been presented for this application.")
        for offer in offers:
            st.text(f"Offer #{offer['id']}: {offer['status']}")
            st.text(offer['details'])
        if status == "advanced":
            st.caption("An offer requires a completed interview and no active interview. The server rechecks both.")
            completed = own is not None and any(item["completed"] for item in own)
            if not completed or active:
                st.info("An offer is unavailable until an interview is confirmed complete and no interview is active.")
                return
            with st.form(f"hr_offer_{application_id}"):
                details = st.text_area("Offer details", max_chars=4000)
                present = st.form_submit_button("Present offer locally")
            if present:
                if not details.strip():
                    st.error("Enter offer details before presenting the offer.")
                else:
                    result = _action(local_api(), f"/api/applications/{application_id}/offers", clear_session,
                                     json={"details": details.strip()})
                    if result is not None:
                        _refresh("Offer presented locally. No message was sent.")

    return bool(status == "offered" and offers is not None and not active
                and any(offer["status"] == "accepted" for offer in offers))
