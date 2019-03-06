import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def number_of_unique_values(values: [str]) -> int:
    """
    Count number of unique strings in list, ignoring the case
    """
    return len(set(map(lambda s: s.lower(), values)))


def extract_version(
    string: str, name: Optional[str] = None
) -> Optional[Tuple[str, str]]:
    """
    Heuristic to extract a version-number from a string.

    See test file for supported formats. Returns None if no unambiguously
    version number could be found.

    :param string: the string to search
    :param name: the name of the program
    :return: None or a tuple of two strings:
             - type of version ("stable", "beta", "alpha", "rc" or "unstable")
             - version number
    """
    string = string.strip()
    VALID_TYPES = ["stable", "beta", "alpha", "rc", "unstable"]
    versiontype = None
    extracted_version = None

    # Remove a prefix of the name of the program if existent
    if name:
        namere = re.compile(r"^" + re.escape(name) + r"[ _/-]?", re.IGNORECASE)
        string = re.sub(namere, "", string)

    # Remove common prefixes/postfixes
    string = re.sub(
        r"^(releases|release|rel|version|vers|v\.)[ _/-]?",
        "",
        string,
        flags=re.IGNORECASE,
    )
    string = re.sub(r"^(v|r)(?<![0-9])", "", string, flags=re.IGNORECASE)
    string = re.sub(
        r"(^|[._ -])(final|release)([._ -]|$)", " ", string, flags=re.IGNORECASE
    )

    # Replace underscore/hyphen with dots if only underscores/hyphens are used
    if re.fullmatch(r"[0-9_]*", string):
        string = string.replace("_", ".")
    if re.fullmatch(r"[0-9-]*", string):
        string = string.replace("-", ".")

    # Detect type of version
    words = ["stable", "beta", "alpha", "rc", "pre", "preview", "b\d", "dev"]
    res = re.findall(r"(" + "|".join(words) + r")", string, re.IGNORECASE)
    if number_of_unique_values(res) == 1:
        versiontype = res[0].lower()
        if versiontype[0] == "b":
            versiontype = "beta"
        if versiontype not in VALID_TYPES:
            versiontype = "unstable"
    elif number_of_unique_values(res) > 1:
        return None

    # Detect version string
    gen = re.compile(
        r"((?<=\s)|^)(\d{1,3}(\.\d{1,3})+[a-z]?([._ -]?(alpha|beta|pre|rc|b|stable|preview|dev)[._-]?\d*|-\d+)?)(\s|$)",
        re.IGNORECASE,
    )
    res = gen.findall(string)
    # remove "stable" from version string
    res = list(map(lambda s: re.sub(r"[._-]stable[._-]?", "", s[1]), res))
    if number_of_unique_values(res) == 1:
        extracted_version = res[0]
    else:
        # If the string contains nothing but a version-number we are more gratefully with what we accept
        full = re.compile(r"[1-9]\d{0,4}", re.IGNORECASE)
        if full.fullmatch(string):
            extracted_version = string

    if extracted_version is not None:
        # if we don't find any indication about the state of the version,
        # we assume it's a stable version
        if versiontype is None:
            versiontype = "stable"
        return (versiontype, extracted_version)

    return None
