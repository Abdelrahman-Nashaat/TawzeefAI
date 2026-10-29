from app.blind import redact_resume


def test_contact_fields_and_inline_pii_do_not_become_skill_evidence():
    result = redact_resume(
        'Name: Python SQL\nEmail: python.sql@example.com\n'
        'Phone: +20 100 123 4567\nBuilt Python services and SQL reports.'
    )
    assert 'Name: Python SQL' not in result.text
    assert 'python.sql@example.com' not in result.text
    assert '+20 100' not in result.text
    assert 'Built Python services and SQL reports.' in result.text
    assert result.masked and result.requires_review


def test_arabic_fields_and_embedded_email_and_link():
    result = redact_resume(
        'الاسم: أحمد\nالنوع: ذكر\nالمحافظة: القاهرة\nالعمر: ٢٦\n'
        'الحالة الاجتماعية: أعزب\nالجامعة: مثال\n'
        'Python للتواصل python.sql@example.com وموقع https://example.com/user'
    )
    assert all(value not in result.text for value in (
        'أحمد', 'ذكر', 'القاهرة', '٢٦', 'أعزب', 'مثال', 'python.sql@example.com', 'https://example.com'
    ))
    assert 'Python للتواصل' in result.text
    assert result.requires_review


def test_unlabelled_names_are_not_claimed_to_be_removed():
    result = redact_resume('Amira Example\nBuilt Python APIs from 2018 to 2023.')
    assert result.requires_review and 'incomplete' in result.notice
    assert '2018 to 2023' in result.text


def test_phone_in_text_is_masked_without_removing_skill_line():
    result = redact_resume('Python developer; call +20 100 123 4567 for details')
    assert '+20 100 123 4567' not in result.text
    assert 'Python developer' in result.text
