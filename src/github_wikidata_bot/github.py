from __future__ import annotations

import asyncio
import datetime
import logging
import textwrap
import time
from asyncio import Semaphore
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import sentry_sdk
from httpx import AsyncClient, HTTPStatusError
from pydantic import BaseModel

from github_wikidata_bot.project import GitHubRepo, WikidataProject
from github_wikidata_bot.settings import Secrets, Settings, cache_root
from github_wikidata_bot.version import SimpleSortableVersion, extract_version

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    sleep: float

    def __init__(self, sleep: float):
        self.sleep = sleep


class GitHubClient:
    auth_headers: dict[str, str]
    api_concurrency: Semaphore
    client: AsyncClient

    def __init__(self, secrets: Secrets, client: AsyncClient):
        self.auth_headers = {"Authorization": f"token {secrets.github_oauth_token}"}
        self.api_concurrency = Semaphore(20)
        self.client = client

    @sentry_sdk.trace
    async def fetch_json(
        self, url: str, caching_headers: dict[str, str] | None = None
    ) -> tuple[Any | None, Mapping[str, str], str]:
        """Get JSON from an API while handling rate limiting and cache headers.

        Returns `(payload, headers, response_url)`. `payload` is `None` if the
        server returned 304 Not Modified.
        """
        if caching_headers is None:
            caching_headers = {}

        response = await self.client.get(
            url, headers={**self.auth_headers, **caching_headers}
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
            return None, response.headers, str(response.url)

        # Handle other 4xx and 5xx status codes
        response.raise_for_status()

        if caching_headers:
            logger.info(f"Fresh response: {response.url}")
        else:
            logger.info(f"Fetched: {response.url}")

        return response.json(), response.headers, str(response.url)


@dataclass
class Release:
    version: str
    timestamp: datetime.datetime
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
    wikidata: WikidataProject
    stable_release: list[Release]
    website: str | None
    license: str | None
    retrieved: datetime.datetime
    # The repo from the response url, to track renames (through redirects).
    canonical_repo: GitHubRepo | None = None


async def fetch_cached(
    api_url: str, cache_path: Path, client: GitHubClient, allow_stale: bool
) -> tuple[Any, str]:
    """Fetch JSON with caching. Returns `(payload, response_url)`."""
    cache_path.parent.mkdir(exist_ok=True, parents=True)
    if cache_path.exists():
        cached: CachedResponse = CachedResponse.model_validate_json(
            cache_path.read_text()
        )
        if allow_stale:
            logger.info(f"Assumed fresh: {api_url}")
            return cached.payload, cached.metadata.response_url or api_url

        logger.info(f"Revalidating: {api_url}")
        headers = {"If-None-Match": cached.metadata.etag}
        payload, headers, response_url = await client.fetch_json(api_url, headers)
        if payload is None:
            logger.info(f"Revalidated, not modified: {api_url}")
            return cached.payload, response_url
    else:
        logger.info(f"No cache: {api_url}")
        headers = {}
        payload, headers, response_url = await client.fetch_json(api_url, headers)
        assert payload is not None  # For the type checker

    etag = headers["etag"]
    if etag.startswith("W/"):
        # Bad etag parsing
        etag = etag.removeprefix("W/")
    cached_release: CachedResponse = CachedResponse(
        metadata=CacheMeta(etag=etag, response_url=response_url), payload=payload
    )
    cache_path.write_text(cached_release.model_dump_json())
    return payload, response_url


class CacheMeta(BaseModel):
    etag: str
    # The final URL after redirects, if different from the request URL.
    response_url: str | None = None


class CachedResponse(BaseModel):
    metadata: CacheMeta
    payload: Any


@sentry_sdk.trace
async def get_releases(
    repo: GitHubRepo, repo_cache_root: Path, client: GitHubClient, allow_stale: bool
) -> list[dict[str, Any]]:
    """Gets all pages of the release/tag information"""
    per_page = 100

    releases_cache = repo_cache_root.joinpath(f"releases-{per_page}")
    releases_cache.mkdir(exist_ok=True, parents=True)

    # GitHub API returns at most 1000 results (100 per page * 10 pages).
    max_pages = 1000 // per_page
    all_releases: list[dict[str, Any]] = []
    for page_number in range(1, max_pages + 1):
        page_url = f"{repo.api_releases()}?page={page_number}&per_page={per_page}"
        page_cache = releases_cache.joinpath(f"{page_number}.json")
        if page_cache.exists():
            cached: CachedResponse = CachedResponse.model_validate_json(
                page_cache.read_text()
            )
            if allow_stale:
                logger.info(f"Cache unchecked: {page_url}")
                all_releases += cached.payload
                # Assumption: github returns 100 entries per page when we request it.
                if len(cached.payload) < per_page:
                    break
                else:
                    continue

            logger.info(f"Revalidating: {page_url}")
            headers = {"If-None-Match": cached.metadata.etag}
            page_releases, headers, _url = await client.fetch_json(page_url, headers)
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
            page_releases, headers, _url = await client.fetch_json(page_url, headers)
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
    release: dict[str, Any], project_name: str | None
) -> Release | None:
    """
    Heuristics to find the version number and according metadata for a release
    marked with github's release-feature
    """
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

    timestamp = datetime.datetime.fromisoformat(release["published_at"])

    return Release(
        version=version,
        timestamp=timestamp,
        page=release["html_url"],
        release_type=release_type,
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
    release: ReleaseTag, tag_details: dict[str, Any]
) -> Release | None:
    if release.tag_type == "tag":
        # For some weird reason the api might not always have a date
        if not tag_details["tagger"]["date"]:
            logger.info(f"No tag date for {release.tag_url}")
            return None
        timestamp = tag_details["tagger"]["date"]
        date = datetime.datetime.fromisoformat(timestamp)
    elif release.tag_type == "commit":
        if not tag_details["committer"]["date"]:
            logger.info(f"No tag date for {release.tag_url}")
            return None
        timestamp = tag_details["committer"]["date"]
        date = datetime.datetime.fromisoformat(timestamp)
    else:
        raise NotImplementedError(f"Unknown type of tag: {release.tag_type}")

    return Release(
        version=release.version,
        release_type=release.release_type,
        timestamp=date,
        page=release.page,
    )


@sentry_sdk.trace
async def get_data_from_github(
    project: WikidataProject,
    allow_stale: bool,
    client: GitHubClient,
    settings: Settings,
    # This is data from wikidata
    tags_over_releases: list[str],
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
    # For the sources of the wikidata claims.
    retrieved = datetime.datetime.now(datetime.UTC)

    repo_cache_root = (
        cache_root().joinpath(project.repo.org).joinpath(project.repo.project)
    )

    # General project information
    api_url = project.repo.api_base()
    project_info, response_url = await fetch_cached(
        api_url, repo_cache_root.joinpath("index.json"), client, allow_stale
    )

    website = project_info.get("homepage")
    if project_license := project_info.get("license"):
        spdx_id = project_license["spdx_id"]
    else:
        spdx_id = None

    # Detect repo renames. We need to use the response body as the redirect goes to
    # `https://api.github.com/repositories/<id>`.
    if response_url != api_url:
        canonical_repo = GitHubRepo(
            project_info["owner"]["login"], project_info["name"]
        )
        logger.info(f"Repo renamed: {project.repo} -> {canonical_repo}")
    else:
        canonical_repo = None

    releases = await get_releases(project.repo, repo_cache_root, client, allow_stale)

    invalid_releases = []
    extracted: list[Release | None] = []
    for release in releases:
        result = analyse_release(release, project_info["name"])
        if result:
            extracted.append(result)
        else:
            invalid_releases.append((release["tag_name"], release["name"]))

    if invalid_releases:
        message = ", ".join(str(i) for i in invalid_releases)
        message = textwrap.shorten(message, width=200, placeholder="...")
        logger.info(f"{len(invalid_releases)} invalid releases: {message}")

    if settings.read_tags and (
        len(extracted) == 0 or project.q_value in tags_over_releases
    ):
        logger.info("Falling back to tags")
        try:
            cache_file = repo_cache_root.joinpath("tags-index").joinpath("index.json")
            tags, _tags_url = await fetch_cached(
                project.repo.api_tags(), cache_file, client, allow_stale
            )
        except HTTPStatusError as e:
            # GitHub raises 404 if there are no tags, 409 for empty repos
            if e.response.status_code in (404, 409):
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
                f"Limiting {project.q_value} to {settings.max_tags} of {len(filtered)} tags "
                f"for performance reasons."
            )
            filtered = filtered[-settings.max_tags :]

        # Fetch tags in parallel
        # TODO: Don't use the API, use the git interface instead?
        async def tag_with_limit(tag: ReleaseTag) -> Release | None:
            async with client.api_concurrency:
                cache_file = repo_cache_root.joinpath("tags-detail").joinpath(
                    f"{tag.sha}.json"
                )
                # Assumption: Tags are immutable, the page never needs to be refreshed.
                tag_details, _tag_url = await fetch_cached(
                    tag.tag_url, cache_file, client, True
                )
                return await get_date_from_tag_details(tag, tag_details)

        extracted = list(
            await asyncio.gather(*[tag_with_limit(tag) for tag in filtered])
        )
        if invalid_version_strings:
            message = ", ".join(invalid_version_strings)
            message = textwrap.shorten(message, width=200, placeholder="...")
            logger.info(f"Invalid version strings in tags of {project.repo}: {message}")

    stable_release = []
    for extract in extracted:
        if extract and extract.release_type == "stable":
            stable_release.append(extract)

    return Project(
        wikidata=project,
        stable_release=stable_release,
        website=website,
        license=spdx_id,
        retrieved=retrieved,
        canonical_repo=canonical_repo,
    )
