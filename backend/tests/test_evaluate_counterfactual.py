import json

import pytest

from evaluate_counterfactual import _docx, evaluate, load_cases


def fixture(tmp_path, cases):
    path = tmp_path / 'cases.json'
    path.write_text(json.dumps({'version': 'synthetic-counterfactual-v1', 'cases': cases}), encoding='utf-8')
    return path


def example():
    return {'id': 'collision', 'language': 'en', 'skill': 'Python',
            'identity_lines': ['Python Smith', 'Morgan Smith'], 'body': 'Skills: SQL\nBuilt SQL services.'}


def test_fixture_must_have_valid_unique_pair_ids_and_identity_lines(tmp_path):
    case = example()
    assert load_cases(fixture(tmp_path, [case]))[0]['cases'] == [case]
    for changed in ([case, case], [case | {'identity_lines': ['Same', 'Same']}],
                    [case | {'identity_lines': ['One\nExtra', 'Two']}], []):
        with pytest.raises(ValueError):
            load_cases(fixture(tmp_path, changed))


def test_docx_variants_share_exact_nonidentity_text():
    from io import BytesIO

    from docx import Document

    case = example()
    paragraphs = [[p.text for p in Document(BytesIO(_docx(name, case['body']))).paragraphs]
                  for name in case['identity_lines']]
    assert paragraphs[0][0] != paragraphs[1][0]
    assert paragraphs[0][1:] == paragraphs[1][1:] == case['body'].splitlines()


def test_diagnostic_reports_a_collision_before_and_equal_after_simulated_mask(tmp_path):
    result = evaluate(fixture(tmp_path, [example()]))
    case = result['cases'][0]
    assert result['case_count'] == 1
    assert not case['before_equal']
    assert case['after_reviewed_mask_equal']
    assert case['variants'][0]['before']['score'] == 100
    assert case['variants'][1]['before']['score'] == 0
    assert all(variant['after_reviewed_mask']['score'] == 0 for variant in case['variants'])
    assert all(variant['candidate_hr_endpoints'] == {'screen_status': 403, 'mask_status': 403}
               for variant in case['variants'])
    assert 'not an independent human review' in result['notice']
