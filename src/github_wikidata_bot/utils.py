"""
Extracted from main to avoid importing pywikibot for the tests
"""

import functools
import re

from pywikibot.exceptions import APIError


@functools.total_ordering
class SimpleSortableVersion:
    """Sort a version by only the release part, ignoring everything else."""

    version: list[int]

    def __init__(self, version: str):
        loose_version = re.sub(r"[^0-9.]", "", version)
        self.version = [int(x) for x in loose_version.split(".") if x]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SimpleSortableVersion):
            return NotImplemented
        return self.version < other.version

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SimpleSortableVersion):
            return NotImplemented
        return self.version == other.version


def parse_filter_list(text: str) -> list[str]:
    r = re.compile(r"(Q\d+)\s*(#.*)?")
    filterlist = []
    for line in text.split():
        fullmatch = r.fullmatch(line)
        if fullmatch:
            filterlist.append(fullmatch.group(1))
    return filterlist


def is_edit_conflict(error: APIError) -> bool:
    """This is a frequent error for some reason"""
    if messages := error.other.get("messages"):
        for message in messages:
            if message.get("name") == "edit-conflict":
                return True
    return False
