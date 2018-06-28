import re


def extract_version(string, name=""):
    if type(string) is not str:
        return None
    name += " "
    if string.startswith(name):
        string = string[len(name):]
    STABLE = "stable"
    UNSTABLE = "unstable"
    exact = re.compile(r"[vV]?(\d{1,5}(\.\d{1,3})*)")
    stable = re.compile(r"(\s|^|v)\d{1,3}(\.\d{1,3})+(-\d\d?|[a-z])?(\s|$)")
    pre = re.compile(r"(\s|^|v)(\d{1,3}(\.\d{1,3})+)[.-]?(alpha|beta|pre|rc|b)[.-]?\d*(\s|$)",
                     re.IGNORECASE)

    match = exact.fullmatch(string)
    if match:
        return (STABLE, match.group(1))

    match = list(stable.finditer(string))
    if len(match) == 1:
        return (STABLE, match[0].group(0).strip())

    match = list(pre.finditer(string))
    if len(match) == 1:
        state = re.search(
            r"[^a-zA-Z](alpha|beta|rc|b)($|[^a-zA-Z])", string, re.IGNORECASE)
        if state:
            statestr = state.group(1).lower()
            if statestr == 'b':
                statestr = 'beta'
            return (statestr, match[0].group(0))
        else:
            return (UNSTABLE, match[0].group(0))

    return None
