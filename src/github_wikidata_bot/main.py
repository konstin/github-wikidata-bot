from __future__ import annotations

import argparse
import asyncio
import logging.config
import textwrap
import time
from pathlib import Path

import sentry_sdk
from httpx import AsyncClient, HTTPError, HTTPStatusError

from github_wikidata_bot.github import (
    Project,
    RateLimitError,
    analyse_release,
    analyse_tag,
    fetch_json,
    get_data_from_github,
)
from github_wikidata_bot.project import WikidataProject
from github_wikidata_bot.session import Config, Session
from github_wikidata_bot.sparql import cached_projects_query, query_best_versions
from github_wikidata_bot.version import SimpleSortableVersion
from github_wikidata_bot.wikidata_api import WikidataError
from github_wikidata_bot.wikidata_update import update_wikidata

logger = logging.getLogger(__name__)


async def check_fast_path(
    project: WikidataProject,
    best_versions: dict[str, list[str]],
    client: AsyncClient,
    session: Session,
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

    try:
        releases, _ = await fetch_json(
            project.repo.api_releases() + "?per_page=1", client, session
        )
        assert isinstance(releases, list)  # For the type checker
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
            tags, _ = await fetch_json(project.repo.api_tags(), client, session)
            assert isinstance(tags, list)  # For the type checker
        except HTTPStatusError as e:
            # GitHub raises a 404 if there are no tags
            if e.response.status_code == 404:
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
            project_info, _ = await fetch_json(project.repo.api_base(), client, session)
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
    client: AsyncClient,
    allow_stale: bool,
    session: Session,
):
    try:
        if await check_fast_path(project, best_versions, client, session):
            return

        properties: Project = await get_data_from_github(
            project.repo, project, client, allow_stale, session
        )
    except HTTPError as e:
        logger.error(
            f"Github API request for {project.label} ({project.q_value}) failed: {e}",
            exc_info=True,
        )
        return

    if not session.dry_run:
        # TODO: Move retries to the http calls themselves, we want to retry individual network requests, not the whole
        # item as we had to do with pywikibot.
        for attempt in range(session.retries):
            try:
                await update_wikidata(properties, client, session)
            except WikidataError as e:
                if attempt < session.retries - 1:
                    backoff = 2**attempt + 2
                    logger.error(
                        f"Failed to update (attempt {attempt + 1}/{session.retries}), "
                        f"retrying after {backoff}s: {e}",
                        exc_info=True,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(f"Failed to update: {e}", exc_info=True)
                    raise
            else:
                return


async def update_project_with_retries(
    project: WikidataProject,
    best_versions: dict[str, list[str]],
    allow_stale: bool,
    client: AsyncClient,
    session: Session,
):
    with sentry_sdk.start_transaction(name="Update project") as transaction:
        transaction.set_data("project", project.q_value_url)
        transaction.set_data("project-label", project.label)
        for _ in range(session.retries):
            start = time.time()
            try:
                # If a project takes over 2min, skip it for performance.
                await asyncio.wait_for(
                    update_project(
                        project, best_versions, client, allow_stale, session
                    ),
                    timeout=120,
                )
            except TimeoutError:
                logger.warning(f"Timeout processing {project.label}")
            except RateLimitError as e:
                # We have to catch this error here to avoid the timeout.
                logger.info(
                    f"github rate limit exceed, sleeping until reset in {int(e.sleep)}s"
                )
                await asyncio.sleep(e.sleep)
                continue
            except WikidataError as e:
                logger.error(f"Failed to update: {e}")
                break

            duration = time.time() - start
            logger.info(f"{project.label} took {duration:.3f}s")
            break


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

    conf = {
        "version": 1,
        "formatters": {
            "extended": {
                "format": "%(asctime)s %(levelname)-8s %(message)s",
                "class": "github_wikidata_bot.session.NoTracebackFormatter",
            }
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "extended"},
            "all": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir.joinpath("all.log")),
                "formatter": "extended",
                "maxBytes": 32 * 1024 * 1024,
                "backupCount": 10,
            },
            "error": {
                "class": "logging.handlers.RotatingFileHandler",
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


async def run(
    project_filter: str | None, cache_sparql: bool, allow_stale: bool, session: Session
):
    await session.connect()
    logger.info("Querying Projects")
    projects = await cached_projects_query(cache_sparql, session, project_filter)
    logger.info(f"Found {len(projects)} projects")
    logger.info("Querying versions")
    best_versions = await query_best_versions(cache_sparql, session)
    logger.info("Processing projects")

    for idx, project in enumerate(projects):
        logger.info(
            f"## [{idx}/{len(projects)}] {project.label}: {project.q_value_url} {project.repo}"
        )
        await update_project_with_retries(
            project, best_versions, allow_stale, session.wikidata.client, session
        )
    logger.info("# Finished successfully")


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
    async with AsyncClient(
        timeout=Session.http_timeout, headers={"User-Agent": Session.user_agent}
    ) as client:
        config = Config.load()
        session = Session(config, client)

        await run(args.filter, args.cache_sparql, args.allow_stale, session)
        logger.info(f"Made {session.wikidata.request_counter} wikidata requests")
