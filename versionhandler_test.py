import pytest

from versionhandler import extract_version


@pytest.mark.parametrize(
    "version_str, result",
    [
        # Stables
        ("0.23", ("stable", "0.23")),
        ("0.23.5", ("stable", "0.23.5")),
        ("1.23.5.1.3.4", ("stable", "1.23.5.1.3.4")),
        ("foo 1.23.5 bar", ("stable", "1.23.5")),
        ("v123", ("stable", "123")),
        ("123", ("stable", "123")),
        ("1.23f", ("stable", "1.23f")),
        ("1.2-1", ("stable", "1.2-1")),
        ("1.2-12", ("stable", "1.2-12")),
        ("v1.2-12", ("stable", "1.2-12")),
        ("0.23.5-stable", ("stable", "0.23.5")),
        ("version-1.2", ("stable", "1.2")),
        ("version/1.2", ("stable", "1.2")),
        ("releases/1.2", ("stable", "1.2")),
        ("release-1.2", ("stable", "1.2")),
        ("release/1.2", ("stable", "1.2")),
        ("rel/1.2", ("stable", "1.2")),
        ("REL-1.2", ("stable", "1.2")),
        ("vers1.2", ("stable", "1.2")),
        ("v.1.2", ("stable", "1.2")),
        ("3_0_5", ("stable", "3.0.5")),
        ("3-0-5", ("stable", "3.0.5")),
        # Unstables
        ("1.4alpha", ("alpha", "1.4alpha")),
        ("1.3beta", ("beta", "1.3beta")),
        ("1.3pre", ("unstable", "1.3pre")),
        ("1.3-preview1", ("unstable", "1.3-preview1")),
        ("1.3rc", ("rc", "1.3rc")),
        ("1.3rc1", ("rc", "1.3rc1")),
        ("1.4Alpha", ("alpha", "1.4Alpha")),
        ("1.4.beta", ("beta", "1.4.beta")),
        ("1.4-beta", ("beta", "1.4-beta")),
        ("1.4-beta1", ("beta", "1.4-beta1")),
        ("1.4-beta.1", ("beta", "1.4-beta.1")),
        ("1.4-beta-1", ("beta", "1.4-beta-1")),
        ("1.3b1", ("beta", "1.3b1")),
        ("Picard 2.0.0beta3", ("beta", "2.0.0beta3")),
        ("v1.0.0-beta3", ("beta", "1.0.0-beta3")),
        ("9.3.2_RC1", ("rc", "9.3.2_RC1")),
        ("v2.1-rc1", ("rc", "2.1-rc1")),
        ("v4.9.0-RC2", ("rc", "4.9.0-RC2")),
    ],
    ids=lambda x: "|".join(x) if isinstance(x, tuple) else x,
)
def test_version_str(version_str, result):
    assert extract_version(version_str) == result


@pytest.mark.parametrize(
    "version, result",
    [
        (("program 1.2", "program"), ("stable", "1.2")),
        (("program-1.2", "program"), ("stable", "1.2")),
        (("Program-1.2", "program"), ("stable", "1.2")),
        (("program1.2", "program"), ("stable", "1.2")),
        (("program 1.4alpha", "program"), ("alpha", "1.4alpha")),
    ],
    ids="|".join,
)
def test_version_with_name(version, result):
    assert extract_version(*version) == result


@pytest.mark.parametrize(
    "version_str",
    [
        ("foo"),
        ("foo1.3"),
        ("1.3bar"),
        ("1.3beta1.4"),
        ("1.3beta 1.4-stable"),
        ("foo1.3bar"),
        ("1.3 foo 2.3"),
        ("1.2.3-1.3"),
        ("2016-10-12"),
        ("2.1.2017"),
        ("foo 2015 bar"),
        ("foo #871"),
        ("RC1"),
        ("1234567"),
    ],
)
def test_invalid_version_str(version_str):
    assert extract_version(version_str) is None


@pytest.mark.parametrize(
    "version", [("foo1.3bar", "foo"), ("mame0199", "mame")], ids="|".join
)
def test_invalid_version_with_name(version):
    assert extract_version(*version) is None


@pytest.mark.xfail(reason="Not yet supported formats")
@pytest.mark.parametrize(
    "version_str, result",
    [
        ("4.9.0 RC2", ("rc", "4.9.0")),
        ("v3.0.5.RELEASE", ("stable", "3.0.5")),
        ("3.0.5-RELEASE", ("stable", "3.0.5")),
        ("3.0.5.Final", ("stable", "3.0.5")),
        ("v3.0-dev", ("dev", "3.0")),
        ("v3.0.5dev", ("dev", "3.0.5")),
        ("v3-0-5", ("stable", "3.0.5")),
    ],
    ids=lambda x: "|".join(x) if isinstance(x, tuple) else x,
)
def test_not_supported_yet(version_str, result):
    """
    Not yet supported formats

    These asserts will all fail. Improve the versionhandler to support them if
    possible and then move them up to the corresponding test.
    """
    assert extract_version(version_str) == result
