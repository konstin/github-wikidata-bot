import argparse
import enum
import logging.config
from typing import Any

import pywikibot
import requests
import sentry_sdk
from pywikibot import Claim, ItemPage, WbTime
from pywikibot.exceptions import APIError

from .github import Project, get_data_from_github
from .redirects import RedirectDict
from .settings import Settings
from .sparql import query_projects, WikidataProject
from .utils import (
    github_repo_to_api,
    normalize_url,
    is_edit_conflict,
    SimpleSortableVersion,
)

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

    def new_claim(self, value: Any) -> Claim:
        """Builds a new claim for this property and the given target value."""
        claim = Claim(Settings.bot.repo, self.value)
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
    title: str | None = None,
    date: WbTime | None = None,
) -> list[Claim]:
    """
    Gets or creates a `source` under the property `claim` to `url`
    """
    sources: list[Claim] = [
        Properties.reference_url.new_claim(url),
        Properties.retrieved.new_claim(retrieved),
    ]
    if title:
        text = pywikibot.WbMonolingualText(title, "en")
        sources.append(Properties.title.new_claim(text))
    if date:
        sources.append(Properties.publication_date.new_claim(date))
    return sources


def normalize_repo_url(
    item: ItemPage,
    url_normalized: str,
    url_raw: str,
    q_value: str,
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
            item.removeClaims(urls[1], summary=Settings.edit_summary)
            return
        if urls[0].getTarget() == url_raw and urls[1].getTarget() == url_normalized:
            logger.info("The old and the new url are already set, removing the old")
            item.removeClaims(urls[0], summary=Settings.edit_summary)
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
        git = ItemPage(Settings.bot.repo, "Q186055")
        url_claim.addQualifier(Properties.vcs.new_claim(git))
    if Properties.web_interface_software.value not in url_claim.qualifiers:
        github = ItemPage(Settings.bot.repo, "Q364")
        url_claim.addQualifier(Properties.web_interface_software.new_claim(github))


def set_website(project: Project) -> Claim | None:
    """Add the website if does not already exist"""
    if not project.website or not project.website.startswith("http"):
        return None

    url = RedirectDict.get_or_add(project.website) or project.website
    return Properties.official_website.new_claim(url)


def set_license(project: Project) -> Claim | None:
    """Add the license if it does not already exist"""
    if not project.license or project.license not in Settings.licenses:
        return None

    project_license = Settings.licenses[project.license]
    page = pywikibot.ItemPage(Settings.bot.repo, project_license)
    return Properties.license.new_claim(page)


@sentry_sdk.trace
def update_wikidata(project: Project):
    """Update wikidata entry with data from GitHub"""
    # Wikidata boilerplate
    q_value = project.project.replace("http://www.wikidata.org/entity/", "")
    with sentry_sdk.start_span(description="Get item page"):
        item = ItemPage(Settings.bot.repo, title=q_value)
        item.get()

    urls = item.claims[Properties.source_code_repository.value]
    if len(urls) == 1:
        url_raw = urls[0].target
        url_normalized = str(normalize_url(url_raw))
        if Settings.normalize_repo_url:
            normalize_repo_url(item, url_normalized, url_raw, q_value)
    else:
        url_raw = project.repo
        url_normalized = str(normalize_url(url_raw))

    for claim, claim_kind in [
        (set_website(project), "website"),
        (set_license(project), "license"),
    ]:
        if not claim:
            continue
        claim.addSources(
            create_sources(
                url=github_repo_to_api(url_normalized), retrieved=project.retrieved
            )
        )
        with sentry_sdk.start_span(description=f"Set {claim_kind}"):
            Settings.bot.user_add_claim_unless_exists(
                item, claim, exists_arg="", summary=Settings.edit_summary
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
        logger.info(
            f"There are duplicate releases in {q_value}: {', '.join(duplicates)}"
        )
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

    if len(stable_releases) > 100:
        logger.info(
            f"Limiting {q_value} to 100 of {len(stable_releases)} stable releases"
        )
        stable_releases = stable_releases[-100:]
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
                    existing.changeRank("normal", summary=Settings.edit_summary)
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

        claim = Properties.software_version.new_claim(release.version)
        claim.addQualifier(Properties.publication_date.new_claim(release.date))
        stable_release = ItemPage(Settings.bot.repo, "Q2804309")
        claim.addQualifier(Properties.version_type.new_claim(stable_release))
        claim.addSources(
            create_sources(
                url=release.page,
                retrieved=project.retrieved,
                title=f"Release {release.version}",
                date=release.date,
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
            added = Settings.bot.user_add_claim_unless_exists(
                item,
                claim,
                # add when claim with same property, but not same target exists
                exists_arg="p",
                summary=Settings.edit_summary,
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
                existing.changeRank("preferred", summary=Settings.edit_summary)


@sentry_sdk.trace
def update_project(project: WikidataProject):
    logger.info(f"## {project.projectLabel}: {project.project}")
    try:
        properties = get_data_from_github(project.repo, project)
    except requests.exceptions.HTTPError as e:
        logger.error(
            f"Github API request for {project.projectLabel} ({project.wikidata_id}) failed: {e}",
            exc_info=True,
        )
        return

    if Settings.do_update_wikidata:
        try:
            update_wikidata(properties)
        except Exception as e:
            logger.error(f"Failed to update {properties.project}: {e}", exc_info=True)
            raise


def run(project_filter: str | None, ignore_blacklist: bool):
    logger.info("# Querying Projects")
    projects = query_projects(project_filter, ignore_blacklist)
    logger.info(f"{len(projects)} projects were found")
    logger.info("# Processing projects")
    for project in projects:
        with sentry_sdk.start_transaction(name="Update project"):
            update_project(project)
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

    Settings.init_logging(args.quiet, args.debug_http)
    Settings.init_config(args.github_oauth_token)
    Settings.init_licenses()
    Settings.init_filter_lists()

    run(args.filter, args.ignore_blacklist)
