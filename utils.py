"""
Extracted from main to avoid importing pywikibot for the tests
"""

import re
from typing import List


def parse_filter_list(text: str) -> List[str]:
    r = re.compile(r"(Q\d+)\s*(#.*)?")
    filterlist = []
    for line in text.split():
        fullmatch = r.fullmatch(line)
        if fullmatch:
            filterlist.append(fullmatch.group(1))
    return filterlist
