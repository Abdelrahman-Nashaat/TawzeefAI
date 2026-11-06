"""HR-reviewed graph rejection through the local API."""

import streamlit as st

from streamlit_client import ApiError, ApiUnavailable
from streamlit_graph_policy import show_policy


def _request(api, clear_session, method, path, **kwargs):
    try:
        return api.request(method, path, **kwargs)
    except ApiError as exc:
        if exc.status in (401, 403):
            clear_session()
            st.rerun()
        message = ("The review changed or is no longer available. Start a new review."
                   if exc.status == 409 else
                   "The request could not be confirmed. Check the application before retrying.")
    except ApiUnavailable:
        message = "The request could not be confirmed. Check the application before retrying."
    st.session_state['graph_reject_notice'] = ('warning', message)
    st.warning(message)
    return None


def render_graph_rejection(api, clear_session, application_id, job_id):
    """Re-fetch the authorized preview before every decision; retain no reason in review state."""
    st.subheader('Graph rejection review')
    notice = st.session_state.pop('graph_reject_notice', None)
    if notice:
        (st.success if notice[0] == 'success' else st.warning)(notice[1])
    st.caption('HR reviews the reason and all local effects. No message is sent.')
    review = st.session_state.get('graph_reject_review')
    if review and (review['application_id'] != application_id or review['job_id'] != job_id):
        st.session_state.pop('graph_reject_review', None)
        review = None
    reason_key = f'graph_reject_reason_{application_id}'
    policy_key = f'graph_reject_policy_{application_id}'
    if st.session_state.pop('graph_reject_clear_reason', False):
        st.session_state.pop(reason_key, None)
        st.session_state.pop(policy_key, None)
    if not review:
        st.caption('If an AI provider is enabled, policy questions and authorized passages may be sent to it.')
        with st.form(f'graph_reject_start_{application_id}'):
            reason = st.text_area('Reason for graph rejection', max_chars=1000, key=reason_key)
            question = st.text_area('Optional policy question for graph rejection', max_chars=2000, key=policy_key,
                                    help='When the AI provider is enabled, the question and authorized passages may be sent to it.')
            start = st.form_submit_button('Start graph rejection review')
        if start:
            if not reason.strip():
                st.error('Enter a reason before starting the review.')
            else:
                data = _request(api, clear_session, 'POST', '/api/graph-reviews',
                                json={'application_id': application_id, 'intent': 'reject_application',
                                      'rejection_reason': reason.strip(), 'policy_question': question.strip()})
                st.session_state['graph_reject_clear_reason'] = True
                if data is not None:
                    st.session_state['graph_reject_review'] = {'id': data['id'], 'application_id': application_id,
                                                               'job_id': job_id, 'policy_requested': bool(question.strip())}
                st.rerun()
        return
    data = _request(api, clear_session, 'GET', f"/api/graph-reviews/{review['id']}")
    if data is None:
        st.session_state.pop('graph_reject_review', None)
        return
    state = data.get('state', {})
    proposal = state.get('action_proposal') or {}
    preview = state.get('rejection_preview') or {}
    base = (data.get('awaiting_review') and state.get('intent') == 'reject_application'
            and proposal.get('type') == 'reject_application' and proposal.get('application_id') == application_id)
    valid = (base and preview.get('status') == 'current' and preview.get('job_id') == job_id
             and isinstance(preview.get('candidate_id'), int) and preview['candidate_id'] > 0
             and all(isinstance(preview.get(k), str) and preview[k] for k in
                     ('candidate_name', 'candidate_username', 'job_title', 'reason', 'current_status'))
             and isinstance(preview.get('interviews'), list) and isinstance(preview.get('slots'), list)
             and isinstance(proposal.get('approval_fingerprint'), str) and len(proposal['approval_fingerprint']) == 64)
    stale = base and preview.get('status') == 'stale'
    if not valid and not stale:
        st.warning('This rejection review is unavailable or complete. Start a new review.')
        st.session_state.pop('graph_reject_review', None)
        return
    if valid:
        st.text(f"Application #{application_id} | Candidate #{preview['candidate_id']}: "
                f"{preview['candidate_name']} ({preview['candidate_username']})")
        st.text(f"Role: {preview['job_title']} ({job_id}) | Status: {preview['current_status']}")
        st.text('Reason: ' + preview['reason'])
        if preview.get('offer_id'):
            offer_action = 'will be withdrawn' if preview.get('offer_status') == 'presented' else 'remains in the record'
            st.text(f"Offer #{preview['offer_id']} ({preview.get('offer_status')}) {offer_action}.")
        st.write('Future interviews to cancel:', preview['interviews'])
        st.write('Private slots to withdraw:', preview['slots'])
        policy_current = show_policy(state, review.get('policy_requested', False))
        if not policy_current:
            valid = False
    if valid:
        confirmed = st.checkbox('I reviewed the rejection reason and effects', key=f"graph_reject_confirm_{review['id']}")
        if st.button('Approve graph rejection', key=f"graph_reject_approve_{review['id']}"):
            if not confirmed:
                st.error('Confirm that you reviewed the reason and effects before approval.')
                return
            result = _request(api, clear_session, 'POST', f"/api/graph-reviews/{review['id']}/resume",
                              json={'reviewed': True, 'approval_fingerprint': proposal['approval_fingerprint']})
            st.session_state.pop('graph_reject_review', None)
            if result is not None:
                status = result.get('state', {}).get('action_result', {}).get('status')
                st.session_state['graph_reject_notice'] = (('success', 'Rejection recorded locally. No message was sent.')
                                                           if status == 'rejected' else
                                                           ('warning', 'Check the application before trying again.'))
            st.rerun()
    elif stale:
        st.warning('Rejection context changed. Discard this review and start a new one.')
    if st.button('Discard graph rejection review', key=f"graph_reject_discard_{review['id']}"):
        result = _request(api, clear_session, 'POST', f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': False})
        st.session_state.pop('graph_reject_review', None)
        if result is not None:
            st.session_state['graph_reject_notice'] = ('success', 'Review discarded. No rejection was recorded.')
        st.rerun()
