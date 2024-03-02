"""
Extracted from main to avoid importing pywikibot for the tests
"""

import re

from pywikibot.exceptions import APIError
from yarl import URL


def parse_filter_list(text: str) -> list[str]:
    r = re.compile(r"(Q\d+)\s*(#.*)?")
    filterlist = []
    for line in text.split():
        fullmatch = r.fullmatch(line)
        if fullmatch:
            filterlist.append(fullmatch.group(1))
    return filterlist


def github_repo_to_api(url: str) -> str:
    """Converts a GitHub repository url to the api entry with the general information"""
    url = normalize_url(url)
    url = url.with_host("api.github.com").with_path("/repos" + url.path)
    return str(url)


def github_repo_to_api_releases(url: str) -> str:
    """Converts a GitHub repository url to the api entry with the releases"""
    url = github_repo_to_api(url)
    url += "/releases"
    return url


def github_repo_to_api_tags(url: str) -> str:
    """Converts a GitHub repository url to the api entry with the tags"""
    url = github_repo_to_api(url)
    url += "/git/refs/tags"
    return url


def normalize_url(url: str) -> URL:
    """Canonical urls be like: https, no slash, no file extension"""
    url = URL(url).with_scheme("https").with_fragment(None)
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
