from __future__ import annotations

import argparse
import asyncio
import logging.config
import logging.handlers
import subprocess
import textwrap
import time
from itertools import batched
from pathlib import Path
from subprocess import CalledProcessError
from typing import Any

import sentry_sdk
from httpx import AsyncClient, HTTPError, HTTPStatusError

from github_wikidata_bot.github import (
    CachedGitHubClient,
    GitHubClient,
    Project,
    RateLimitError,
    RepositoryUnavailableError,
    analyse_release,
    analyse_tag,
    get_data_from_github,
)
from github_wikidata_bot.project import GitHubRepo, InvalidProject, WikidataProject
from github_wikidata_bot.run_summary import (
    FailureReason,
    ProjectOutcome,
    ProjectResult,
    RunSummary,
    SkipReason,
)
from github_wikidata_bot.settings import Secrets, Settings
from github_wikidata_bot.sparql import cached_projects_query, query_best_versions
from github_wikidata_bot.version import SimpleSortableVersion
from github_wikidata_bot.wikidata_api import (
    APIError,
    MaxLagError,
    MissingEntityError,
    WikidataClient,
    WikidataError,
)
from github_wikidata_bot.wikidata_update import update_wikidata

logger = logging.getLogger(__name__)


async def check_fast_path(
    project: WikidataProject,
    best_versions: dict[str, list[str]],
    github_client: GitHubClient,
    releases: list[dict[str, Any]] | None = None,
) -> bool:
    """Check whether the latest GitHub release matches the latest version on wikidata, and if so,
    skip the expensive processing."""
    if not project.label:
        logger.info(f"No fast path, no project label: {project.label}")
        return False

    if len(best_versions.get(project.q_value_url, [])) == 1:
        project_version = best_versions[project.q_value_url][0]
    else:
        project_version = None

    if releases is None:
        try:
            releases, _, _ = await github_client.fetch_json(
                project.repo.api_releases() + "?per_page=1"
            )
            assert isinstance(releases, list)  # For the type checker
        except RepositoryUnavailableError:
            raise
        except HTTPError as e:
            logger.info(f"No fast path, fetch releases errored: {e}")
            return False
    if len(releases) == 1:
        result = analyse_release(releases[0], project.label)
        if result:
            if result.version == project_version:
                logger.info(f"Fresh using releases fast path: {project_version}")
                return True
            else:
                wikidata = ", ".join(best_versions.get(project.q_value_url, []))
                wikidata = textwrap.shorten(wikidata, width=50, placeholder="...")
                logger.info(
                    "No fast path, "
                    + f"wikidata: {wikidata}, "
                    + f"releases: {result.version}"
                )
                return False
        else:
            logger.info("No fast path, release failed to analyse")
            return False
    else:
        try:
            tags, _, _ = await github_client.fetch_json(project.repo.api_tags())
            assert isinstance(tags, list)  # For the type checker
        except RepositoryUnavailableError:
            raise
        except HTTPStatusError as e:
            # GitHub raises 404 if there are no tags, 409 for empty repos
            if e.response.status_code in (404, 409):
                if not best_versions.get(project.q_value_url):
                    logger.info("Fresh, no releases or tags")
                    return True
                else:
                    logger.info(
                        f"No fast path, no releases or tags but a wikidata version {project_version}"
                    )
                    return False
            else:
                logger.info(f"No fast path, fetch tags errored: {e}")
                return False
        else:
            project_info, _, _ = await github_client.fetch_json(project.repo.api_base())
            assert isinstance(project_info, dict)  # For the type checker
            extracted_tags = [
                analyse_tag(release, project_info, []) for release in tags
            ]
            filtered = [v for v in extracted_tags if v is not None]
            filtered.sort(key=lambda x: SimpleSortableVersion(x.version))
            if len(filtered) > 0:
                if filtered[-1].version == project_version:
                    logger.info(f"Fresh using tags fast path: {project_version}")
                    return True
                else:
                    wikidata = ", ".join(best_versions.get(project.q_value_url, []))
                    wikidata = textwrap.shorten(wikidata, width=50, placeholder="...")
                    logger.info(
                        f"No fast path, wikidata: {wikidata}, tags: {filtered[-1].version}"
                    )
                    return False
            else:
                logger.info("No fast path, tag failed to analyse")
                return False


@sentry_sdk.trace
async def update_project(
    project: WikidataProject,
    best_versions: dict[str, list[str]],
    allow_stale: bool,
    settings: Settings,
    wikidata: WikidataClient,
    github_client: GitHubClient,
    latest_releases: list[dict[str, Any]] | None = None,
) -> SkipReason | None:
    try:
        if await check_fast_path(
            project, best_versions, github_client, latest_releases
        ):
            return

        properties: Project = await get_data_from_github(
            project, allow_stale, github_client, settings, wikidata.tags_over_releases
        )
    except RepositoryUnavailableError as err:
        logger.warning(f"GitHub repository unavailable, skipping: {err}")
        return SkipReason.REPOSITORY_UNAVAILABLE
    except HTTPStatusError as err:
        # TODO: Figure out what update wikidata should get when a project was deleted.
        if err.response.status_code == 404:
            logger.warning(f"GitHub repo not found: {err}")
            return SkipReason.REPOSITORY_NOT_FOUND
        else:
            raise

    if not settings.dry_run:
        # TODO: Move retries to the http calls themselves, we want to retry individual network requests, not the whole
        # item as we had to do with pywikibot.
        for attempt in range(settings.retries):
            try:
                return await update_wikidata(properties, settings, wikidata)
            except MissingEntityError:
                logger.warning(
                    f"Wikidata entity {project.q_value} no longer exists, skipping"
                )
                return SkipReason.MISSING_ENTITY
            except APIError as err:
                if err.is_entity_too_big():
                    # Wikidata has a hard 3 MiB entity size limit (maxSerializedEntitySize=3000 KB).
                    # https://www.wikidata.org/wiki/Wikidata:WikiProject_Limits_of_Wikidata
                    # https://doc.wikimedia.org/Wikibase/master/php/docs_topics_options.html
                    # TODO: Remove old version claims to make room for new ones.
                    logger.warning("Entity too big, skipping")
                    return SkipReason.ENTITY_TOO_BIG
                if attempt < settings.retries - 1:
                    backoff = 2**attempt + 2
                    logger.warning(
                        f"Failed to update (attempt {attempt + 1}/{settings.retries}), "
                        f"retrying after {backoff}s: {err}"
                    )
                    await asyncio.sleep(backoff)
                else:
                    raise
            except WikidataError as err:
                if attempt < settings.retries - 1:
                    backoff = 2**attempt + 2
                    logger.warning(
                        f"Failed to update (attempt {attempt + 1}/{settings.retries}), "
                        f"retrying after {backoff}s: {err}"
                    )
                    await asyncio.sleep(backoff)
                else:
                    raise
        raise ValueError("retries can't be 0")
    return SkipReason.DRY_RUN


async def update_project_with_retries(
    project: WikidataProject,
    best_versions: dict[str, list[str]],
    allow_stale: bool,
    settings: Settings,
    wikidata: WikidataClient,
    github_client: GitHubClient,
    latest_releases: list[dict[str, Any]] | None = None,
) -> ProjectResult:
    edits_before = wikidata.edit_counter
    with sentry_sdk.start_transaction(name="Update project") as transaction:
        transaction.set_data("project", project.q_value_url)
        transaction.set_data("project-label", project.label)
        for _ in range(settings.retries):
            start = time.time()
            try:
                # If a project takes over 5min, skip it for performance.
                skip_reason = await asyncio.wait_for(
                    update_project(
                        project,
                        best_versions,
                        allow_stale,
                        settings,
                        wikidata,
                        github_client,
                        latest_releases,
                    ),
                    timeout=5 * 60,
                )
            except TimeoutError:
                logger.warning(f"Timeout processing {project.label}")
                return ProjectResult(ProjectOutcome.SKIPPED, SkipReason.TIMEOUT)
            except RateLimitError as e:
                # We have to catch this error here to avoid the timeout.
                logger.info(
                    f"github rate limit exceed, sleeping until reset in {int(e.sleep)}s"
                )
                await asyncio.sleep(e.sleep)
                continue
            except InvalidProject as e:
                logger.warning(f"Invalid project, skipping: {e}")
                return ProjectResult(ProjectOutcome.SKIPPED, SkipReason.INVALID_PROJECT)
            except MaxLagError as err:
                logger.warning(
                    f"Wikidata server lag, skipping {project.q_value}: {err}"
                )
                return ProjectResult(ProjectOutcome.SKIPPED, SkipReason.SERVER_LAG)
            except (WikidataError, HTTPError) as err:
                # NoTracebackFormatter suppresses exception details, so include them here.
                logger.exception(
                    f"Failed to update {project.q_value}: {type(err).__name__}: {err}"  # noqa: TRY401
                )
                return ProjectResult(
                    ProjectOutcome.FAILED, FailureReason.from_exception(err)
                )

            duration = time.time() - start
            logger.info(f"{project.label} took {duration:.3f}s")
            if skip_reason is not None:
                return ProjectResult(ProjectOutcome.SKIPPED, skip_reason)
            return ProjectResult(
                ProjectOutcome.UPDATED
                if wikidata.edit_counter > edits_before
                else ProjectOutcome.UNCHANGED
            )
        logger.warning(
            f"GitHub rate limit retries exhausted, skipping {project.q_value}"
        )
        return ProjectResult(ProjectOutcome.SKIPPED, SkipReason.RATE_LIMIT)


def init_logging(quiet: bool) -> None:
    """
    In cron jobs you do not want logging to stdout / stderr,
    therefore the quiet option allows disabling that.
    """
    if quiet:
        handlers = ["all", "error"]
    else:
        handlers = ["console", "all", "error"]

    log_dir = Path("log")
    log_dir.mkdir(exist_ok=True)

    def _qualified(cls: type) -> str:
        return f"{cls.__module__}.{cls.__qualname__}"

    conf = {
        "version": 1,
        "formatters": {
            "extended": {
                "format": "%(asctime)s %(levelname)-8s %(message)s",
                "class": _qualified(NoTracebackFormatter),
            }
        },
        "handlers": {
            "console": {
                "class": _qualified(logging.StreamHandler),
                "formatter": "extended",
            },
            "all": {
                "class": _qualified(logging.handlers.RotatingFileHandler),
                "filename": str(log_dir.joinpath("all.log")),
                "formatter": "extended",
                "maxBytes": 32 * 1024 * 1024,
                "backupCount": 10,
            },
            "error": {
                "class": _qualified(logging.handlers.RotatingFileHandler),
                "filename": str(log_dir.joinpath("error.log")),
                "formatter": "extended",
                "level": "WARN",
                "maxBytes": 32 * 1024 * 1024,
                "backupCount": 10,
            },
        },
        "loggers": {"github_wikidata_bot": {"handlers": handlers, "level": "INFO"}},
    }

    logging.config.dictConfig(conf)
    logger.info("Starting")


def init_sentry(dsn: str):
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


async def run(
    project_filter: str | None,
    cache_sparql: bool,
    allow_stale: bool,
    settings: Settings,
    wikidata: WikidataClient,
    github_client: GitHubClient,
) -> RunSummary:
    logger.info("Querying Projects")
    projects = await cached_projects_query(
        cache_sparql, wikidata, settings, project_filter
    )
    logger.info(f"Found {len(projects)} projects")
    logger.info("Querying versions")
    best_versions = await query_best_versions(cache_sparql, wikidata, settings)
    logger.info("Processing projects")

    projects_by_repo: dict[GitHubRepo, list[WikidataProject]] = {}
    for project in projects:
        projects_by_repo.setdefault(project.repo, []).append(project)

    summary = RunSummary()
    idx = 0
    for repo_batch in batched(projects_by_repo.items(), 100):
        latest_releases = await github_client.fetch_latest_releases_graphql(
            [
                repo
                for repo, repo_projects in repo_batch
                if any(project.label for project in repo_projects)
            ]
        )
        for repo, repo_projects in repo_batch:
            repo_client = CachedGitHubClient(github_client)
            for project in repo_projects:
                logger.info(
                    f"## [{idx}/{len(projects)}] {project.label}: {project.q_value_url} {project.repo}"
                )
                result = await update_project_with_retries(
                    project,
                    best_versions,
                    allow_stale,
                    settings,
                    wikidata,
                    repo_client,
                    latest_releases.get(repo),
                )
                summary.record(result)
                idx += 1
    logger.info(f"# Finished: {summary}")
    return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter")
    parser.add_argument(
        "--cache-sparql",
        help="Use locally cached wikidata sparql queries instead of doing a fresh network request",
        action="store_true",
    )
    parser.add_argument(
        "--allow-stale",
        help="Allow stale cached responses from the GitHub API",
        action="store_true",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Do not log to stdout/stderr"
    )
    args = parser.parse_args()

    init_logging(args.quiet)

    secrets = Secrets.load()
    if secrets.sentry_dsn:
        init_sentry(secrets.sentry_dsn)
    settings = Settings()
    try:
        async with AsyncClient(
            timeout=settings.http_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            wikidata = WikidataClient(client, secrets, settings)
            await wikidata.connect(settings)
            github_client = GitHubClient(secrets, client, settings)

            await run(
                args.filter,
                args.cache_sparql,
                args.allow_stale,
                settings,
                wikidata,
                github_client,
            )
            logger.info(f"Made {wikidata.request_counter} wikidata requests")
    finally:
        # Close the client so the atexit handler early-returns instead of
        # printing "Sentry is attempting to send N pending events". flush()
        # alone leaves the client alive, and events queued during teardown
        # re-trigger the atexit drain (getsentry/sentry-python#862).
        sentry_sdk.get_client().close(timeout=10)


class NoTracebackFormatter(logging.Formatter):
    """https://stackoverflow.com/a/73695412/3549270"""

    def formatException(self, ei):
        return ""

    def formatStack(self, stack_info):
        return ""
