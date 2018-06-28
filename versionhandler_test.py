from versionhandler import extract_version


def test_stable():
    assert extract_version("0.23") == ("stable", "0.23")
    assert extract_version("0.23.5") == ("stable", "0.23.5")
    assert extract_version("1.23.5.1.3.4") == ("stable", "1.23.5.1.3.4")
    assert extract_version("foo 1.23.5 bar") == ("stable", "1.23.5")
    assert extract_version("v123") == ("stable", "123")
    assert extract_version("123") == ("stable", "123")
    assert extract_version("1.23f") == ("stable", "1.23f")
    assert extract_version("1.2-1") == ("stable", "1.2-1")
    assert extract_version("1.2-12") == ("stable", "1.2-12")
    assert extract_version("0.23.5-stable") == ("stable", "0.23.5")
    assert extract_version("program 1.2", "program") == ("stable", "1.2")
    assert extract_version("program-1.2", "program") == ("stable", "1.2")
    assert extract_version("Program-1.2", "program") == ("stable", "1.2")


def test_unstable():
    assert extract_version("1.4alpha") == ("alpha", "1.4alpha")
    assert extract_version("program 1.4alpha", "program") == ("alpha", "1.4alpha")
    assert extract_version("1.3beta") == ("beta", "1.3beta")
    assert extract_version("1.3pre") == ("unstable", "1.3pre")
    assert extract_version("1.3-preview1") == ("unstable", "1.3-preview1")
    assert extract_version("1.3rc") == ("rc", "1.3rc")
    assert extract_version("1.3rc1") == ("rc", "1.3rc1")
    assert extract_version("1.4Alpha") == ("alpha", "1.4Alpha")
    assert extract_version("1.4.beta") == ("beta", "1.4.beta")
    assert extract_version("1.4-beta") == ("beta", "1.4-beta")
    assert extract_version("1.4-beta1") == ("beta", "1.4-beta1")
    assert extract_version("1.4-beta.1") == ("beta", "1.4-beta.1")
    assert extract_version("1.4-beta-1") == ("beta", "1.4-beta-1")
    assert extract_version("1.3b1") == ("beta", "1.3b1")


def test_invalid():
    assert extract_version("foo") is None
    assert extract_version("foo1.3") is None
    assert extract_version("1.3bar") is None
    assert extract_version("1.3beta1.4") is None
    assert extract_version("1.3beta 1.4-stable") is None
    assert extract_version("foo1.3bar") is None
    assert extract_version("foo1.3", "foo") is None
    assert extract_version("foo1.3bar", "foo") is None
    assert extract_version("1.3 foo 2.3") is None
    assert extract_version("1.2.3-1.3") is None
    assert extract_version("2016-10-12") is None
    assert extract_version("2.1.2017") is None
    assert extract_version("foo 2015 bar") is None
    assert extract_version("foo #871") is None
    assert extract_version("RC1") is None
    assert extract_version("1234567") is None
