import asyncio
import datetime
import itertools
import logging
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping
from urllib.parse import quote_plus

import pywikibot
import sentry_sdk
from httpx import AsyncClient, HTTPStatusError
from pydantic import BaseModel
from pywikibot import WbTime
from yarl import URL

from .settings import Settings
from .sparql import WikidataProject
from .utils import SimpleSortableVersion
from .versionhandler import extract_version

logger = logging.getLogger(__name__)


@dataclass
class Release:
    version: str
    date: WbTime
    page: str
    release_type: str


@dataclass
class ReleaseTag:
    version: str
    page: str
    release_type: str
    tag_url: str
    tag_type: str
    sha: str


@dataclass
class Project:
    project: str
    stable_release: list[Release]
    website: str | None
    license: str | None
    repo: str
    retrieved: WbTime


class RateLimitError(Exception):
    sleep: float

    def __init__(self, sleep: float):
        self.sleep = sleep


class GitHubRepo:
    """The URL to a github repository."""

    org: str
    project: str

    def __init__(self, url: str):
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
        _, self.org, self.project = parsed.path.split("/")

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


def string_to_wddate(iso_timestamp: str, settings: Settings) -> WbTime:
    """
    Create a wikidata compatible wikibase date from an ISO 8601 timestamp
    """
    date = WbTime.fromTimestr(iso_timestamp, calendarmodel=settings.calendar_model)
    date.hour = 0
    date.minute = 0
    date.second = 0
    date.precision = WbTime.PRECISION["day"]
    return date


@sentry_sdk.trace
async def fetch_json(
    url: str,
    client: AsyncClient,
    settings: Settings,
    caching_headers: dict[str, str] | None = None,
) -> tuple[Any | None, Mapping[str, str]]:
    """Get JSON from an API while handling rate limiting and cache headers.

    Returns `None` instead of the payload if the server returned a 304 Not Modified."""
    if caching_headers is None:
        caching_headers = {}

    response = await client.get(
        url, headers={**settings.github_auth_headers, **caching_headers}
    )

    # We stop before we hit the actual rate limit cause github doesn't seem to like it
    # if we go to zero.
    total_limit = int(response.headers.get("x-ratelimit-limit", "0"))
    remaining_requests = int(response.headers.get("x-ratelimit-remaining", "0"))
    if remaining_requests < total_limit * 0.1 or (
        response.status_code == 403 and remaining_requests == 0
    ):
        reset = response.headers["x-ratelimit-reset"]
        seconds_to_reset = int(reset) - time.time()
        # Sleep a second longer as buffer
        for key, value in response.headers.items():
            if key.startswith("x-ratelimit"):
                logger.info(f"header {key}: {value}")
        raise RateLimitError(seconds_to_reset + 1)

    if response.status_code == 429:
        # We've hit github's abuse limits, wait 5min and try again
        raise RateLimitError(5 * 60)

    if response.status_code == 304:
        logger.info(f"Not modified: {url}")
        return None, response.headers

    # Handle other 4xx and 5xx status codes
    response.raise_for_status()

    if caching_headers:
        logger.info(f"Fresh response: {response.url}")
    else:
        logger.info(f"Received data: {response.url}")

    return response.json(), response.headers


async def fetch_cached(
    api_url: str,
    cache_path: Path,
    client: AsyncClient,
    allow_stale: bool,
    settings: Settings,
) -> Any:
    cache_path.parent.mkdir(exist_ok=True, parents=True)
    if cache_path.exists():
        cached: CachedResponse = CachedResponse.model_validate_json(
            cache_path.read_text()
        )
        if allow_stale:
            logger.info(f"Assumed fresh: {api_url}")
            return cached.payload

        logger.info(f"Revalidating: {api_url}")
        headers = {"If-None-Match": cached.metadata.etag}
        payload, headers = await fetch_json(api_url, client, settings, headers)
        if not payload:
            logger.info(f"Revalidated, no change: {api_url}")
            return cached.payload
    else:
        logger.info(f"No cache: {api_url}")
        headers = {}
        payload, headers = await fetch_json(api_url, client, settings, headers)
        assert payload is not None  # For the type checker

    etag = headers["etag"]
    if etag.startswith("W/"):
        # Bad etag parsing
        etag = etag.removeprefix("W/")
    cached_release: CachedResponse = CachedResponse(
        metadata=CacheMeta(etag=etag), payload=payload
    )
    cache_path.write_text(cached_release.model_dump_json())
    return payload


class CacheMeta(BaseModel):
    etag: str


class CachedResponse(BaseModel):
    metadata: CacheMeta
    payload: Any


@sentry_sdk.trace
async def get_releases(
    repo: GitHubRepo,
    repo_cache_root: Path,
    client: AsyncClient,
    allow_stale: bool,
    settings: Settings,
) -> list[dict[str, Any]]:
    """Gets all pages of the release/tag information"""
    per_page = 100

    releases_cache = repo_cache_root.joinpath(f"releases-{per_page}")
    releases_cache.mkdir(exist_ok=True, parents=True)

    all_releases: list[dict[str, Any]] = []
    for page_number in itertools.count(1):
        page_url = f"{repo.api_releases()}?page={page_number}&per_page={per_page}"
        page_cache = releases_cache.joinpath(f"{page_number}.json")
        if page_cache.exists():
            cached: CachedResponse = CachedResponse.model_validate_json(
                page_cache.read_text()
            )
            if allow_stale:
                logger.info(f"Stale cache: {page_url}")
                all_releases += cached.payload
                # Assumption: github returns 100 entries per page when we request it.
                if len(cached.payload) < per_page:
                    break
                else:
                    continue

            logger.info(f"Revalidating: {page_url}")
            headers = {"If-None-Match": cached.metadata.etag}
            page_releases, headers = await fetch_json(
                page_url, client, settings, headers
            )
            # If the first page matches, assume all other pages are fresh too.
            # It's unlikely that a release older than 100 gets updated, and can save a lot of requests.
            if not page_releases:
                all_releases += cached.payload
                if page_number == 1:
                    allow_stale = True
                # Assumption: github returns 100 entries per page when we request it.
                if len(cached.payload) < per_page:
                    break
                else:
                    continue
        else:
            logger.info(f"No cache: {page_url}")
            headers = {}
            page_releases, headers = await fetch_json(
                page_url, client, settings, headers
            )
            assert page_releases is not None  # For the type checker

        logger.info(f"Fresh response: {page_url}")

        assert isinstance(page_releases, list)  # For the type checker
        all_releases += page_releases

        etag = headers["etag"]
        if etag.startswith("W/"):
            # Bad etag parsing
            etag = etag.removeprefix("W/")
        cached_release = CachedResponse(
            metadata=CacheMeta(etag=etag), payload=page_releases
        )
        page_cache.write_text(cached_release.model_dump_json())

        # Assumption: github returns 100 entries per page when we request it.
        if len(page_releases) < per_page:
            break

    return all_releases


def analyse_release(
    release: dict[str, Any], project_info: dict[str, Any], settings: Settings
) -> Release | None:
    """
    Heuristics to find the version number and according meta-data for a release
    marked with github's release-feature
    """
    project_name = project_info["name"]
    match_tag_name = extract_version(release.get("tag_name") or "", project_name)
    match_name = extract_version(release.get("name") or "", project_name)
    if (
        match_tag_name is not None
        and match_name is not None
        and match_tag_name != match_name
    ):
        logger.debug(
            f"Conflicting versions {match_tag_name} and {match_name} "
            f"for tag {release['tag_name']} and name {release['name']} in {project_name}"
        )
        return None
    elif match_tag_name is not None:
        release_type, version = match_tag_name
        original_version = release["tag_name"]
    elif match_name is not None:
        release_type, version = match_name
        original_version = release["name"]
    else:
        return None

    # Often prereleases aren't marked as such, so we need manually catch those cases
    if not release["prerelease"] and release_type != "stable":
        logger.debug(f"Diverting release type: {original_version}")
        release_type = "unstable"
    elif release["prerelease"] and release_type == "stable":
        release_type = "unstable"

    # Convert github's timestamps to wikidata dates
    date = string_to_wddate(release["published_at"], settings)

    return Release(
        version=version, date=date, page=release["html_url"], release_type=release_type
    )


def analyse_tag(
    release: dict, project_info: dict, invalid_version_strings: list[str]
) -> ReleaseTag | None:
    """
    Heuristics to find the version number and according meta-data for a release
    not marked with github's release-feature but tagged with git.

    Compared to analyse_release this needs an extra API-call which makes this
    function considerably slower.
    """
    project_name = project_info["name"]
    tag_name = release.get("ref", "refs/tags/")[10:]
    match_name = extract_version(tag_name, project_name)
    if match_name is not None:
        release_type, version = match_name
    else:
        invalid_version_strings.append(tag_name)
        return None

    tag_type = release["object"]["type"]
    tag_url = release["object"]["url"]
    sha = release["object"]["sha"]
    html_url = project_info["html_url"] + "/releases/tag/" + quote_plus(tag_name)

    return ReleaseTag(
        version=version,
        page=html_url,
        release_type=release_type,
        tag_type=tag_type,
        tag_url=tag_url,
        sha=sha,
    )


async def get_date_from_tag_details(
    release: ReleaseTag, tag_details: dict[str, Any], settings: Settings
) -> Release | None:
    if release.tag_type == "tag":
        # For some weird reason the api might not always have a date
        if not tag_details["tagger"]["date"]:
            logger.info(f"No tag date for {release.tag_url}")
            return None
        date = string_to_wddate(tag_details["tagger"]["date"], settings)
    elif release.tag_type == "commit":
        if not tag_details["committer"]["date"]:
            logger.info(f"No tag date for {release.tag_url}")
            return None
        date = string_to_wddate(tag_details["committer"]["date"], settings)
    else:
        raise NotImplementedError(f"Unknown type of tag: {release.tag_type}")

    return Release(
        version=release.version,
        release_type=release.release_type,
        date=date,
        page=release.page,
    )


@sentry_sdk.trace
async def get_data_from_github(
    repo: GitHubRepo,
    properties: WikidataProject,
    client: AsyncClient,
    allow_stale: bool,
    settings: Settings,
) -> Project:
    """
    Retrieve the following data from github:
     - website / homepage
     - version number string and release date of all stable releases

    Version marked with github's own release-function are received primarily.
    Only if a project has none releases marked that way this function will fall
    back to parsing the tags of the project.

    All data is preprocessed, i.e. the version numbers are extracted and
    unmarked prereleases are discovered
    """
    # "retrieved" does only accept dates without time, so create a timestamp with no
    # date
    iso_timestamp = pywikibot.Timestamp.now(datetime.UTC).isoformat()
    retrieved = string_to_wddate(iso_timestamp, settings)

    # General project information
    repo_cache_root = Path("cache").joinpath(repo.org).joinpath(repo.project)

    project_info = await fetch_cached(
        repo.api_base(),
        repo_cache_root.joinpath("index.json"),
        client,
        allow_stale,
        settings,
    )

    website = project_info.get("homepage")
    if project_license := project_info.get("license"):
        spdx_id = project_license["spdx_id"]
    else:
        spdx_id = None

    releases = await get_releases(repo, repo_cache_root, client, allow_stale, settings)

    invalid_releases = []
    extracted: list[Release | None] = []
    for release in releases:
        result = analyse_release(release, project_info, settings)
        if result:
            extracted.append(result)
        else:
            invalid_releases.append((release["tag_name"], release["name"]))

    if invalid_releases:
        message = ", ".join(str(i) for i in invalid_releases)
        message = textwrap.shorten(message, width=200, placeholder="...")
        logger.info(f"{len(invalid_releases)} invalid releases: {message}")

    if settings.read_tags and (
        len(extracted) == 0 or properties.wikidata_id in settings.whitelist
    ):
        logger.info("Falling back to tags")
        try:
            cache_file = repo_cache_root.joinpath("tags-index").joinpath("index.json")
            tags = await fetch_cached(
                repo.api_tags(), cache_file, client, allow_stale, settings
            )
        except HTTPStatusError as e:
            # Github raises a 404 if there are no tags
            if e.response.status_code == 404:
                tags = []
            else:
                raise

        invalid_version_strings: list[str] = []
        extracted_tags = [
            analyse_tag(release, project_info, invalid_version_strings)
            for release in tags
        ]
        filtered = [v for v in extracted_tags if v is not None]
        filtered.sort(key=lambda x: SimpleSortableVersion(x.version))
        if len(filtered) > settings.max_tags:
            logger.info(
                f"Limiting {properties.wikidata_id} to {settings.max_tags} of {len(filtered)} tags "
                f"for performance reasons."
            )
            filtered = filtered[-settings.max_tags :]

        # Fetch tags in parallel
        # TODO: Don't use the API, use the git interface instead?
        async def tag_with_limit(tag: ReleaseTag) -> Release | None:
            async with settings.github_api_limit:
                cache_file = repo_cache_root.joinpath("tags-detail").joinpath(
                    f"{tag.sha}.json"
                )
                # Assumption: Tags are immutable, the page never needs to be refreshed.
                tag_details = await fetch_cached(
                    tag.tag_url, cache_file, client, True, settings
                )
                return await get_date_from_tag_details(tag, tag_details, settings)

        extracted = list(
            await asyncio.gather(*[tag_with_limit(tag) for tag in filtered])
        )
        if invalid_version_strings:
            message = ", ".join(invalid_version_strings)
            message = textwrap.shorten(message, width=200, placeholder="...")
            logger.info(
                f"Invalid version strings in tags of {properties.wikidata_id}: {message}"
            )

    stable_release = []
    for extract in extracted:
        if extract and extract.release_type == "stable":
            stable_release.append(extract)

    return Project(
        stable_release=stable_release,
        website=website,
        license=spdx_id,
        retrieved=retrieved,
        repo=properties.repo,
        project=properties.project,
    )
