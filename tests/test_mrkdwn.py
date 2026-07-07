from __future__ import annotations

from jean.slack.mrkdwn import chunk_text, md_to_mrkdwn


def test_bold():
    assert md_to_mrkdwn("**important**") == "*important*"
    assert md_to_mrkdwn("__important__") == "*important*"


def test_italic():
    assert md_to_mrkdwn("*subtle*") == "_subtle_"


def test_italic_does_not_eat_bold():
    assert md_to_mrkdwn("**bold** and *italic*") == "*bold* and _italic_"


def test_link():
    assert md_to_mrkdwn("[jean](https://example.com)") == "<https://example.com|jean>"


def test_heading():
    assert md_to_mrkdwn("# Title") == "*Title*"
    assert md_to_mrkdwn("### Sub") == "*Sub*"


def test_bullets():
    md = "- one\n- two\n* three"
    assert md_to_mrkdwn(md) == "• one\n• two\n• three"


def test_strike():
    assert md_to_mrkdwn("~~gone~~") == "~gone~"


def test_code_span_preserved():
    assert md_to_mrkdwn("use `md_to_mrkdwn()` here") == "use `md_to_mrkdwn()` here"


def test_code_span_not_mangled_by_bold_italic_inside():
    assert md_to_mrkdwn("`**not bold**`") == "`**not bold**`"


def test_fenced_code_block_preserved():
    md = "```python\nx = 1\n**not bold**\n```"
    assert md_to_mrkdwn(md) == md


def test_bare_url_autolinked():
    assert md_to_mrkdwn("see https://example.com/x_y*z for details") == (
        "see <https://example.com/x_y*z> for details"
    )


def test_chunk_text_short_text_single_chunk():
    assert chunk_text("hello", max=100) == ["hello"]


def test_chunk_text_splits_long_text():
    text = "a" * 50 + "\n" + "b" * 50 + "\n" + "c" * 50
    chunks = chunk_text(text, max=60)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 60
    # no content lost
    assert "".join(chunks) == text


def test_chunk_text_never_exceeds_max_even_for_single_long_line():
    text = "x" * 250
    chunks = chunk_text(text, max=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text
