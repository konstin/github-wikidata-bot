"""
Extracted from main to avoid importing pywikibot for the tests
"""

import functools
import re

from pywikibot.exceptions import APIError
from yarl import URL


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


def github_repo_to_api(raw_url: str) -> str:
    """Converts a GitHub repository url to the api entry with the general information"""
    normalized_url = normalize_url(raw_url)
    path = "/repos" + normalized_url.path
    url = normalized_url.with_host("api.github.com").with_path(path)
    return str(url)


def github_repo_to_api_releases(url: str) -> str:
    """Converts a GitHub repository url to the api entry with the releases"""
    return github_repo_to_api(url) + "/releases"


def github_repo_to_api_tags(url: str) -> str:
    """Converts a GitHub repository url to the api entry with the tags"""
    return github_repo_to_api(url) + "/git/refs/tags"


def normalize_url(raw_url: str) -> URL:
    """Canonical urls be like: https, no slash, no file extension"""
    url = URL(raw_url).with_scheme("https").with_fragment(None)
    if url.path.endswith(".git"):
        url = url.with_path(url.path[:-4])
    # remove a trailing slash
    # ok: https://api.github.com/repos/simonmichael/hledger
    # not found: https://api.github.com/repos/simonmichael/hledger/
    # https://www.wikidata.org/wiki/User_talk:Konstin#How_to_run_/_how_often_is_it_run?
    url = url.with_path(url.path.rstrip("/"))
    return url


def is_edit_conflict(error: APIError) -> bool:
    """This is a frequent error for some reason"""
    if messages := error.other.get("messages"):
        for message in messages:
            if message.get("name") == "edit-conflict":
                return True
    return False
