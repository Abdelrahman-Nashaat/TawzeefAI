import json

from app.knowledge import answer_question


def test_retrieval_normalizes_arabic_articles_and_question_words(tmp_path):
    (tmp_path / 'policies.json').write_text(json.dumps({'documents': [{
        'id': 'staff-policy', 'title': 'إبلاغ الأجهزة', 'audience': 'staff',
        'text': 'يبلغ الموظف تقنية المعلومات والمدير عن تلف جهاز الشركة في يوم العمل نفسه.',
    }]}), encoding='utf-8')
    (tmp_path / 'jobs.json').write_text('{"jobs": []}', encoding='utf-8')
    employee = answer_question('متى أبلغ عن تلف جهاز الشركة؟', 'employee', fixture_dir=tmp_path)
    assert [source.id for source in employee.sources] == ['staff-policy']
    candidate = answer_question('متى أبلغ عن تلف جهاز الشركة؟', 'candidate', fixture_dir=tmp_path)
    assert candidate.mode == 'abstain' and candidate.sources == []


def test_missing_subject_must_not_return_a_general_policy(tmp_path):
    (tmp_path / 'policies.json').write_text(json.dumps({'documents': [{
        'id': 'staff-policy', 'title': 'Annual leave', 'audience': 'staff',
        'text': 'Annual leave needs manager approval.',
    }]}), encoding='utf-8')
    (tmp_path / 'jobs.json').write_text('{"jobs": []}', encoding='utf-8')
    result = answer_question('What is the maternity leave entitlement?', 'employee', fixture_dir=tmp_path)
    assert result.mode == 'abstain' and result.sources == []
