import json
import logging
import logging.config
import random
import re
from pathlib import Path
import sys
from typing import List, Dict

import pywikibot
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache
from cachecontrol.heuristics import ExpiresAfter
from pywikibot.data import sparql

from .utils import parse_filter_list


class Settings:
    do_update_wikidata = True

    # Read also tags if a project doesn't use github's releases
    read_tags = True

    normalize_repo_url = True

    blacklist_page = "User:Github-wiki-bot/Exceptions"
    whitelist_page = "User:Github-wiki-bot/Whitelist"
    blacklist: List[str] = []
    whitelist: List[str] = []
    sparql_file = Path("free_software_items.rq")

    license_sparql_file = Path("free_licenses.rq")
    licenses: Dict[str, str] = {}

    # https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots
    edit_group_hash = f"{random.randrange(0, 2**48):x}"
    """https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots"""
    edit_summary = (
        f"Update with GitHub data "
        f"([[:toollabs:editgroups/b/CB/{edit_group_hash}|details]])"
    )

    bot = pywikibot.WikidataBot(always=True)
    # pywikibot doesn't cache the calendar model, so let's do this manually
    calendar_model = bot.repo.calendarmodel()

    repo_regex = re.compile(r"^[a-z]+://github.com/[^/]+/[^/]+/?$")

    cached_session: requests.Session = CacheControl(
        requests.Session(), cache=FileCache("cache"), heuristic=ExpiresAfter(days=30)
    )

    @staticmethod
    def init_logging(quiet: bool, http_debug: bool) -> None:
        """
        In cron jobs you do not want logging to stdout / stderr,
        therefore the quiet option allows disabling that.
        """
        if quiet:
            handlers = ["all", "error"]
        else:
            handlers = ["console", "all", "error"]

        conf = {
            "version": 1,
            "formatters": {"extended": {"format": "%(levelname)-8s %(message)s"}},
            "handlers": {
                "console": {"class": "logging.StreamHandler"},
                "all": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": "all.log",
                    "formatter": "extended",
                    "maxBytes": 8 * 1024 * 1024,
                    "backupCount": 2,
                },
                "error": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": "error.log",
                    "formatter": "extended",
                    "level": "WARN",
                    "maxBytes": 8 * 1024 * 1024,
                    "backupCount": 2,
                },
            },
            "loggers": {"github_wikidata_bot": {"handlers": handlers, "level": "INFO"}},
        }

        logging.config.dictConfig(conf)

        if http_debug:
            from http.client import HTTPConnection

            HTTPConnection.debuglevel = 1

            requests_log = logging.getLogger("urllib3")
            requests_log.setLevel(logging.DEBUG)
            requests_log.propagate = True

    @classmethod
    def init_github(cls, github_oauth_token: str) -> None:
        if not github_oauth_token:
            try:
                github_oauth_token = json.loads(Path("config.json").read_text())[
                    "github-oauth-token"
                ]
            except FileNotFoundError:
                print("Please create a config.json", file=sys.stderr)
                sys.exit(1)
        cls.cached_session.headers.update(
            {"Authorization": "token " + github_oauth_token}
        )

    @classmethod
    def init_licenses(cls) -> None:
        response = sparql.SparqlQuery().select(cls.license_sparql_file.read_text())
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
