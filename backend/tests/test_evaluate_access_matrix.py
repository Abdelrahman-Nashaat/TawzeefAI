"""The diagnostic must report denied leaks and failed positive controls explicitly."""
import httpx

from evaluate_access_matrix import CV_MARKER, evaluate, evaluate_response


def test_status_and_marker_mismatches_do_not_pass():
    assert evaluate_response(httpx.Response(403, text='denied'), 403, CV_MARKER)['status'] == 'pass'
    assert evaluate_response(httpx.Response(200, text='denied'), 403, CV_MARKER)['status'] == 'fail'
    assert evaluate_response(httpx.Response(403, text=CV_MARKER), 403, CV_MARKER)['status'] == 'fail'
    assert evaluate_response(httpx.Response(200, text='not found'), 200, CV_MARKER, True)['status'] == 'fail'
    assert evaluate_response(httpx.Response(503, text='unavailable'), 403)['status'] == 'error'


def test_synthetic_matrix_runs_actual_api_and_positive_controls():
    result = evaluate()
    assert result['counts'] == {'pass': 22, 'fail': 0, 'error': 0}
    checks = {item['id']: item for item in result['checks']}
    assert checks['dashboard-candidate']['http_status'] == 403
    assert checks['candidate-other-application']['http_status'] == 403
    assert checks['candidate-other-chat-denied']['http_status'] == 404
    assert checks['hr-application-positive-control']['marker_present'] is True
    assert checks['policy-candidate']['marker_present'] is False
    assert checks['policy-employee']['marker_present'] is False
    assert checks['policy-hr']['marker_present'] is True
    assert 'do not establish zero leaks' in result['notice']
