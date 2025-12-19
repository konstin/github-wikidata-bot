import json
import logging.config
import random
import re
import subprocess
import sys
from pathlib import Path
from subprocess import CalledProcessError

import pywikibot
import sentry_sdk
from pywikibot.data import sparql

from .utils import parse_filter_list


class NoTracebackFormatter(logging.Formatter):
    """https://stackoverflow.com/a/73695412/3549270"""

    def formatException(self, ei):
        return ""

    def formatStack(self, stack_info):
        return ""


class Settings:
    do_update_wikidata = True

    # Read also tags if a project doesn't use github's releases
    read_tags = True

    normalize_repo_url = True

    blacklist_page = "User:Github-wiki-bot/Exceptions"
    whitelist_page = "User:Github-wiki-bot/Whitelist"
    max_releases = 100
    max_tags = 300
    blacklist: list[str] = []
    whitelist: list[str] = []
    query_projects = Path("src/free_software_items.rq")
    query_versions = Path("src/free_software_versions.rq")

    license_sparql_file = Path("src/free_licenses.rq")
    licenses: dict[str, str] = {}

    # https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots
    edit_group_hash = f"{random.randrange(0, 2**48):x}"
    """https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots"""
    edit_summary = (
        f"Update with GitHub data "
        f"([[:toollabs:editgroups/b/CB/{edit_group_hash}|details]])"
    )

    github_auth_headers: dict[str, str] = {}

    bot = pywikibot.WikidataBot(always=True)
    # pywikibot doesn't cache the calendar model, so let's do this manually
    calendar_model = bot.repo.calendarmodel()

    repo_regex = re.compile(r"^[a-z]+://github.com/[^/]+/[^/]+/?$")

    def __init__(self, github_oauth_token: str | None) -> None:
        config_json = Path("config.json")
        if config_json.exists():
            config = json.loads(config_json.read_text())
        else:
            config = {}

        if github_oauth_token is None:
            github_oauth_token: str | None = config.get("github-oauth-token")

        if github_oauth_token is None:
            print("Please add github-oauth-token to config.json", file=sys.stderr)
            sys.exit(1)
        else:
            self.github_auth_headers = {"Authorization": "token " + github_oauth_token}

        self.init_licenses()
        self.init_filter_lists()

        if dsn := config.get("sentry-dsn"):
            self.init_sentry(dsn)

    @staticmethod
    def init_sentry(dsn: str):
        try:
            version = (
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
                )
                .strip()
                .decode()
            )
        except (CalledProcessError, FileNotFoundError):
            version = "unknown"
        release = "github-wikidata-bot@" + version
        sentry_sdk.init(
            dsn=dsn,
            release=release,
            ignore_errors=[KeyboardInterrupt],
            traces_sample_rate=1.0,
            profiles_sample_rate=1.0,
        )

    def init_licenses(self) -> None:
        response = sparql.SparqlQuery().select(self.license_sparql_file.read_text())
        assert response is not None
        self.licenses = {row["spdx"]: row["license"][31:] for row in response}

    def init_filter_lists(self) -> None:
        self.blacklist = self._get_filter_list(self.blacklist_page)
        self.whitelist = self._get_filter_list(self.whitelist_page)

    @staticmethod
    def _get_filter_list(page_title: str) -> list[str]:
        site = pywikibot.Site()
        page = pywikibot.Page(site, page_title)
        return parse_filter_list(page.text)  # ty: ignore[invalid-argument-type]
