import asyncio
import datetime
import logging
import textwrap
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import pywikibot
import sentry_sdk
from httpx import AsyncClient, HTTPStatusError
from pywikibot import WbTime

from .settings import Settings
from .sparql import WikidataProject
from .utils import (
    SimpleSortableVersion,
    github_repo_to_api,
    github_repo_to_api_releases,
    github_repo_to_api_tags,
)
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
async def fetch_json(url: str, client: AsyncClient, settings: Settings):
    """Get JSON from an API and cache the result."""
    response = await client.get(url, headers=settings.github_auth_headers)

    if response.extensions.get("hishel_from_cache"):
        if response.extensions.get("hishel_revalidated"):
            logger.info(f"cached, revalidated: {url}")
        else:
            logger.info(f"cached, stale: {url}")
    else:
        if response.extensions.get("hishel_revalidated"):
            logger.info(f"revalidated: {url}")
        else:
            logger.info(f"uncached: {url}")

    if (
        response.status_code == 403
        and response.headers.get("x-ratelimit-remaining") == "0"
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
    response.raise_for_status()
    return response.json()


@sentry_sdk.trace
async def get_all_pages(
    url: str, client: AsyncClient, settings: Settings
) -> list[dict[str, Any]]:
    """Gets all pages of the release/tag information"""
    page_number = 1
    results: list[dict[str, Any]] = []
    while True:
        page = await fetch_json(
            f"{url}?page={page_number}&per_page=100", client, settings
        )
        if not page:
            break
        page_number += 1
        results += page
        # Assumption: github returns 100 entries per page when we request it.
        if len(page) < 100:
            break
    return results


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
    html_url = project_info["html_url"] + "/releases/tag/" + quote_plus(tag_name)

    return ReleaseTag(
        version=version,
        page=html_url,
        release_type=release_type,
        tag_type=tag_type,
        tag_url=tag_url,
    )


async def get_date_from_tag_url(
    release: ReleaseTag, client: AsyncClient, settings: Settings
) -> Release | None:
    tag_details = await fetch_json(release.tag_url, client, settings)
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
    url: str, properties: WikidataProject, client: AsyncClient, settings: Settings
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
    project_info = await fetch_json(github_repo_to_api(url), client, settings)

    website = project_info.get("homepage")
    if project_license := project_info.get("license"):
        spdx_id = project_license["spdx_id"]
    else:
        spdx_id = None

    api_url = github_repo_to_api_releases(url)
    releases = await get_all_pages(api_url, client, settings)

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
        api_url = github_repo_to_api_tags(url)
        try:
            tags = await fetch_json(api_url, client, settings)
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
                return await get_date_from_tag_url(tag, client, settings)

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
