import random
import re
from typing import List, Dict

import pywikibot
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache
from cachecontrol.heuristics import ExpiresAfter


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
