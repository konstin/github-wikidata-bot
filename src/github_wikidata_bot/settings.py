from __future__ import annotations

import json
import subprocess
import sys

from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import Self


@dataclass
class Secrets:
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


@dataclass
class Settings:
    # Do not update wikidata
    dry_run = False
    # Read also tags if a project doesn't use github's releases
    read_tags = True
    normalize_repo_url = True
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
    denylist_page = "User:Github-wiki-bot/Exceptions"
    tags_over_releases_page = "User:Github-wiki-bot/Whitelist"
    api_url: str = "https://www.wikidata.org/w/api.php"
    sparql_url: str = "https://query.wikidata.org/sparql"
    api_assert: str | None = "bot"


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
