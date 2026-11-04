"""HR-reviewed graph offer through the local API."""

import streamlit as st

from streamlit_client import ApiError, ApiUnavailable


def _request(api, clear_session, method, path, **kwargs):
    try:
        return api.request(method, path, **kwargs)
    except ApiError as exc:
        if exc.status in (401, 403):
            clear_session()
            st.rerun()
        message = ('The review changed or is no longer available. Start a new review.'
                   if exc.status == 409 else
                   'The request could not be confirmed. Check the application before retrying.')
    except ApiUnavailable:
        message = 'The request could not be confirmed. Check the application before retrying.'
    st.session_state['graph_offer_notice'] = ('warning', message)
    st.warning(message)
    return None


def render_graph_offer(api, clear_session, application_id, job_id):
    """Display a fresh authorized preview before recording a local offer."""
    st.subheader('Graph offer review')
    notice = st.session_state.pop('graph_offer_notice', None)
    if notice:
        (st.success if notice[0] == 'success' else st.warning)(notice[1])
    st.caption('Review the candidate, role and offer terms. No message is sent.')
    review = st.session_state.get('graph_offer_review')
    if review and (review['application_id'] != application_id or review['job_id'] != job_id):
        st.session_state.pop('graph_offer_review', None)
        review = None
    details_key = f'graph_offer_details_{application_id}'
    if st.session_state.pop('graph_offer_clear_details', False):
        st.session_state.pop(details_key, None)
    if not review:
        with st.form(f'graph_offer_start_{application_id}'):
            details = st.text_area('Terms for graph offer', max_chars=4000, key=details_key)
            start = st.form_submit_button('Start graph offer review')
        if start:
            if not details.strip():
                st.error('Enter offer terms before starting the review.')
            else:
                data = _request(api, clear_session, 'POST', '/api/graph-reviews',
                                json={'application_id': application_id, 'intent': 'present_offer',
                                      'offer_details': details.strip()})
                st.session_state['graph_offer_clear_details'] = True
                if data is not None:
                    st.session_state['graph_offer_review'] = {'id': data['id'], 'application_id': application_id,
                                                              'job_id': job_id}
                st.rerun()
        return
    data = _request(api, clear_session, 'GET', f"/api/graph-reviews/{review['id']}")
    if data is None:
        st.session_state.pop('graph_offer_review', None)
        return
    state = data.get('state', {})
    proposal = state.get('action_proposal') or {}
    preview = state.get('offer_preview') or {}
    base = (data.get('awaiting_review') and state.get('intent') == 'present_offer'
            and proposal.get('type') == 'present_offer' and proposal.get('application_id') == application_id)
    valid = (base and preview.get('status') == 'current' and preview.get('job_id') == job_id
             and isinstance(preview.get('candidate_id'), int) and preview['candidate_id'] > 0
             and all(isinstance(preview.get(k), str) and preview[k] for k in
                     ('candidate_name', 'candidate_username', 'job_title', 'details'))
             and isinstance(proposal.get('approval_fingerprint'), str) and len(proposal['approval_fingerprint']) == 64)
    stale = base and preview.get('status') == 'stale'
    if not valid and not stale:
        st.warning('This offer review is unavailable or complete. Start a new review.')
        st.session_state.pop('graph_offer_review', None)
        return
    if valid:
        st.text(f"Application #{application_id} | Candidate #{preview['candidate_id']}: "
                f"{preview['candidate_name']} ({preview['candidate_username']})")
        st.text(f"Role: {preview['job_title']} ({job_id})")
        st.text('Offer terms: ' + preview['details'])
        confirmed = st.checkbox('I reviewed the candidate, role and offer terms', key=f"graph_offer_confirm_{review['id']}")
        if st.button('Approve graph offer', key=f"graph_offer_approve_{review['id']}"):
            if not confirmed:
                st.error('Confirm that you reviewed the candidate, role and terms before approval.')
                return
            result = _request(api, clear_session, 'POST', f"/api/graph-reviews/{review['id']}/resume",
                              json={'reviewed': True, 'approval_fingerprint': proposal['approval_fingerprint']})
            st.session_state.pop('graph_offer_review', None)
            if result is not None:
                status = result.get('state', {}).get('action_result', {}).get('status')
                st.session_state['graph_offer_notice'] = (('success', 'Offer presented locally. No message was sent.')
                                                          if status == 'offer_presented' else
                                                          ('warning', 'Check the application before trying again.'))
            st.rerun()
    else:
        st.warning('Offer context changed. Discard this review and start a new one.')
    if st.button('Discard graph offer review', key=f"graph_offer_discard_{review['id']}"):
        result = _request(api, clear_session, 'POST', f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': False})
        st.session_state.pop('graph_offer_review', None)
        if result is not None:
            st.session_state['graph_offer_notice'] = ('success', 'Review discarded. No offer was presented.')
        st.rerun()
