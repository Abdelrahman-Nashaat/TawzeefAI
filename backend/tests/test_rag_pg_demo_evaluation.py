from app.rag_pg_demo_evaluation import analyze, summarize


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def test_reports_distinct_excerpts_suggestions_and_abstentions():
    docs = [{'title': 'Policy', 'active': True, 'audience': 'public', 'pages': ['Wrong page', 'Approved vacation.']},
            {'title': 'Private', 'active': True, 'audience': 'hr', 'pages': ['Private information.']}]
    case = {'id': 'a', 'role': 'candidate', 'language': 'en', 'expected': {'title': 'Policy', 'page': 2}}
    source = {'title': 'Policy', 'page': 2, 'text': 'Approved vacation.'}
    result = analyze(case, 'hybrid', Response({'mode': 'abstain', 'sources': [], 'suggestions': [source]}), docs, 0.5)
    assert result['abstained'] and result['expected_in_suggestions'] and not result['expected_in_excerpts']
    assert not result['citation_violation'] and not result['authorization_violation']
    bad = analyze(case, 'hybrid', Response({'mode': 'extractive', 'sources': [
        {'title': 'Private', 'page': 1, 'text': 'Private information.'},
        {'title': 'Policy', 'page': 1, 'text': 'Approved vacation.'}]}), docs, 0.1)
    assert bad['authorization_violation'] and bad['citation_violation']
    report = summarize([result, bad])['hybrid']
    assert report['answerable_cases'] == 2 and report['expected_in_suggestions'] == 1
    assert report['authorization_violations'] == 1 and report['citation_violations'] == 1


def test_http_errors_do_not_count_as_successful_abstentions():
    case = {'id': 'x', 'role': 'candidate', 'language': 'en', 'expected': None}
    result = analyze(case, 'lexical', Response({}, 503), [], 0.1)
    report = summarize([result])['lexical']
    assert report['http_errors'] == 1 and report['unsupported_abstentions'] == 0


def test_restricted_passage_in_model_input_is_a_violation():
    case = {'id': 'x', 'role': 'candidate', 'language': 'en', 'expected': None}
    docs = [{'title': 'Private', 'active': True, 'audience': 'hr', 'pages': ['Restricted content.']}]
    result = analyze(case, 'hybrid', Response({'mode': 'abstain', 'sources': []}), docs, 0.1,
                     ['Restricted content.'])
    assert result['authorization_violation']


def test_restricted_chunk_of_long_page_in_model_input_is_a_violation():
    from app.knowledge import _document_passages

    page = 'Restricted content. ' * 300
    docs = [{'title': 'Private', 'active': True, 'audience': 'hr', 'pages': [page]}]
    chunk = _document_passages({'id': 1, 'title': 'Private', 'text': page})[0].text
    assert chunk != page
    case = {'id': 'x', 'role': 'candidate', 'language': 'en', 'expected': None}
    result = analyze(case, 'hybrid', Response({'mode': 'abstain', 'sources': []}), docs, 0.1, [chunk])
    assert result['authorization_violation']
