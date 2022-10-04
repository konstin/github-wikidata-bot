import json
import random
import re
from typing import List, Dict

import pywikibot
from pywikibot.data import sparql
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache
from cachecontrol.heuristics import ExpiresAfter

from utils import parse_filter_list


class Settings:
    do_update_wikidata = True

    # Read also tags if a project doesn't use github's releases
    read_tags = True

    normalize_repo_url = True

    blacklist_page = "User:Github-wiki-bot/Exceptions"
    whitelist_page = "User:Github-wiki-bot/Whitelist"
    blacklist: List[str] = []
    whitelist: List[str] = []
    sparql_file = "free_software_items.rq"

    license_sparql_file = "free_licenses.rq"
    licenses: Dict[str, str] = {}

    # https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots
    edit_group_hash = "{:x}".format(random.randrange(0, 2 ** 48))
    """https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots"""
    edit_summary = f"Update with GitHub data ([[:toollabs:editgroups/b/CB/{edit_group_hash}|details]])"

    bot = pywikibot.WikidataBot(always=True)
    # pywikibot doesn't cache the calendar model, so let's do this manually
    calendar_model = bot.repo.calendarmodel()

    repo_regex = re.compile(r"^[a-z]+://github.com/[^/]+/[^/]+/?$")

    cached_session: requests.Session = CacheControl(
        requests.Session(), cache=FileCache("cache"), heuristic=ExpiresAfter(days=30)
    )

    @classmethod
    def init_github(cls, github_oauth_token: str) -> None:
        if not github_oauth_token:
            with open("config.json") as config:
                github_oath_token = json.load(config)["github-oauth-token"]
        cls.cached_session.headers.update(
            {"Authorization": "token " + github_oath_token}
        )

    @classmethod
    def init_licenses(cls) -> None:
        sparql_license_items = "".join(open(cls.license_sparql_file).readlines())
        response = sparql.SparqlQuery().select(sparql_license_items)
        cls.licenses = {row["spdx"]: row["license"][31:] for row in response}

    @classmethod
    def init_filter_lists(cls) -> None:
        cls.blacklist = cls.__get_filter_list(cls.blacklist_page)
        cls.whitelist = cls.__get_filter_list(cls.whitelist_page)

    @staticmethod
    def __get_filter_list(page_title: str) -> List[str]:
        site = pywikibot.Site()
        page = pywikibot.Page(site, page_title)
        return parse_filter_list(page.text)
