from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from yarl import URL

WIKIDATA_PREFIX = "http://www.wikidata.org/entity/"


@dataclass(frozen=True)
class WikidataProject:
    # Ex) Q42
    q_value: str
    # The wikidata label, english preferred.
    label: str
    repo: GitHubRepo

    @property
    def q_value_url(self) -> str:
        """The full URL. Ex) `http://www.wikidata.org/entity/Q42`."""
        return WIKIDATA_PREFIX + self.q_value

    @classmethod
    def from_sparql(cls, project: dict[str, str]) -> Self:
        url = project["project"]
        if not url.startswith(WIKIDATA_PREFIX):
            raise ValueError(f"Invalid wikidata entity URL: {url}")
        q_value = url.removeprefix(WIKIDATA_PREFIX)
        return cls(
            q_value=q_value,
            label=project["projectLabel"],
            repo=GitHubRepo.from_url(project["repo"]),
        )


@dataclass(frozen=True)
class GitHubRepo:
    """The URL to a github repository."""

    org: str
    project: str

    @classmethod
    def from_url(cls, url: str) -> Self:
        """Parse from a github URL in the form `https://github.com/python/cpython`."""
        parsed = URL(url).with_scheme("https").with_fragment(None)
        if parsed.path.endswith(".git"):
            parsed = parsed.with_path(parsed.path[:-4])
        # remove a trailing slash
        # ok: https://api.github.com/repos/simonmichael/hledger
        # not found: https://api.github.com/repos/simonmichael/hledger/
        # https://www.wikidata.org/wiki/User_talk:Konstin#How_to_run_/_how_often_is_it_run?
        parsed = parsed.with_path(parsed.path.rstrip("/"))

        if parsed.host != "github.com" or parsed.path.count("/") != 2:
            raise ValueError(f"Invalid repo URL: {url}")
        # Ignore the trailing slash at the beginning of the path.
        _, org, project = parsed.path.split("/")
        return cls(org, project)

    def __str__(self) -> str:
        return f"https://github.com/{self.org}/{self.project}"

    def api_base(self) -> str:
        """The base github api URL for the repository."""
        return f"https://api.github.com/repos/{self.org}/{self.project}"

    def api_releases(self) -> str:
        """The github api URL for the releases of the repository."""
        return self.api_base() + "/releases"

    def api_tags(self) -> str:
        """The github api URL for the tags of the repository."""
        return self.api_base() + "/git/refs/tags"
