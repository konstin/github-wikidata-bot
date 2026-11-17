from __future__ import annotations

import argparse
import asyncio
import logging.config
import os
import subprocess
import textwrap
import time
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from subprocess import CalledProcessError

import sentry_sdk
from httpx2 import AsyncClient, HTTPError, HTTPStatusError
from sentry_sdk import metrics
from sentry_sdk.profiler import start_profiler, stop_profiler
from sentry_sdk.profiler.continuous_profiler import get_profiler_id

from github_wikidata_bot.github import (
    RELEASE_LIST_ADAPTER,
    CachedGitHubClient,
    GitHubClient,
    GitHubRelease,
    Project,
    RateLimitError,
    RepositorySummary,
    RepositoryUnavailableError,
    analyse_release,
    analyse_tag,
    get_data_from_github,
    wait_for_rate_limit,
)
from github_wikidata_bot.project import GitHubRepo, InvalidProject, WikidataProject
from github_wikidata_bot.run_summary import (
    FailureReason,
    ProjectOutcome,
    ProjectResult,
    RunSummary,
    SkipReason,
    UnavailableReason,
)
from github_wikidata_bot.settings import Secrets, Settings
from github_wikidata_bot.sparql import cached_projects_query, query_best_versions
from github_wikidata_bot.version import SimpleSortableVersion, extract_version
from github_wikidata_bot.wikidata_api import (
    APIError,
    MaxLagError,
    MaxLagStats,
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
    releases: list[GitHubRelease] | None = None,
    tags: list[str] | None = None,
) -> bool:
    """Check whether the latest GitHub release matches the latest version on wikidata, and if so,
    skip the expensive processing.

    `releases` and `tags` are fetched if missing. `tags` must be all tag names, and only for
    a repository that was not renamed.
    """
    if not project.label:
        logger.info(f"No fast path, no project label: {project.label}")
        return False

    if len(best_versions.get(project.q_value_url, [])) == 1:
        project_version = best_versions[project.q_value_url][0]
    else:
        project_version = None

    if releases is None:
        try:
            payload, _, _ = await github_client.fetch_json(
                project.repo.api_releases() + "?per_page=1"
            )
            releases = RELEASE_LIST_ADAPTER.validate_python(payload)
        except RepositoryUnavailableError:
            raise
        except HTTPError as e:
            logger.info(f"No fast path, fetch releases errored: {e}")
            return False
    if len(releases) == 1:
        result = analyse_release(releases[0], project.label)
        if result and result.release_type == "stable":
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
            logger.info("No fast path, release is unstable or could not be parsed")
            return False

    if tags is not None:
        versions = [
            match
            for tag in tags
            if (match := extract_version(tag, project.repo.project)) is not None
        ]
    else:
        try:
            rest_tags, _, _ = await github_client.fetch_json(project.repo.api_tags())
            assert isinstance(rest_tags, list)  # For the type checker
        except RepositoryUnavailableError:
            raise
        except HTTPStatusError as e:
            # A tags 404 can also mean the repository is gone. Check it on the full path.
            if e.response.status_code == 409:
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
        try:
            project_info, _, _ = await github_client.fetch_json(project.repo.api_base())
        except HTTPStatusError as err:
            if err.response.status_code == 404:
                return False
            raise
        assert isinstance(project_info, dict)  # For the type checker
        extracted_tags = [
            analyse_tag(release, project_info, []) for release in rest_tags
        ]
        versions = [
            (tag.release_type, tag.version) for tag in extracted_tags if tag is not None
        ]

    versions.sort(key=lambda version: SimpleSortableVersion(version[1]))
    if versions:
        release_type, latest_version = versions[-1]
        if release_type == "stable" and latest_version == project_version:
            logger.info(f"Fresh using tags fast path: {project_version}")
            return True
        else:
            wikidata = ", ".join(best_versions.get(project.q_value_url, []))
            wikidata = textwrap.shorten(wikidata, width=50, placeholder="...")
            logger.info(f"No fast path, wikidata: {wikidata}, tags: {latest_version}")
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
    maxlag: MaxLagStats,
    repository: RepositorySummary | None = None,
) -> SkipReason | UnavailableReason | None:
    tags = repository.tags if repository is not None else None
    no_releases_or_tags = (
        bool(project.label)
        and repository is not None
        and not repository.latest_releases
        and tags == []
    )
    try:
        if no_releases_or_tags:
            if not best_versions.get(project.q_value_url):
                logger.info("Fresh, no releases or tags")
                return
            logger.info("No fast path, no releases or tags but Wikidata has a version")
        elif await check_fast_path(
            project,
            best_versions,
            github_client,
            repository.latest_releases if repository is not None else None,
            tags,
        ):
            return

        properties: Project = await get_data_from_github(
            project,
            allow_stale,
            github_client,
            settings,
            wikidata.tags_over_releases,
            no_tags=no_releases_or_tags,
        )
    except RepositoryUnavailableError as err:
        if err.response.status_code == 404:
            logger.warning(f"GitHub repo not found: {err}")
            return UnavailableReason.REPOSITORY_NOT_FOUND
        logger.warning(f"GitHub repository unavailable: {err}")
        return UnavailableReason.REPOSITORY_UNAVAILABLE

    if not settings.dry_run:
        # TODO: Move retries to the http calls themselves, we want to retry individual network requests, not the whole
        # item as we had to do with pywikibot.
        for attempt in range(settings.retries):
            try:
                return await update_wikidata(properties, settings, wikidata, maxlag)
            except MissingEntityError:
                logger.warning(f"Wikidata entity {project.q_value} no longer exists")
                return UnavailableReason.MISSING_ENTITY
            except APIError as err:
                if err.is_entity_too_big():
                    # Wikidata has a hard 3 MiB entity size limit (maxSerializedEntitySize=3000 KB).
                    # https://www.wikidata.org/wiki/Wikidata:WikiProject_Limits_of_Wikidata
                    # https://doc.wikimedia.org/Wikibase/master/php/docs_topics_options.html
                    # TODO: Remove old version claims to make room for new ones.
                    logger.warning(
                        f"Wikidata entity {project.q_value} exceeds the size limit"
                    )
                    return UnavailableReason.ENTITY_TOO_BIG
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
    repository: RepositorySummary | None = None,
) -> ProjectResult:
    edits_before = wikidata.edit_counter
    maxlag = MaxLagStats()
    duration = 0.0  # Excludes GitHub rate-limit waits between attempts.
    with sentry_sdk.start_transaction(name="Update project") as transaction:
        transaction.set_data("project", project.q_value_url)
        transaction.set_data("project-label", project.label)

        for _ in range(settings.retries):
            start = time.perf_counter()
            try:
                # Collect profiling data only when actually working, but not during
                # rate-limit induced sleeps.
                try:
                    if transaction.sampled:
                        start_profiler()
                        # The transaction was created before the profiler started.
                        transaction.set_profiler_id(get_profiler_id())
                    reason = await asyncio.wait_for(
                        update_project(
                            project,
                            best_versions,
                            allow_stale,
                            settings,
                            wikidata,
                            github_client,
                            maxlag,
                            repository,
                        ),
                        timeout=5 * 60,
                    )
                finally:
                    duration += time.perf_counter() - start
                    if transaction.sampled:
                        stop_profiler()
            except RateLimitError as e:
                # We have to catch this error here to avoid the timeout.
                logger.info(
                    f"github rate limit exceed, sleeping until reset in {int(e.sleep)}s"
                )
                await wait_for_rate_limit(e.sleep, "rest")
                continue
            except InvalidProject as e:
                logger.warning(f"Invalid project, skipping: {e}")
                result = ProjectResult(
                    ProjectOutcome.SKIPPED, SkipReason.INVALID_PROJECT
                )
            except Exception as err:
                # NoTracebackFormatter suppresses exception details, so include them here.
                logger.exception(
                    f"Failed to update {project.q_value}: {type(err).__name__}: {err}"  # noqa: TRY401
                )
                result = ProjectResult(
                    ProjectOutcome.FAILED, FailureReason.from_exception(err)
                )
            else:
                if isinstance(reason, UnavailableReason):
                    result = ProjectResult(ProjectOutcome.UNAVAILABLE, reason)
                elif wikidata.edit_counter > edits_before:
                    result = ProjectResult(ProjectOutcome.UPDATED, reason)
                elif reason is not None:
                    result = ProjectResult(ProjectOutcome.SKIPPED, reason)
                else:
                    result = ProjectResult(ProjectOutcome.UP_TO_DATE)
            break
        else:
            logger.error(
                "Failed to update %s: GitHub rate limit retries exhausted",
                project.q_value,
            )
            result = ProjectResult(ProjectOutcome.FAILED, FailureReason(RateLimitError))

        edits = wikidata.edit_counter - edits_before
        if edits and result.outcome is not ProjectOutcome.UPDATED:
            logger.info(
                "Project %s: %s (%s Wikidata edits saved before stopping)",
                project.q_value,
                result,
                edits,
            )
        else:
            logger.info("Project %s: %s", project.q_value, result)
        logger.info(
            f"{project.label} took {duration:.3f}s "
            + f"({duration - maxlag.wait_seconds:.3f}s outside maxlag waits); {maxlag}"
        )
        return ProjectResult(result.outcome, result.reason, maxlag)


def init_logging(quiet: bool) -> None:
    """
    In cron jobs you do not want logging to stdout / stderr,
    therefore the quiet option allows disabling that.
    """
    if quiet:
        handlers = ["file"]
    else:
        handlers = ["console", "file"]

    log_dir = Path("log")
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    log_path = log_dir / f"run-{timestamp}-{os.getpid()}.log"

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
            "file": {
                "class": _qualified(logging.FileHandler),
                "filename": str(log_path),
                "formatter": "extended",
                "mode": "x",
            },
        },
        "loggers": {"github_wikidata_bot": {"handlers": handlers, "level": "INFO"}},
    }

    logging.config.dictConfig(conf)
    logger.info("Starting")
    # Each run creates a log file, so keep only the five newest to limit disk use.
    for old_log in sorted(log_dir.glob("run-*.log"))[:-5]:
        old_log.unlink(missing_ok=True)


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
        traces_sample_rate=0.1,
        profile_session_sample_rate=1.0,
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
        repository_summaries = await github_client.fetch_repository_summaries_graphql(
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
                    repository_summaries.get(repo),
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
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    run_attributes: dict[str, str | int] = {
        "run.mode": "filtered" if args.filter else "full",
        "run.status": "incomplete",
    }
    try:
        async with AsyncClient(
            timeout=settings.http_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            wikidata = WikidataClient(client, secrets, settings)
            await wikidata.connect(settings)
            github_client = GitHubClient(secrets, client, settings)

            summary = await run(
                args.filter,
                args.cache_sparql,
                args.allow_stale,
                settings,
                wikidata,
                github_client,
            )
            logger.info(f"Made {wikidata.request_counter} wikidata requests")
        run_attributes.update(
            {
                "run.status": "completed",
                "run.projects": summary.outcomes.total(),
                "run.wikidata_requests": wikidata.request_counter,
            }
        )
    finally:
        wall_time = time.perf_counter() - wall_start
        cpu_time = time.process_time() - cpu_start
        logger.info(
            "Run performance (%s, %s): wall=%.3fs, cpu=%.3fs",
            run_attributes["run.mode"],
            run_attributes["run.status"],
            wall_time,
            cpu_time,
        )
        if secrets.sentry_dsn:
            metrics.distribution(
                "bot.run.duration", wall_time, unit="second", attributes=run_attributes
            )
            metrics.distribution(
                "bot.run.cpu_time", cpu_time, unit="second", attributes=run_attributes
            )
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
