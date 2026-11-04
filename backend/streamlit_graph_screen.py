"""Human-reviewed graph screening through the local API."""

import streamlit as st

from streamlit_client import ApiError, ApiUnavailable


def _request(api, clear_session, method, path, **kwargs):
    try:
        return api.request(method, path, **kwargs)
    except ApiError as exc:
        if exc.status in (401, 403):
            clear_session()
            st.rerun()
        if exc.status == 409:
            message = "The review changed or is no longer available. Start a new review."
        else:
            message = "The review could not be confirmed. Check the application before retrying."
    except ApiUnavailable:
        message = "The request could not be confirmed. Check the application before retrying."
    st.session_state['graph_screen_notice'] = ('warning', message)
    st.warning(message)
    return None


def render_graph_screen(api, clear_session, application_id, job_id):
    """Load a fresh authorized preview each rerun; never persist score excerpts."""
    st.subheader("Graph screening review")
    notice = st.session_state.pop('graph_screen_notice', None)
    if notice:
        (st.success if notice[0] == 'success' else st.warning)(notice[1])
    st.caption("Keyword matches are provisional. Approval records screening only; it does not decide or notify.")
    review = st.session_state.get("graph_screen_review")
    if review and (review['application_id'] != application_id or review['job_id'] != job_id):
        st.session_state.pop("graph_screen_review", None)
        review = None
    if st.button("Start graph screening review", key=f"graph_screen_start_{application_id}"):
        st.session_state.pop("graph_screen_review", None)
        review = None
        data = _request(api, clear_session, 'POST', '/api/graph-reviews',
                        json={'application_id': application_id, 'intent': 'screen_application'})
        if data is not None:
            st.session_state['graph_screen_review'] = {'id': data['id'], 'application_id': application_id, 'job_id': job_id}
            st.rerun()
    if not review:
        return
    data = _request(api, clear_session, 'GET', f"/api/graph-reviews/{review['id']}")
    if data is None:
        st.session_state.pop('graph_screen_review', None)
        return
    state = data.get('state', {})
    proposal = state.get('action_proposal') or {}
    preview = state.get('screening_preview') or {}
    valid = (data.get('awaiting_review') and state.get('intent') == 'screen_application'
             and proposal.get('type') == 'screen_application' and proposal.get('application_id') == application_id
             and proposal.get('job_id') == job_id and isinstance(proposal.get('job_title'), str)
             and isinstance(proposal.get('candidate_id'), int) and proposal['candidate_id'] > 0
             and isinstance(proposal.get('approval_fingerprint'), str) and len(proposal['approval_fingerprint']) == 64
             and preview.get('status') == 'current' and isinstance(preview.get('score'), dict))
    if not valid:
        st.warning("This screening review is stale or complete. Start a new review.")
        st.session_state.pop('graph_screen_review', None)
        return
    st.text(f"Application #{application_id} | Candidate #{proposal['candidate_id']} | Role: {proposal['job_title']} ({job_id})")
    st.caption("Review the current masked-source scoring details before approving.")
    st.json(preview['score'])
    confirmed = st.checkbox("I reviewed this graph screening", key=f"graph_screen_confirm_{review['id']}")
    if st.button("Approve and record graph screening", key=f"graph_screen_approve_{review['id']}"):
        if not confirmed:
            st.error("Confirm that you reviewed the screening before approval.")
            return
        result = _request(api, clear_session, 'POST', f"/api/graph-reviews/{review['id']}/resume",
                          json={'reviewed': True, 'approval_fingerprint': proposal['approval_fingerprint']})
        st.session_state.pop('graph_screen_review', None)
        if result is not None:
            if result.get('state', {}).get('action_result', {}).get('status') == 'screened':
                st.session_state['graph_screen_notice'] = ('success', "Screening recorded locally. No decision or message was sent.")
            else:
                st.session_state['graph_screen_notice'] = ('warning', "Check the application before trying again.")
        st.rerun()
