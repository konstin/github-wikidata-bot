import argparse
import asyncio
import enum
import logging.config
import textwrap
import time
from pathlib import Path
from typing import Any

import pywikibot
import sentry_sdk
from hishel import AsyncSqliteStorage
from hishel.httpx import AsyncCacheClient
from httpx import AsyncClient, HTTPError, HTTPStatusError
from pywikibot import Claim, ItemPage, WbTime
from pywikibot.exceptions import APIError

from .github import (
    Project,
    analyse_release,
    analyse_tag,
    get_data_from_github,
    get_json_cached,
    RateLimitError,
)
from .redirects import RedirectDict
from .settings import Settings
from .sparql import WikidataProject, query_best_versions, query_projects
from .utils import (
    SimpleSortableVersion,
    github_repo_to_api,
    github_repo_to_api_releases,
    github_repo_to_api_tags,
    is_edit_conflict,
    normalize_url,
)
from .website import is_website_other_property

logger = logging.getLogger(__name__)


class Properties(enum.Enum):
    """Commonly used Wikidata properties"""

    software_version = "P348"
    publication_date = "P577"
    retrieved = "P813"
    reference_url = "P854"
    official_website = "P856"
    source_code_repository = "P1324"
    title = "P1476"
    vcs = "P8423"
    web_interface_software = "P10627"
    license = "P275"
    version_type = "P548"

    def new_claim(self, value: Any, settings: Settings) -> Claim:
        """Builds a new claim for this property and the given target value."""
        claim = Claim(settings.bot.repo, self.value)
        claim.setTarget(value)
        return claim

    def get_claim(self, item: ItemPage, target: Any) -> Claim | None:
        """Returns an existing claim for this property and the given target value."""
        if self.value not in item.claims:
            return None
        all_claims: list[Claim] = item.claims.get(self.value, [])
        return next((c for c in all_claims if c.target_equals(target)), None)


def create_sources(
    url: str,
    retrieved: WbTime,
    settings: Settings,
    title: str | None = None,
    date: WbTime | None = None,
) -> list[Claim]:
    """
    Gets or creates a `source` under the property `claim` to `url`
    """
    sources: list[Claim] = [
        Properties.reference_url.new_claim(url, settings),
        Properties.retrieved.new_claim(retrieved, settings),
    ]
    if title:
        text = pywikibot.WbMonolingualText(title, "en")
        sources.append(Properties.title.new_claim(text, settings))
    if date:
        sources.append(Properties.publication_date.new_claim(date, settings))
    return sources


def normalize_repo_url(
    item: ItemPage,
    url_normalized: str,
    url_raw: str,
    q_value: str,
    settings: Settings,
):
    """Canonicalize the github url
    This use the format https://github.com/[owner]/[repo]

    Note: This apparently only works with a bot account
    """
    if url_raw == url_normalized:
        return

    logger.info(f"Normalizing {url_raw} to {url_normalized}")

    source_p = Properties.source_code_repository.value
    urls = item.claims[source_p]
    if source_p in item.claims and len(urls) == 2:
        if urls[0].getTarget() == url_normalized and urls[1].getTarget() == url_raw:
            logger.info("The old and the new url are already set, removing the old")
            item.removeClaims(urls[1], summary=settings.edit_summary)
            return
        if urls[0].getTarget() == url_raw and urls[1].getTarget() == url_normalized:
            logger.info("The old and the new url are already set, removing the old")
            item.removeClaims(urls[0], summary=settings.edit_summary)
            return

    if source_p in item.claims and len(urls) > 1:
        logger.info(f"Multiple source code repositories for {q_value} not supported")
        return

    url_claim = urls[0]
    if url_claim.getTarget() != url_raw:
        logger.error(
            f"The url on the object ({urls[0].getTarget()}) doesn't match "
            f"the url from the sparql query ({url_raw}) for {q_value}"
        )
        return

    # See https://www.wikidata.org/wiki/User_talk:Konstin#Github-wiki-bot_is_replacing_source_code_repository_qualifiers_with_obsolete_qualifier
    # Add git as vcs and github as web ui
    url_claim.changeTarget(url_normalized)
    if Properties.vcs.value not in url_claim.qualifiers:
        git = ItemPage(settings.bot.repo, "Q186055")
        url_claim.addQualifier(Properties.vcs.new_claim(git, settings))
    if Properties.web_interface_software.value not in url_claim.qualifiers:
        github = ItemPage(settings.bot.repo, "Q364")
        url_claim.addQualifier(
            Properties.web_interface_software.new_claim(github, settings)
        )


async def set_website(
    project: Project, client: AsyncClient, settings: Settings
) -> Claim | None:
    """Add the website if does not already exist"""
    if not project.website or not project.website.startswith("http"):
        return None

    if is_website_other_property(project.website):
        logger.info(f"Website is different property: {project.website}")
        return None

    url = (await RedirectDict.get_or_add(project.website, client)) or project.website
    return Properties.official_website.new_claim(url, settings)


def set_license(project: Project, settings: Settings) -> Claim | None:
    """Add the license if it does not already exist"""
    if not project.license or project.license not in settings.licenses:
        return None

    project_license = settings.licenses[project.license]
    page = pywikibot.ItemPage(settings.bot.repo, project_license)
    return Properties.license.new_claim(page, settings)


@sentry_sdk.trace
async def update_wikidata(project: Project, client: AsyncClient, settings: Settings):
    """Update wikidata entry with data from GitHub"""
    # Wikidata boilerplate
    q_value = project.project.replace("http://www.wikidata.org/entity/", "")
    with sentry_sdk.start_span(description="Get item page"):
        item = ItemPage(settings.bot.repo, title=q_value)
        item.get()

    urls = item.claims.get(Properties.source_code_repository.value, [])
    if len(urls) == 1:
        url_raw = urls[0].target
        url_normalized = str(normalize_url(url_raw))
        if settings.normalize_repo_url:
            normalize_repo_url(item, url_normalized, url_raw, q_value, settings)
    else:
        url_raw = project.repo
        url_normalized = str(normalize_url(url_raw))

    for claim, claim_kind in [
        (await set_website(project, client, settings), "website"),
        (set_license(project, settings), "license"),
    ]:
        if not claim:
            continue
        claim.addSources(
            create_sources(
                url=github_repo_to_api(url_normalized),
                retrieved=project.retrieved,
                settings=settings,
            )
        )
        with sentry_sdk.start_span(description=f"Set {claim_kind}"):
            settings.bot.user_add_claim_unless_exists(
                item, claim, exists_arg="", summary=settings.edit_summary
            )

    # Add all stable releases
    stable_releases = project.stable_release
    stable_releases.sort(key=lambda x: SimpleSortableVersion(x.version))

    if len(stable_releases) == 0:
        logger.info("No stable releases")
        return

    versions = [i.version for i in stable_releases]
    if len(versions) != len(set(versions)):
        duplicates = [
            f"{release.version} ({release.page})"
            for release in stable_releases
            if versions.count(release.version) > 1
        ]
        message = textwrap.shorten(", ".join(duplicates), width=200, placeholder="...")
        logger.info(f"There are duplicate releases in {q_value}: {message}")
        return

    latest_version: str | None = stable_releases[-1].version

    existing_versions = item.claims.get(Properties.software_version.value, [])
    github_version_names = [i.version for i in stable_releases]
    existing_preferred_ranks = [
        i for i in existing_versions if i.getRank() == "preferred"
    ]
    logger.info(
        f"Latest version github {latest_version}, "
        f"existing preferred ranks: {' '.join(i.getTarget() for i in existing_preferred_ranks)}"
    )

    for i in existing_preferred_ranks:
        if i.getTarget() not in github_version_names:
            logger.warning(
                f"There's a preferred rank for {q_value} for a version "
                f"which is not in the github page: {i.getTarget()}"
            )
            latest_version = None

    if len(stable_releases) > settings.max_releases:
        logger.info(
            f"Limiting {q_value} to {settings.max_releases} of {len(stable_releases)} stable releases"
        )
        stable_releases = stable_releases[-settings.max_releases :]
    else:
        logger.info(f"There are {len(stable_releases)} stable releases")

    avoid_changing_preferred = False
    # https://www.wikidata.org/w/index.php?title=Topic%3AY3yzaiuczuywkbgr&topic_showPostId=y3z03mkycwf3s6b4
    if len(stable_releases) == 1:
        if (
            len(existing_versions) == 1
            and existing_versions[0].getTarget() == stable_releases[0].version
        ):
            logger.info("Only a single version, avoiding setting preferred rank")
            avoid_changing_preferred = True
        if len(existing_versions) == 0:
            logger.info(
                "Creating only a single version, avoiding setting preferred rank"
            )
            avoid_changing_preferred = True

    for release in stable_releases:
        existing = Properties.software_version.get_claim(item, release.version)
        if (
            existing
            and existing.getRank() == "preferred"
            and latest_version
            and release.version != latest_version
            # Exclude long-term support releases
            # https://www.wikidata.org/wiki/User_talk:Konstin#Github-wiki-bot_does_not_add_%22version_type%22_(P548)
            and not any(
                version_type.target.id == "Q15726348"
                for version_type in existing.qualifiers.get(Properties.version_type, [])
            )
        ):
            logger.info(f"Setting normal rank for {existing.getTarget()}")
            try:
                with sentry_sdk.start_span(description="Change rank to normal"):
                    existing.changeRank("normal", summary=settings.edit_summary)
            except APIError as e:
                if is_edit_conflict(e):
                    logger.error(
                        f"Edit conflict for setting the normal rank on {q_value}",
                        exc_info=True,
                    )
                    # Try avoiding https://www.wikidata.org/wiki/User_talk:Konstin#Software_version
                    avoid_changing_preferred = True
                    continue
                else:
                    raise

        claim = Properties.software_version.new_claim(release.version, settings)
        claim.addQualifier(
            Properties.publication_date.new_claim(release.date, settings)
        )
        stable_release = ItemPage(settings.bot.repo, "Q2804309")
        claim.addQualifier(Properties.version_type.new_claim(stable_release, settings))
        claim.addSources(
            create_sources(
                url=release.page,
                retrieved=project.retrieved,
                title=f"Release {release.version}",
                date=release.date,
                settings=settings,
            )
        )

        set_preferred_rank = (
            latest_version
            and release.version == latest_version
            and not avoid_changing_preferred
        )

        if set_preferred_rank:
            if not existing or existing.rank != "preferred":
                logger.info(f"Setting preferred rank for {claim.getTarget()}")
            claim.setRank("preferred")
        if not existing:
            logger.info(
                f"Creating {release.version} (rank: {'preferred' if set_preferred_rank else 'default'})"
            )
        with sentry_sdk.start_span(description="Create version"):
            added = settings.bot.user_add_claim_unless_exists(
                item,
                claim,
                # add when claim with same property, but not same target exists
                exists_arg="p",
                summary=settings.edit_summary,
            )
        if (
            not added
            and set_preferred_rank
            and existing
            and existing.rank != "preferred"
        ):
            logger.info(
                f"Claim exists, changing to preferred rank for {claim.getTarget()}"
            )
            with sentry_sdk.start_span(description="Change rank to preferred"):
                existing.changeRank("preferred", summary=settings.edit_summary)


async def check_fast_path(
    project: WikidataProject, best_versions: dict[str, list[str]], client: AsyncClient
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
        releases = await get_json_cached(api_url + "?per_page=1", client)
    except HTTPError as e:
        logger.info(f"No fast path, fetch releases errored: {e}")
        return False
    if len(releases) == 1:
        result = analyse_release(releases[0], {"name": project.projectLabel})
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
            tags = await get_json_cached(api_url, client)
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
            project_info = await get_json_cached(
                github_repo_to_api(project.repo), client
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
    if await check_fast_path(project, best_versions, client):
        return True

    try:
        properties: Project = await get_data_from_github(
            project.repo, project, client, settings
        )
    except HTTPError as e:
        logger.error(
            f"Github API request for {project.projectLabel} ({project.wikidata_id}) failed: {e}",
            exc_info=True,
        )
        return False

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
                return False
            await update_wikidata(properties, client, settings)
        except Exception as e:
            logger.error(f"Failed to update {properties.project}: {e}", exc_info=True)
            raise

    return False


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


async def run(project_filter: str | None, ignore_blacklist: bool, settings: Settings):
    storage = AsyncSqliteStorage(
        database_path="cache-http.db",
        default_ttl=60 * 60 * 24,  # 1 day
    )
    async with AsyncCacheClient(storage=storage) as client:
        logger.info("# Querying Projects")
        projects = query_projects(project_filter, ignore_blacklist)
        logger.info(f"{len(projects)} projects were found")
        logger.info("# Processing projects")
        best_versions = query_best_versions()

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
    parser.add_argument("--debug-http", action="store_true")
    parser.add_argument("--ignore-blacklist", action="store_true")
    parser.add_argument(
        "--quiet", action="store_true", help="Do not log to stdout/stderr"
    )
    args = parser.parse_args()

    init_logging(args.quiet, args.debug_http)
    settings = Settings(args.github_oauth_token)

    asyncio.run(run(args.filter, args.ignore_blacklist, settings))
