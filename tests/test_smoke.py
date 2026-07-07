import jean


def test_version():
    assert isinstance(jean.__version__, str)
    assert jean.__version__
