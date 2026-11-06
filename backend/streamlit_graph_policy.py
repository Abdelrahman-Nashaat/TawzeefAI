"""Display policy evidence reconstructed by the authorized graph API."""
import streamlit as st


def show_policy(state, requested):
    """Return whether requested evidence is still available for review."""
    if not requested:
        return True
    policy = state.get('policy') or {}
    if policy.get('status') == 'abstain':
        st.warning('No policy answer was found. Contact HR and review the source before deciding.')
        return True
    sources = policy.get('sources')
    if (policy.get('status') != 'answer' or not isinstance(sources, list) or not sources
            or any(not isinstance(s, dict) or not all(isinstance(s.get(k), str) and s[k]
                    for k in ('id', 'title', 'text')) for s in sources)):
        st.warning('Policy sources changed or are unavailable. Discard this review and start a new one.')
        return False
    st.caption('Policy excerpts are unverified. Review the original source for relevance.')
    for source in sources:
        page = source.get('page')
        st.text(f"{source['title']} | {source['id']}" + (f' | page {page}' if page is not None else ''))
        st.text(source['text'])
    return True
