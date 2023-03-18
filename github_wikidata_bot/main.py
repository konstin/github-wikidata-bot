import argparse
import enum
import logging.config
import re
from distutils.version import LooseVersion
from typing import Any, Dict, List, Optional

import pywikibot
import requests
from pywikibot import Claim, ItemPage, WbTime
from pywikibot.data import sparql
from pywikibot.exceptions import APIError

from .github import Project, get_data_from_github
from .redirects import RedirectDict
from .settings import Settings
from .utils import github_repo_to_api, normalize_url, is_edit_conflict

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

    def new_claim(self, value: Any) -> Claim:
        """Builds a new claim for this property and the given target value."""
        claim = Claim(Settings.bot.repo, self.value)
        claim.setTarget(value)
        return claim

    def get_claim(self, item: ItemPage, target: Any) -> Optional[Claim]:
        """Returns an existing claim for this property and the given target value."""
        if self.value not in item.claims:
            return None
        all_claims: List[Claim] = item.claims.get(self.value, [])
        return next((c for c in all_claims if c.target_equals(target)), None)


def create_sources(
    url: str,
    retrieved: WbTime,
    title: Optional[str] = None,
    date: Optional[WbTime] = None,
) -> List[Claim]:
    """
    Gets or creates a `source` under the property `claim` to `url`
    """
    sources: List[Claim] = [
        Properties.reference_url.new_claim(url),
        Properties.retrieved.new_claim(retrieved),
    ]
    if title:
        text = pywikibot.WbMonolingualText(title, "en")
        sources.append(Properties.title.new_claim(text))
    if date:
        sources.append(Properties.publication_date.new_claim(date))
    return sources


def query_projects(
    project_filter: Optional[str] = None, ignore_blacklist: bool = False
) -> List[Dict[str, str]]:
    """
    Queries for all software projects and returns them as an array of simplified dicts
    :return: the data splitted into projects with and without github
    """
    wikidata_sparql = sparql.SparqlQuery()
    sparql_free_software_items = "".join(open(Settings.sparql_file).readlines())
    response = wikidata_sparql.select(sparql_free_software_items)

    projects = []
    logger.info("{} projects were found by the sparql query".format(len(response)))
    for project in response:
        if (
            project_filter
            and project_filter.lower() not in project["project"].lower()
            and project_filter.lower() not in project["projectLabel"].lower()
        ):
            continue
        if project["project"][31:] in Settings.blacklist and not ignore_blacklist:
            logger.info(
                f"{project['projectLabel']} ({project['project'][31:]}) is blacklisted"
            )
            continue

        if not Settings.repo_regex.match(project["repo"]):
            logger.info(
                " - Removing {}: {} {}".format(
                    project["projectLabel"], project["project"], project["repo"]
                )
            )
            continue

        projects.append(project)

    logger.info("{} projects remained after filtering".format(len(projects)))

    return projects


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

    logger.info("Normalizing {} to {}".format(url_raw, url_normalized))

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
        logger.info(
            "Multiple source code repositories for {} not supported".format(q_value)
        )
        return

    if urls[0].getTarget() != url_raw:
        logger.error(
            f"The url on the object ({urls[0].getTarget()}) doesn't match "
            f"the url from the sparql query ({url_raw}) for {q_value}"
        )
        return

    # See https://www.wikidata.org/wiki/User_talk:Konstin#Github-wiki-bot_is_replacing_source_code_repository_qualifiers_with_obsolete_qualifier
    # Add git as vcs and github as web ui
    git = ItemPage(Settings.bot.repo, "Q186055")
    github = ItemPage(Settings.bot.repo, "Q364")
    # Editing is in this case actually remove the old value and adding the new one
    claim = Properties.source_code_repository.new_claim(url_normalized)
    claim.addQualifier(Properties.vcs.new_claim(git))
    claim.addQualifier(Properties.web_interface_software.new_claim(github))
    claim.setSnakType("value")
    item.addClaim(claim, summary=Settings.edit_summary)

    item.removeClaims(urls[0], summary=Settings.edit_summary)


def set_website(project: Project) -> Optional[Claim]:
    """Add the website if does not already exist"""
    if not project.website or not project.website.startswith("http"):
        return

    url = RedirectDict.get_or_add(project.website) or project.website
    return Properties.official_website.new_claim(url)


def set_license(project: Project) -> Optional[Claim]:
    """Add the license if it does not already exist"""
    if not project.license or project.license not in Settings.licenses:
        return

    project_license = Settings.licenses[project.license]
    page = pywikibot.ItemPage(Settings.bot.repo, project_license)
    return Properties.license.new_claim(page)




def update_wikidata(project: Project):
    """Update wikidata entry with data from GitHub"""
    # Wikidata boilerplate
    q_value = project.project.replace("http://www.wikidata.org/entity/", "")
    item = ItemPage(Settings.bot.repo, title=q_value)
    item.get()

    url_raw = project.repo
    url_normalized = str(normalize_url(url_raw))
    if Settings.normalize_repo_url:
        normalize_repo_url(item, url_normalized, url_raw, q_value)

    for claim in (
        set_website(project),
        set_license(project),
    ):
        if not claim:
            continue
        claim.addSources(
            create_sources(
                url=github_repo_to_api(url_normalized),
                retrieved=project.retrieved,
            )
        )
        Settings.bot.user_add_claim_unless_exists(
            item, claim, exists_arg="", summary=Settings.edit_summary
        )

    # Add all stable releases
    stable_releases = project.stable_release
    stable_releases.sort(key=lambda x: LooseVersion(re.sub(r"[^0-9.]", "", x.version)))

    if len(stable_releases) == 0:
        logger.info("No stable releases")
        return

    versions = [i.version for i in stable_releases]
    if len(versions) != len(set(versions)):
        duplicates = [
            release
            for release in stable_releases
            if versions.count(release.version) > 1
        ]
        logger.warning(
            "There are duplicate releases in {}: {}".format(q_value, duplicates)
        )
        return

    latest_version: Optional[str] = stable_releases[-1].version

    existing_versions = item.claims.get(Properties.software_version, [])
    github_version_names = [i.version for i in stable_releases]

    for i in existing_versions:
        if i.getRank() == "preferred" and i.getTarget() not in github_version_names:
            logger.warning(
                "There's a preferred rank for {} for a version which is not in the github page: {}".format(
                    q_value, i.getTarget()
                )
            )
            latest_version = None

    if len(stable_releases) > 100:
        logger.warning(
            "Limiting {} to 100 of {} stable releases".format(
                q_value, len(stable_releases)
            )
        )
        stable_releases = stable_releases[-100:]
    else:
        logger.info("There are {} stable releases".format(len(stable_releases)))

    for release in stable_releases:
        existing = Properties.software_version.get_claim(item, release.version)
        if (
            existing
            and existing.getRank() == "preferred"
            and latest_version
            and release.version != latest_version
        ):
            logger.info("Setting normal rank for {}".format(existing.getTarget()))
            try:
                existing.changeRank("normal", summary=Settings.edit_summary)
            except APIError as e:
                if is_edit_conflict(e):
                    logger.error(
                        f"Edit conflict for setting the normal rank on {q_value}"
                    )
                    continue
                else:
                    raise

        claim = Properties.software_version.new_claim(release.version)
        claim.addQualifier(Properties.publication_date.new_claim(release.date))
        claim.addSources(
            create_sources(
                url=release.page,
                retrieved=project.retrieved,
                title="Release %s" % release.version,
                date=release.date,
            )
        )
        if latest_version and release.version == latest_version:
            logger.info("Setting preferred rank for {}".format(claim.getTarget()))
            claim.setRank("preferred")
        Settings.bot.user_add_claim_unless_exists(
            item,
            claim,
            # add when claim with same property, but not same target exists
            exists_arg="p",
            summary=Settings.edit_summary,
        )


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
    Settings.init_github(args.github_oauth_token)
    Settings.init_licenses()
    Settings.init_filter_lists()

    logger.info("# Querying Projects")
    projects = query_projects(args.filter, args.ignore_blacklist)
    logger.info("{} projects were found".format(len(projects)))

    logger.info("# Processing projects")
    for project in projects:
        logger.info("## " + project["projectLabel"] + ": " + project["project"])

        try:
            properties = get_data_from_github(project["repo"], project)
        except requests.exceptions.HTTPError as e:
            logger.error(
                "HTTP request for {} failed: {}".format(project["projectLabel"], e)
            )
            continue

        if Settings.do_update_wikidata:
            try:
                update_wikidata(properties)
            except Exception as e:
                logger.error("Failed to update {}: {}".format(properties.project, e))
                raise e

    logger.info("# Finished successfully")
