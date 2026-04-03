from __future__ import annotations
from github_wikidata_bot.settings import Settings

import enum
import logging
import textwrap

import sentry_sdk
from httpx import AsyncClient

from github_wikidata_bot.github import Project, Release
from github_wikidata_bot.project import GitHubRepo
from github_wikidata_bot.redirects import RedirectDict
from github_wikidata_bot.version import SimpleSortableVersion
from github_wikidata_bot.website import is_website_other_property
from github_wikidata_bot.wikidata_api import (
    Claim,
    Item,
    ItemValue,
    WikibaseMonolingualText,
    WikibaseTime,
    WikidataClient,
)

logger = logging.getLogger(__name__)


class Property(enum.Enum):
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


async def normalize_repo_url(
    item: Item,
    url_normalized: str,
    url_raw: str,
    q_value: str,
    wikidata: WikidataClient,
):
    """Canonicalize the github url to the format `https://github.com/[owner]/[repo]`.

    Note: This apparently only works with a bot account
    """
    if url_raw == url_normalized:
        return

    logger.info(f"Normalizing {url_raw} to {url_normalized}")

    source_p = Property.source_code_repository.value
    urls = item.claims[source_p]
    if source_p in item.claims and len(urls) == 2:
        if urls[0].value == url_normalized and urls[1].value == url_raw:
            logger.info("The old and the new url are already set, removing the old")
            await wikidata.remove_claim(urls[1], summary=wikidata.edit_summary)
            return
        if urls[0].value == url_raw and urls[1].value == url_normalized:
            logger.info("The old and the new url are already set, removing the old")
            await wikidata.remove_claim(urls[0], summary=wikidata.edit_summary)
            return

    if source_p in item.claims and len(urls) > 1:
        logger.info(f"Multiple source code repositories for {q_value} not supported")
        return

    url_claim = urls[0]
    if url_claim.value != url_raw:
        logger.error(
            f"The url on the object ({urls[0].value}) doesn't match "
            f"the url from the sparql query ({url_raw}) for {q_value}"
        )
        return

    # See https://www.wikidata.org/wiki/User_talk:Konstin#Github-wiki-bot_is_replacing_source_code_repository_qualifiers_with_obsolete_qualifier
    # Update value and add git as vcs and github as web ui
    url_claim.value = url_normalized
    if Property.vcs.value not in url_claim.qualifiers:
        git = ItemValue("Q186055")
        url_claim.add_qualifier(Property.vcs.value, git)
    if Property.web_interface_software.value not in url_claim.qualifiers:
        github = ItemValue("Q364")
        url_claim.add_qualifier(Property.web_interface_software.value, github)
    await wikidata.save_claims(item.id, [url_claim], summary=wikidata.edit_summary)


async def set_website(project: Project, client: AsyncClient) -> Claim | None:
    """Add the website if does not already exist"""
    if not project.website or not project.website.startswith("http"):
        return None

    if is_website_other_property(project.website):
        logger.info(f"Website is different property: {project.website}")
        return None

    url = (await RedirectDict.get_or_add(project.website, client)) or project.website
    return Claim(Property.official_website.value, url)


def set_license(project: Project, wikidata: WikidataClient) -> Claim | None:
    """Add the license if it does not already exist"""
    if not project.license or project.license not in wikidata.licenses:
        return None

    project_license = wikidata.licenses[project.license]
    return Claim(Property.license.value, ItemValue(project_license))


def release_to_claim(release: Release, project: Project, rank: str = "normal") -> Claim:
    """Build a version claim with qualifiers and sources."""
    claim = Claim(Property.software_version.value, release.version, rank=rank)
    publication_date = WikibaseTime.from_iso(release.timestamp.isoformat())
    claim.add_qualifier(Property.publication_date.value, publication_date)
    claim.add_qualifier(Property.version_type.value, ItemValue("Q2804309"))
    claim.add_sources(
        [
            Claim(
                Property.title.value,
                WikibaseMonolingualText(f"Release {release.version}", "en"),
            ),
            Claim(Property.reference_url.value, release.page),
            Claim(
                Property.retrieved.value,
                WikibaseTime.from_iso(project.retrieved.isoformat()),
            ),
            Claim(
                Property.publication_date.value,
                WikibaseTime.from_iso(release.timestamp.isoformat()),
            ),
        ]
    )
    return claim


async def update_website_and_license(
    project: Project, item: Item, wikidata: WikidataClient, settings: Settings
):
    urls = item.claims.get(Property.source_code_repository.value, [])
    if len(urls) == 1:
        assert isinstance(urls[0].value, str)
        url_raw = urls[0].value
        repo = GitHubRepo.from_url(url_raw)
        if settings.normalize_repo_url:
            await normalize_repo_url(
                item, str(repo), url_raw, project.wikidata.q_value, wikidata
            )
    else:
        repo = project.wikidata.repo

    # TODO: Stop breaking client isolation to fetch redirects
    resolved_website = await set_website(project, wikidata.client)
    for claim, kind in [
        (resolved_website, "website"),
        (set_license(project, wikidata), "license"),
    ]:
        if not claim:
            continue
        url = repo.api_base()
        claim.add_sources(
            [
                Claim(Property.reference_url.value, url),
                Claim(
                    Property.retrieved.value,
                    WikibaseTime.from_iso(project.retrieved.isoformat()),
                ),
            ]
        )
        if not item.has_claim(claim, single_valued=True):
            logger.info(f"Updating {kind}")
            await wikidata.add_claim(item, claim, summary=wikidata.edit_summary)


@sentry_sdk.trace
async def update_wikidata(
    project: Project, settings: Settings, wikidata: WikidataClient
):
    """Update wikidata entry with data from GitHub"""
    item = await wikidata.get_entity(project.wikidata.q_value)

    await update_website_and_license(project, item, wikidata, settings)

    # Add all stable releases
    stable_releases = project.stable_release
    stable_releases.sort(key=lambda x: SimpleSortableVersion(x.version))

    if len(stable_releases) == 0:
        logger.info("No stable releases")
        return

    versions = [release.version for release in stable_releases]
    if len(versions) != len(set(versions)):
        duplicates = [
            f"{release.version} ({release.page})"
            for release in stable_releases
            if versions.count(release.version) > 1
        ]
        message = textwrap.shorten(", ".join(duplicates), width=200, placeholder="...")
        logger.info(f"There are duplicate releases: {message}")
        return

    if len(stable_releases) > settings.max_releases:
        logger.info(
            f"Limiting to {settings.max_releases} of {len(stable_releases)} stable releases"
        )
        stable_releases = stable_releases[-settings.max_releases :]
    else:
        logger.info(f"There are {len(stable_releases)} stable releases")

    latest_version = stable_releases[-1].version

    existing_claims = item.claims.get(Property.software_version.value, [])
    github_versions = [release.version for release in stable_releases]
    existing_preferred_ranks = [
        claim for claim in existing_claims if claim.rank == "preferred"
    ]
    logger.info(
        f"Latest version github {latest_version}, "
        f"existing preferred ranks: {' '.join(str(i.value) for i in existing_preferred_ranks)}"
    )

    for claim in existing_preferred_ranks:
        if claim.value not in github_versions:
            logger.warning(
                f"A version which is not in the github page has a preferred rank: {claim.value}"
            )
            latest_version = None

    # A single version should have a normal rank, not a preferred rank.
    # https://www.wikidata.org/w/index.php?title=Topic%3AY3yzaiuczuywkbgr&topic_showPostId=y3z03mkycwf3s6b4
    if len(stable_releases) == 1:
        if (
            len(existing_claims) == 1
            and existing_claims[0].value == stable_releases[0].version
        ):
            logger.info("Only a single version, avoiding setting preferred rank")
            latest_version = None
        if len(existing_claims) == 0:
            logger.info(
                "Creating only a single version, avoiding setting preferred rank"
            )
            latest_version = None

    # Create all missing releases that get a normal rank (which exclude the latest release, if any)
    for release in stable_releases:
        if release.version == latest_version:
            continue
        claim = release_to_claim(release, project)
        if item.has_claim(claim):
            continue
        logger.info(f"Creating {release.version}")
        await wikidata.add_claim(item, claim, summary=wikidata.edit_summary)

    # In a single api call, demote non-latest preferred to normal and promote or create the latest with preferred rank,
    # to avoid a project having zero or two preferred ranks.
    # https://www.wikidata.org/w/index.php?title=Topic%3AY3yzaiuczuywkbgr&topic_showPostId=y3z03mkycwf3s6b4
    if latest_version:
        rank_changes: list[Claim] = []
        change_summaries = []
        for release in stable_releases:
            existing = item.get_claim(Property.software_version.value, release.version)
            if (
                existing
                and existing.rank == "preferred"
                and release.version != latest_version
            ):
                # preferred -> normal
                change_summaries.append(f"normal rank for {existing.value}")
                existing.rank = "normal"
                rank_changes.append(existing)
            elif (
                existing
                and release.version == latest_version
                and existing.rank != "preferred"
            ):
                # normal -> preferred
                change_summaries.append(f"preferred rank for {existing.value}")
                existing.rank = "preferred"
                rank_changes.append(existing)
            elif not existing and release.version == latest_version:
                # preferred (new)
                claim = release_to_claim(release, project, rank="preferred")
                change_summaries.append(f"Creating {release.version} (rank: preferred)")
                rank_changes.append(claim)
        if rank_changes:
            logger.info(f"Rank changes: {', '.join(change_summaries)}")
            await wikidata.save_claims(
                item.id, rank_changes, summary=wikidata.edit_summary
            )
