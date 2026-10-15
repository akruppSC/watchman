from app.util.strings import collapse_whitespace, truncate


def test_collapse_whitespace():
    assert collapse_whitespace("  a \n b\t c ") == "a b c"


def test_truncate_adds_suffix():
    assert truncate("abcdefghij", 5) == "abcd…"


def test_truncate_leaves_short_text():
    assert truncate("abc", 5) == "abc"
