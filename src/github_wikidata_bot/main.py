import argparse
import asyncio
import logging.config
import time
from pathlib import Path

import pywikibot
import sentry_sdk
from hishel import AsyncSqliteStorage, CacheOptions, SpecificationPolicy
from hishel.httpx import AsyncCacheClient
from httpx import AsyncClient, HTTPError, HTTPStatusError
from pydantic import TypeAdapter

from .github import (
    Project,
    analyse_release,
    analyse_tag,
    get_data_from_github,
    fetch_json,
    RateLimitError,
)
from .settings import Settings
from .sparql import (
    WikidataProject,
    query_best_versions,
    filter_projects,
    cached_sparql_query,
)
from .utils import (
    SimpleSortableVersion,
    github_repo_to_api,
    github_repo_to_api_releases,
    github_repo_to_api_tags,
)
from .wikidata import update_wikidata

logger = logging.getLogger(__name__)


async def check_fast_path(
    project: WikidataProject,
    best_versions: dict[str, list[str]],
    client: AsyncClient,
    settings: Settings,
) -> bool:
    """Check whether the latest github release matches the latest version on wikidata, and if so,
    skip the expensive processing."""
    if not project.projectLabel:
        logger.info(f"No fast path, no project label: {project.projectLabel}")
        return False

    if len(best_versions.get(project.project, [])) == 1:
        project_version = best_versions[project.project][0]
    else:
        project_version = None

    api_url = github_repo_to_api_releases(project.repo)
    try:
        releases = await fetch_json(api_url + "?per_page=1", client, settings)
    except HTTPError as e:
        logger.info(f"No fast path, fetch releases errored: {e}")
        return False
    if len(releases) == 1:
        result = analyse_release(releases[0], {"name": project.projectLabel}, settings)
        if result:
            if result.version == project_version:
                logger.info(f"Fresh using releases fast path: {project_version}")
                return True
            else:
                logger.info(
                    f"No fast path, wikidata: {best_versions.get(project.project, [])}, releases: {result.version}"
                )
                return False
        else:
            logger.info("No fast path, release failed to analyse")
            return False
    else:
        api_url = github_repo_to_api_tags(project.repo)
        try:
            tags = await fetch_json(api_url, client, settings)
        except HTTPStatusError as e:
            # GitHub raises a 404 if there are no tags
            if e.response.status_code == 404:
                if not best_versions.get(project.project):
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
            project_info = await fetch_json(
                github_repo_to_api(project.repo), client, settings
            )
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
                    logger.info(
                        f"No fast path, wikidata: {best_versions.get(project.project, [])}, tags: {filtered[-1].version}"
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
    settings: Settings,
):
    if await check_fast_path(project, best_versions, client, settings):
        return

    try:
        properties: Project = await get_data_from_github(
            project.repo, project, client, settings
        )
    except HTTPError as e:
        logger.error(
            f"Github API request for {project.projectLabel} ({project.wikidata_id}) failed: {e}",
            exc_info=True,
        )
        return

    if settings.do_update_wikidata:
        try:
            # There are many spurious errors, mostly because pywikibot lacks http retrying,
            # so we just retry any pywikibot once.
            try:
                await update_wikidata(properties, client, settings)
            except pywikibot.exceptions.Error as e:
                logger.error(
                    f"Failed to update {properties.project}, retrying: {e}",
                    exc_info=True,
                )
            else:
                return
            await update_wikidata(properties, client, settings)
        except Exception as e:
            logger.error(f"Failed to update {properties.project}: {e}", exc_info=True)
            raise


def init_logging(quiet: bool, http_debug: bool) -> None:
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
                "class": "github_wikidata_bot.settings.NoTracebackFormatter",
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

    if http_debug:
        from http.client import HTTPConnection

        HTTPConnection.debuglevel = 1

        requests_log = logging.getLogger("urllib3")
        requests_log.setLevel(logging.DEBUG)
        requests_log.propagate = True


async def run(
    project_filter: str | None,
    ignore_blacklist: bool,
    cache_sparql: bool,
    allow_stale: bool,
    settings: Settings,
):
    storage = AsyncSqliteStorage(
        database_path="cache-http.db",
    )
    cache_options = CacheOptions(
        shared=False,
        allow_stale=allow_stale,
    )
    async with AsyncCacheClient(
        storage=storage, policy=SpecificationPolicy(cache_options=cache_options)
    ) as client:
        logger.info("Querying Projects")
        response = cached_sparql_query("free_software_items", cache_sparql, settings)
        project_list = TypeAdapter(list[WikidataProject]).validate_python(response)
        projects = filter_projects(
            ignore_blacklist, project_filter, project_list, response, settings
        )
        logger.info(f"{len(projects)} projects were found")
        logger.info("Querying versions")
        best_versions = query_best_versions(cache_sparql, settings)
        logger.info("Processing projects")

        for idx, project in enumerate(projects):
            while True:
                with sentry_sdk.start_transaction(name="Update project") as transaction:
                    transaction.set_data("project", project.project)
                    transaction.set_data("project-label", project.projectLabel)
                    logger.info(
                        f"## [{idx}/{len(projects)}] {project.projectLabel}: {project.project}"
                    )
                    try:
                        start = time.time()
                        try:
                            # If a project takes over 1min, skip it for performance.
                            await asyncio.wait_for(
                                update_project(
                                    project, best_versions, client, settings
                                ),
                                timeout=60,
                            )
                        except TimeoutError:
                            logger.warning(f"Timeout processing {project.projectLabel}")

                        duration = time.time() - start
                        logger.info(f"{project.projectLabel} took {duration:.3f}s")
                    except RateLimitError as e:
                        logger.info(
                            f"github rate limit exceed, sleeping until reset in {int(e.sleep)}s"
                        )
                        await asyncio.sleep(e.sleep)
                        continue
                break
        logger.info("# Finished successfully")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default="")
    parser.add_argument("--github-oauth-token")
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
    parser.add_argument("--debug-http", action="store_true")
    parser.add_argument("--ignore-blacklist", action="store_true")
    parser.add_argument(
        "--quiet", action="store_true", help="Do not log to stdout/stderr"
    )
    args = parser.parse_args()

    init_logging(args.quiet, args.debug_http)
    settings = Settings(args.github_oauth_token)

    asyncio.run(
        run(
            args.filter,
            args.ignore_blacklist,
            args.cache_sparql,
            args.allow_stale,
            settings,
        )
    )
