import json

from evaluate_policy_api import evaluate


def test_real_policy_api_diagnostic_maps_sources_and_checks_revocation(tmp_path, monkeypatch):
    section = {'id': 'cap-policy-annual-leave', 'document_type': 'handbook',
               'title': 'Annual leave', 'title_ar': 'الإجازة السنوية', 'audience': 'staff',
               'text_en': 'Annual leave needs manager approval.', 'text_ar': 'تحتاج الإجازة السنوية موافقة المدير.'}
    payloads = {
        'handbook.json': {'sections': [section]},
        'policy-qa.json': {'cases': [{'id': 'positive', 'language': 'en', 'role': 'employee',
                                     'question': 'annual leave', 'expected_section_ids': [section['id']]}]},
        'negative-qa.json': {'cases': [{'id': 'restricted', 'role': 'candidate', 'question': 'annual leave',
                                       'forbidden_section_ids': [section['id']]}]},
    }
    for name, body in payloads.items():
        (tmp_path / name).write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    result = evaluate(tmp_path)
    assert result['summary']['recall_at_3'] == 1
    assert result['summary']['negative_abstained'] == 1
    assert result['summary']['negative_forbidden_returned'] == 0
    assert result['summary']['revoked_source_hidden'] is True
    assert result['settings']['provider'] == 'disabled'
    assert len(result['settings']['sha256']['handbook.json']) == 64
    assert result['retrieval'][0]['mode'] == 'extractive'
    # The diagnostic must not change the user's provider settings afterward.
    import os

    assert os.environ['TAWZEEF_ENABLE_GROQ'] == '1'
