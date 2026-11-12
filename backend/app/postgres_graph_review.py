"""Read-only LangGraph review over the experimental PostgreSQL demo.

The graph runs in a single request transaction. No graph state is persisted,
and no node can change recruitment state or call an external service.
"""
from typing import Annotated, TypedDict

from fastapi import Depends, Request
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.auth import User
from app.knowledge import Source, _rank_lexical, _tokens
from app.postgres_feedback import current_review
from app.postgres_rubric import _source
from app.resume_structure import structure_resume


class ReviewRequest(BaseModel):
    policy_question: str = Field(default='', max_length=2000)


class ReviewState(TypedDict, total=False):
    parsed: dict
    matching: dict
    status: dict
    scheduling: dict
    policy: dict


def review_graph(db, application_id, question):
    """Build the specialists over the caller's already authorized transaction."""
    row, masked, digest = _source(db, application_id)
    owner = db.execute('SELECT candidate_id FROM applications WHERE id=%s', (application_id,)).fetchone()['candidate_id']

    def parser(_state):
        structured = structure_resume(masked)
        # Section names and counts avoid copying applicant text into graph state.
        sections = structured.get('sections', {})
        return {'parsed': {'sections': {k: {'status': v['status'], 'item_count': len(v['items'])}
                                       for k, v in sections.items()}}}

    def matching(_state):
        assessment = db.execute('''SELECT audit_event_id FROM hr_rubric_history
            WHERE application_id=%s ORDER BY audit_event_id DESC LIMIT 1''', (application_id,)).fetchone()
        bounds = current_review(db, application_id, assessment['audit_event_id'], digest) if assessment else None
        return {'matching': {'status': 'current' if bounds else 'unavailable',
                             'range': list(bounds) if bounds else None}}

    def status(_state):
        interview = db.execute('''SELECT completed FROM interviews WHERE application_id=%s
            AND candidate_id=%s AND status='scheduled' ORDER BY id DESC LIMIT 1''',
            (application_id, owner)).fetchone()
        interview_state = ('completed' if interview['completed'] else 'scheduled') if interview else None
        return {'status': {'application_status': row['status'], 'job_id': row['job_id'],
                           'latest_interview': interview_state}}

    def scheduling(_state):
        # The caller takes the scheduling lock before the application lock.
        booked = db.execute("""SELECT 1 FROM interviews WHERE application_id=%s
            AND status='scheduled' AND NOT completed LIMIT 1""", (application_id,)).fetchone()
        if row['status'] != 'advanced' or booked:
            return {'scheduling': {'slots': [],
                                   'notice': 'No slot can be booked for this application now.'}}
        slots = db.execute('''SELECT id,start_at,end_at,interviewer FROM interview_slots s
            WHERE NOT withdrawn AND start_at>clock_timestamp()
            AND (target_application_id IS NULL OR (target_application_id=%s AND target_candidate_id=%s))
            AND NOT EXISTS (SELECT 1 FROM interviews i WHERE i.status='scheduled'
                AND i.start_at<s.end_at AND i.end_at>s.start_at
                AND (i.slot_id=s.id OR i.candidate_id=%s OR i.interviewer_key=s.interviewer_key))
            ORDER BY start_at,id LIMIT 20''', (application_id, owner, owner)).fetchall()
        return {'scheduling': {'slots': [dict(s) for s in slots],
                               'notice': 'Availability is a preview and may change; no booking is made.'}}

    def policy(_state):
        if not question.strip():
            return {'policy': {'status': 'not_requested', 'sources': []}}
        documents = db.execute("""SELECT id,title,body,page FROM knowledge_documents
            WHERE active AND audience IN ('public','staff','hr') AND collection='handbook'
            ORDER BY id LIMIT 500 FOR SHARE""").fetchall()
        sources = [Source(id=f"pg-doc-{d['id']}", title=d['title'], text=d['body'], page=d['page']) for d in documents]
        ranked = _rank_lexical(_tokens(question), sources)[:3]
        return {'policy': {'status': 'sources' if ranked else 'abstain',
                           'sources': [{'id': s.id, 'title': s.title, 'page': s.page, 'text': s.text} for _, s in ranked],
                           'notice': 'Extractive leads; review the original policy.'}}

    graph = StateGraph(ReviewState)
    for name, node in [('parser', parser), ('matching', matching), ('status', status),
                       ('scheduling', scheduling), ('policy', policy)]:
        graph.add_node(name, node)
    graph.add_edge(START, 'parser')
    graph.add_edge('parser', 'matching')
    graph.add_edge('matching', 'status')
    graph.add_edge('status', 'scheduling')
    graph.add_edge('scheduling', 'policy')
    graph.add_edge('policy', END)
    return graph.compile().invoke({})


def install_routes(app, connect, authenticate, authorize_locked):
    @app.post('/api/applications/{application_id}/graph-review')
    def graph_review(application_id: int, payload: ReviewRequest, request: Request,
                     user: Annotated[User, Depends(authenticate)]):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            # Match the order used by scheduling writes to avoid lock inversion.
            db.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            result = review_graph(db, application_id, payload.policy_question)
            authorize_locked(db, request, user, 'hr')
            return {'application_id': application_id, **result,
                    'notice': 'Read-only local review; HR must make all decisions separately.'}
