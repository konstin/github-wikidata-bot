import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


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
    # Remove a prefix of the name of the program if existent
    if name:
        namere = re.compile(r"^" + re.escape(name) + r"[ -_]", re.IGNORECASE)
        match = namere.match(string)
        if match:
            string = string[match.end() :]

    # Check for proper stable versions such as `v1.2.3`
    exact = re.compile(r"[vV]?(\d{1,3}(\.\d{1,3})*)")
    match = exact.fullmatch(string)
    if match:
        return "stable", match.group(1)

    stable = re.compile(r"(\s|^|v)(\d{1,3}(\.\d{1,3})+(-\d\d?|[a-z])?)(\s|$)")
    pre = re.compile(
        r"(\s|^|v)((\d{1,3}(\.\d{1,3})+)[._-]?(alpha|beta|pre|rc|b|preview)[._-]?\d*)(\s|$)",
        re.IGNORECASE,
    )
    explicitstable = re.compile(
        r"(\s|^|v)(\d{1,3}(\.\d{1,3})+)(-stable)(\s|$)", re.IGNORECASE
    )

    match_stable = list(stable.finditer(string))
    match_pre = list(pre.finditer(string))
    match_explicit = list(explicitstable.finditer(string))
    if len(match_stable) + len(match_pre) + len(match_explicit) > 1:
        logger.warning("Multiple versions found for {} in '{}'".format(name, string))
        return None

    if match_stable:
        return "stable", match_stable[0].group(2).strip()

    if match_pre:
        state = re.search(
            r"[^a-zA-Z](alpha|beta|rc|b)($|[^a-zA-Z])", string, re.IGNORECASE
        )
        if state:
            statestr = state.group(1).lower()
            if statestr == "b":
                statestr = "beta"
            return statestr, match_pre[0].group(2).strip()
        else:
            return "unstable", match_pre[0].group(2).strip()

    if match_explicit:
        return "stable", match_explicit[0].group(2).strip()

    return None
