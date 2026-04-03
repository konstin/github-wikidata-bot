from __future__ import annotations

import json
import logging.config
import random
import re
import subprocess
import sys
from asyncio import Semaphore
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from subprocess import CalledProcessError
from typing import TYPE_CHECKING, Self

import sentry_sdk
from httpx import AsyncClient

if TYPE_CHECKING:
    from github_wikidata_bot.wikidata_api import WikidataClient

logger = logging.getLogger(__name__)


@dataclass
class Config:
    username: str
    bot_name: str
    password: str
    github_oauth_token: str
    sentry_dsn: str | None = None

    @classmethod
    def load(cls, config_json: Path = Path("config.json")) -> Self:
        """Load config.json."""
        if not config_json.exists():
            print(f"Missing config file at `{config_json.absolute()}`", file=sys.stderr)
            sys.exit(1)
        config = json.loads(config_json.read_text())
        return cls(
            username=config["username"],
            bot_name=config["bot-name"],
            password=config["password"],
            github_oauth_token=config["github-oauth-token"],
            sentry_dsn=config.get("sentry-dsn"),
        )


@lru_cache
def project_root() -> Path:
    """Use the git repository root as project root."""
    return Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
    )


def cache_root() -> Path:
    return project_root().joinpath("cache")


def sparql_dir() -> Path:
    return project_root().joinpath("src").joinpath("sparql")


class NoTracebackFormatter(logging.Formatter):
    """https://stackoverflow.com/a/73695412/3549270"""

    def formatException(self, ei):
        return ""

    def formatStack(self, stack_info):
        return ""


class Session:
    # Do not update wikidata
    dry_run = False
    # Read also tags if a project doesn't use github's releases
    read_tags = True
    normalize_repo_url = True
    denylist_page = "User:Github-wiki-bot/Exceptions"
    allowlist_page = "User:Github-wiki-bot/Whitelist"
    max_releases = 100
    max_tags = 300
    http_timeout = 60
    retries = 3

    # Wikidata API settings
    edit_throttle = 1
    # Higher than the recommendation from https://www.mediawiki.org/wiki/Manual:Maxlag_parameter, we get too timeouts
    # otherwise.
    max_lag = 8
    user_agent = f"github-wikidata-bot/{version('github-wikidata-bot')} (https://github.com/konstin/github-wikidata-bot)"

    denylist: list[str]
    allowlist: list[str]
    licenses: dict[str, str]
    # https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots
    edit_group_hash: str
    edit_summary: str
    github_auth_headers: dict[str, str]
    github_api_limit: Semaphore

    wikidata: WikidataClient
    config: Config

    def __init__(self, config: Config) -> None:
        from github_wikidata_bot.wikidata_api import (
            WikidataClient,
        )  # Local import due to import cycle

        self.config = config
        self.github_auth_headers = {
            "Authorization": f"token {config.github_oauth_token}"
        }
        self.github_api_limit = Semaphore(20)

        # TODO: use async with once the config types layout changes.
        client = AsyncClient(
            timeout=self.http_timeout, headers={"User-Agent": self.user_agent}
        )
        self.wikidata = WikidataClient(
            client=client,
            edit_throttle=self.edit_throttle,
            max_lag=self.max_lag,
            retries=self.retries,
        )

        # https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots
        self.edit_group_hash = f"{random.randrange(0, 2**48):x}"
        self.edit_summary = f"Update with GitHub data ([[:toollabs:editgroups/b/CB/{self.edit_group_hash}|details]])"

        if config.sentry_dsn:
            self.init_sentry(config.sentry_dsn)

    async def connect(self) -> None:
        """Login and fetch initial data from Wikidata."""
        await self.wikidata.login(
            self.config.username, self.config.bot_name, self.config.password
        )

        response = await self.wikidata.sparql_query(
            sparql_dir().joinpath("free_licenses.rq").read_text()
        )
        self.licenses = {row["spdx"]: row["license"][31:] for row in response}

        logger.info("Fetching allow and deny lists")
        self.denylist = await self._get_filter_list(self.denylist_page)
        self.allowlist = await self._get_filter_list(self.allowlist_page)

    @staticmethod
    def init_sentry(dsn: str):
        from github_wikidata_bot.wikidata_api import (
            MaxLagError,
        )  # Local import due to import cycle

        try:
            git_version = (
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
                )
                .strip()
                .decode()
            )
        except CalledProcessError, FileNotFoundError:
            git_version = "unknown"
        release = f"github-wikidata-bot@{git_version}"
        sentry_sdk.init(
            dsn=dsn,
            release=release,
            ignore_errors=[KeyboardInterrupt, MaxLagError],
            traces_sample_rate=1.0,
            profile_session_sample_rate=1.0,
            profile_lifecycle="trace",
        )

    async def _get_filter_list(self, page_title: str) -> list[str]:
        text = await self.wikidata.get_page_text(page_title)
        return parse_filter_list(text)


def parse_filter_list(text: str) -> list[str]:
    r = re.compile(r"(Q\d+)\s*(#.*)?")
    filterlist = []
    for line in text.splitlines():
        line = line.strip()
        fullmatch = r.fullmatch(line)
        if fullmatch:
            filterlist.append(fullmatch.group(1))
    return filterlist
