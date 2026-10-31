"""Offline synthetic diagnostic; never used to authorize or answer questions.

Pairs were authored for this diagnostic, not reviewed by domain experts.
Similarity is not an entailment score; this experiment cannot certify safety.
"""
import json
from pathlib import Path

from app.knowledge import Source, _rank_lexical, _tokens
from app.policy_agent import _qualifiers_match

# Newly authored pairs, not the previous nine reranker development questions.
# Positive means the single passage contains the requested information; a
# negative means that passage alone is not adequate evidence for the question.
PAIRS = [
    ('en-paid', 'What is the annual paid time off allowance?', 'Holiday', 'Employees receive 21 days of paid vacation each year.', True),
    ('en-remote', 'Can employees work from home?', 'Remote work', 'Staff may work remotely two days per week with manager approval.', True),
    ('en-resign', 'How much notice is required to resign?', 'Leaving', 'Employees must give four weeks written notice before leaving employment.', True),
    ('en-trial', 'How long is the trial period?', 'Probation', 'The probationary period lasts three months.', True),
    ('ar-holiday', 'كم يوم إجازة سنوية مدفوعة؟', 'العطلات', 'يحصل الموظف على ٢١ يوم عطلة مدفوعة كل سنة.', True),
    ('ar-remote', 'هل يسمح بالعمل من المنزل؟', 'العمل عن بعد', 'يسمح للموظف بالعمل عن بعد يومين أسبوعيا بموافقة المدير.', True),
    ('en-parental', 'How long is parental leave?', 'Annual leave', 'Employees receive 21 days annual leave each year.', False),
    ('en-unpaid', 'How many days of unpaid leave are available?', 'Paid leave', 'Employees receive 21 paid leave days each year.', False),
    ('en-eligibility', 'Who is eligible for annual leave?', 'Annual leave', 'Annual leave requests are recorded in the HR portal.', False),
    ('en-dismissal', 'How much notice must the employer give for dismissal?', 'Resignation', 'Employees must give four weeks notice before resigning.', False),
    ('en-sick', 'How many sick days are paid?', 'Annual leave', 'Employees receive 21 paid annual leave days.', False),
    ('ar-parental', 'كم مدة إجازة الأبوة؟', 'الإجازة السنوية', 'الإجازة السنوية المدفوعة ٢١ يومًا.', False),
    ('ar-medical', 'كم يوم إجازة مرضية مدفوعة؟', 'العطلة السنوية', 'يحصل الموظف على ٢١ يوم عطلة مدفوعة سنويا.', False),
    ('ar-remote-nopay', 'هل تدفع الشركة بدل الإنترنت للعمل من المنزل؟', 'العمل عن بعد', 'يسمح بالعمل من المنزل يومين في الأسبوع.', False),
]


def evaluate(embedder):
    """Compare current lexical gate to pre-existing 0.85 E5 cutoff.

    No threshold is learned from these pairs. Single-passage similarity tests
    candidate filtering only: neither ranking nor answer quality is measured.
    """
    questions = [case[1] for case in PAIRS]
    passages = [case[3] for case in PAIRS]
    queries = embedder.encode(questions, query=True)
    vectors = embedder.encode(passages, query=False)
    if len(queries) != len(PAIRS) or len(vectors) != len(PAIRS):
        raise ValueError('Embedding count mismatch')
    rows = []
    for case, query, vector in zip(PAIRS, queries, vectors, strict=True):
        identifier, question, title, passage, supported = case
        source = Source(id=identifier, title=title, text=passage)
        terms = _tokens(question)
        lexical = bool(_rank_lexical(terms, [source])) and terms <= _tokens(title + ' ' + passage) and _qualifiers_match(question, source)
        similarity = float(query @ vector)
        if not -1.0001 <= similarity <= 1.0001:
            raise ValueError('Invalid similarity')
        semantic = similarity >= 0.85 and _qualifiers_match(question, source)
        rows.append({'id': identifier, 'supported': supported, 'lexical': lexical,
                     'semantic_candidate': semantic, 'cosine': round(similarity, 6)})
    def counts(field):
        return {'supported_returned': sum(r['supported'] and r[field] for r in rows),
                'supported_abstained': sum(r['supported'] and not r[field] for r in rows),
                'unsupported_returned': sum(not r['supported'] and r[field] for r in rows),
                'unsupported_abstained': sum(not r['supported'] and not r[field] for r in rows)}
    return {'scope': 'Synthetic unreviewed single-passage diagnostic; not an independent human benchmark or proof of support.',
            'threshold': 0.85, 'lexical': counts('lexical'), 'semantic_candidate': counts('semantic_candidate'), 'cases': rows}


def main():
    from app.semantic_experiment import LocalE5

    model = Path(__file__).resolve().parents[2] / '.tooling' / 'multilingual-e5-small'
    print(json.dumps(evaluate(LocalE5(model)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
