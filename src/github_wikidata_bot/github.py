from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import textwrap
import time
from asyncio import Semaphore
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import sentry_sdk
from httpx import (
    AsyncClient,
    HTTPError,
    HTTPStatusError,
    NetworkError,
    RemoteProtocolError,
    Response,
    TimeoutException,
)
from pydantic import BaseModel, ValidationError

from github_wikidata_bot.project import GitHubRepo, WikidataProject
from github_wikidata_bot.settings import Secrets, Settings, cache_root
from github_wikidata_bot.version import SimpleSortableVersion, extract_version

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    sleep: float

    def __init__(self, sleep: float):
        self.sleep = sleep


class RepositoryUnavailableError(HTTPStatusError):
    """GitHub has blocked access to the repository."""


@dataclass(frozen=True)
class RequestKey:
    url: str
    headers: frozenset[tuple[str, str]]


class GitHubClient:
    auth_headers: dict[str, str]
    api_concurrency: Semaphore
    client: AsyncClient
    settings: Settings

    def __init__(self, secrets: Secrets, client: AsyncClient, settings: Settings):
        self.auth_headers = {"Authorization": f"token {secrets.github_oauth_token}"}
        self.api_concurrency = Semaphore(20)
        self.client = client
        self.settings = settings

    @sentry_sdk.trace
    async def fetch_json(
        self,
        url: str,
        caching_headers: dict[str, str] | None = None,
        *,
        query: str | None = None,
    ) -> tuple[Any | None, Mapping[str, str], str]:
        """Fetch REST JSON or a GraphQL query with retries and rate-limit handling.

        Returns `(payload, headers, response_url)`. `payload` is `None` if the
        server returned 304 Not Modified.
        """
        response = await self._fetch_response(url, caching_headers, query=query)
        payload = None
        if response.status_code != 304:
            payload = response.json()
        return payload, response.headers, str(response.url)

    async def _fetch_response(
        self,
        url: str,
        caching_headers: dict[str, str] | None = None,
        *,
        query: str | None = None,
    ) -> Response:
        if caching_headers is None:
            caching_headers = {}

        for attempt in range(self.settings.retries):
            try:
                if query is None:
                    response = await self.client.get(
                        url, headers={**self.auth_headers, **caching_headers}
                    )
                else:
                    response = await self.client.post(
                        url, headers=self.auth_headers, json={"query": query}
                    )
            except (NetworkError, RemoteProtocolError, TimeoutException) as err:
                if attempt == self.settings.retries - 1:
                    raise
                backoff = 2**attempt
                logger.warning(
                    f"GitHub {type(err).__name__} for {url}, "
                    f"retrying after {backoff}s: {err}"
                )
                await asyncio.sleep(backoff)
                continue
            if (
                500 <= response.status_code < 600
                and attempt < self.settings.retries - 1
            ):
                backoff = 2**attempt
                logger.warning(
                    f"GitHub {response.status_code} for {url}, "
                    f"retrying after {backoff}s"
                )
                await asyncio.sleep(backoff)
                continue
            break
        else:
            raise ValueError("retries can't be 0")

        if response.status_code == 451:
            raise RepositoryUnavailableError(
                f"GitHub repository unavailable (HTTP 451): {response.url}",
                request=response.request,
                response=response,
            )

        # We stop before we hit the actual rate limit cause github doesn't seem to like it
        # if we go to zero.
        total_limit = int(response.headers.get("x-ratelimit-limit", "0"))
        remaining_requests = int(response.headers.get("x-ratelimit-remaining", "0"))
        if "x-ratelimit-reset" in response.headers and (
            remaining_requests < total_limit * 0.1
            or (response.status_code == 403 and remaining_requests == 0)
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
        else:
            response.raise_for_status()
            if caching_headers:
                logger.info(f"Fresh response: {response.url}")
            else:
                logger.info(f"Fetched: {response.url}")

        return response

    async def fetch_latest_releases_graphql(
        self, repos: list[GitHubRepo]
    ) -> dict[GitHubRepo, list[dict[str, Any]]]:
        """Fetch the first release for up to 100 repositories.

        Missing entries need a REST lookup; an empty list means the repository
        has no releases. Keep partial successes when individual lookups fail.
        """
        repos = list(dict.fromkeys(repos))
        if not repos:
            return {}
        if len(repos) > 100:
            raise ValueError("Release batches must contain at most 100 repositories")

        # Alias the fields to their REST names for the existing release parser.
        # Include prereleases, as the REST release list does.
        repositories = "\n".join(
            f"""
            repo{idx}: repository(owner: {json.dumps(repo.org)}, name: {json.dumps(repo.project)}) {{
                releases(first: 1, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
                    nodes {{
                        tag_name: tagName
                        name
                        prerelease: isPrerelease
                        published_at: publishedAt
                        html_url: url
                    }}
                }}
            }}
            """
            for idx, repo in enumerate(repos)
        )
        query = "query {" + repositories + "rateLimit { cost }}"
        for attempt in range(self.settings.retries):
            try:
                payload, headers, _ = await self.fetch_json(
                    "https://api.github.com/graphql", query=query
                )
                assert isinstance(payload, dict)
                errors = payload.get("errors", [])
                if any(error.get("type") == "RATE_LIMITED" for error in errors):
                    raise RateLimitError(float(headers.get("retry-after", 300)))
            except RateLimitError as err:
                if attempt == self.settings.retries - 1:
                    logger.warning(
                        "GitHub GraphQL rate limit retries exhausted; using REST"
                    )
                    return {}
                logger.info(
                    f"GitHub GraphQL rate limit, sleeping for {int(err.sleep)}s"
                )
                await asyncio.sleep(err.sleep)
                continue
            except HTTPError as err:
                logger.warning(
                    f"GitHub GraphQL release lookup failed; using REST: {err}"
                )
                return {}
            break
        else:
            raise ValueError("retries can't be 0")

        failed = set()
        for error in errors:
            logger.warning(f"GitHub GraphQL release lookup: {error['message']}")
            if not error.get("path"):
                return {}
            failed.add(error["path"][0])

        data = payload.get("data") or {}
        releases = {}
        for idx, repo in enumerate(repos):
            alias = f"repo{idx}"
            repository = data.get(alias)
            if alias in failed or repository is None:
                continue
            connection = repository.get("releases")
            if connection is None or connection.get("nodes") is None:
                continue
            nodes = connection["nodes"]
            if any(node is None for node in nodes):
                continue
            releases[repo] = nodes
        cost = (data.get("rateLimit") or {}).get("cost")
        logger.info(
            f"Fetched latest releases for {len(releases)}/{len(repos)} repositories "
            f"with GraphQL (cost: {cost})"
        )
        return releases


class CachedGitHubClient(GitHubClient):
    """Reuse successful GET responses for one repository group."""

    def __init__(self, client: GitHubClient):
        self.auth_headers = client.auth_headers
        self.api_concurrency = client.api_concurrency
        self.client = client.client
        self.settings = client.settings
        self._responses: dict[RequestKey, Response] = {}

    @sentry_sdk.trace
    async def fetch_json(
        self,
        url: str,
        caching_headers: dict[str, str] | None = None,
        *,
        query: str | None = None,
    ) -> tuple[Any | None, Mapping[str, str], str]:
        if query is not None:
            return await super().fetch_json(url, caching_headers, query=query)

        if caching_headers is None:
            caching_headers = {}
        cache_key = RequestKey(url, frozenset(caching_headers.items()))
        if (response := self._responses.get(cache_key)) is None:
            response = await self._fetch_response(url, caching_headers)
        else:
            logger.info(f"Reusing response: {url}")

        payload = None
        if response.status_code != 304:
            # Decode on each use so callers cannot mutate another item's payload.
            payload = response.json()
        self._responses[cache_key] = response
        return payload, response.headers, str(response.url)


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
    if (cached := _read_cached_response(cache_path)) is not None:
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
    _write_cached_response(cache_path, cached_release)
    return payload, response_url


class CacheMeta(BaseModel):
    etag: str
    # The final URL after redirects, if different from the request URL.
    response_url: str | None = None


class CachedResponse(BaseModel):
    metadata: CacheMeta
    payload: Any


def _read_cached_response(cache_path: Path) -> CachedResponse | None:
    try:
        return CachedResponse.model_validate_json(cache_path.read_bytes())
    except FileNotFoundError:
        return None
    except ValidationError as err:
        logger.warning(f"Ignoring invalid GitHub cache: {cache_path}")
        # Report corruption even though a fresh fetch lets the bot recover.
        sentry_sdk.capture_exception(err)
        return None


def _write_cached_response(cache_path: Path, cached: CachedResponse) -> None:
    """Publish a complete cache file without named staging files (Linux only)."""
    # The unnamed inode is discarded when its last fd closes, even after SIGKILL.
    # O_EXCL is omitted so we can link the completed file into the cache.
    fd = os.open(cache_path.parent, os.O_TMPFILE | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as cache_file:
        cache_file.write(cached.model_dump_json())
        # Flush Python's buffer before making the file visible to readers.
        cache_file.flush()
        # Hard links cannot replace an existing path. Remove the old cache only
        # after staging is complete; the brief gap is a safe, refetchable miss.
        cache_path.unlink(missing_ok=True)
        try:
            # Dereference the procfs fd symlink to link the completed inode,
            # rather than the symlink itself, directly to the final cache path.
            os.link(
                f"/proc/self/fd/{cache_file.fileno()}",
                cache_path,
                follow_symlinks=True,
            )
        except FileExistsError:
            # Another writer has already published a complete response.
            pass


@sentry_sdk.trace
async def get_releases(
    repo: GitHubRepo, repo_cache_root: Path, client: GitHubClient, allow_stale: bool
) -> list[dict[str, Any]]:
    """Fetch releases, reducing the page size if GitHub cannot serve larger pages."""
    try:
        return await _get_releases(repo, repo_cache_root, client, allow_stale, 100)
    except (HTTPStatusError, RemoteProtocolError) as err:
        if isinstance(err, HTTPStatusError) and err.response.status_code != 504:
            raise
        logger.warning(
            f"GitHub release requests for {repo} failed after retries: {err}; "
            "restarting with 30 releases per page"
        )

    # Page offsets change with the page size, so restart with a separate cache.
    return await _get_releases(repo, repo_cache_root, client, allow_stale, 30)


async def _get_releases(
    repo: GitHubRepo,
    repo_cache_root: Path,
    client: GitHubClient,
    allow_stale: bool,
    per_page: int,
) -> list[dict[str, Any]]:
    releases_cache = repo_cache_root.joinpath(f"releases-{per_page}")
    releases_cache.mkdir(exist_ok=True, parents=True)

    # Preserve the 1000-release limit even when the page size does not divide it.
    max_releases = 1000
    max_pages = (max_releases + per_page - 1) // per_page
    all_releases: list[dict[str, Any]] = []
    for page_number in range(1, max_pages + 1):
        page_url = f"{repo.api_releases()}?page={page_number}&per_page={per_page}"
        page_cache = releases_cache.joinpath(f"{page_number}.json")
        if (cached := _read_cached_response(page_cache)) is not None:
            if allow_stale:
                logger.info(f"Cache unchecked: {page_url}")
                all_releases += cached.payload
                # A short page marks the end of the release history.
                if len(cached.payload) < per_page:
                    break
                else:
                    continue

            logger.info(f"Revalidating: {page_url}")
            headers = {"If-None-Match": cached.metadata.etag}
            page_releases, headers, _url = await client.fetch_json(page_url, headers)
            # If the first page matches, assume all other pages are fresh too.
            # Older releases rarely change, so this can save a lot of requests.
            if page_releases is None:
                all_releases += cached.payload
                if page_number == 1:
                    allow_stale = True
                # A short page marks the end of the release history.
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
        _write_cached_response(page_cache, cached_release)

        # A short page marks the end of the release history.
        if len(page_releases) < per_page:
            break

    return all_releases[:max_releases]


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
