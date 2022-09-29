#!/usr/bin/env python3
import argparse
import json
import logging.config
import os
import random
import re
from dataclasses import dataclass
from distutils.version import LooseVersion
from json.decoder import JSONDecodeError
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import pywikibot
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache
from cachecontrol.heuristics import ExpiresAfter

from pywikibot import Claim, ItemPage, WbTime
from pywikibot.data import sparql
from requests import HTTPError, RequestException

from utils import (
    parse_filter_list,
    github_repo_to_api,
    github_repo_to_api_releases,
    github_repo_to_api_tags,
    normalize_url,
)
from versionhandler import extract_version

logger = logging.getLogger(__name__)


class Settings:
    do_update_wikidata = True

    # Read also tags if a project doesn't use github's releases
    read_tags = True

    normalize_repo_url = True

    blacklist_page = "User:Github-wiki-bot/Exceptions"
    whitelist_page = "User:Github-wiki-bot/Whitelist"
    blacklist: List[str] = []
    whitelist: List[str] = []
    sparql_file = "free_software_items.rq"

    license_sparql_file = "free_licenses.rq"
    licenses: Dict[str, str] = {}

    # pywikibot doesn't cache the calendar model, so let's do this manually
    calendar_model = pywikibot.Site().data_repository().calendarmodel()
    wikidata_repo = pywikibot.Site("wikidata", "wikidata").data_repository()

    repo_regex = re.compile(r"^[a-z]+://github.com/[^/]+/[^/]+/?$")

    cached_session: requests.Session = CacheControl(
        requests.Session(), cache=FileCache("cache"), heuristic=ExpiresAfter(days=30)
    )


@dataclass
class Properties:
    software_version = "P348"
    publication_date = "P577"
    retrieved = "P813"
    reference_url = "P854"
    official_website = "P856"
    source_code_repository = "P1324"
    title = "P1476"
    protocol = "P2700"
    license = "P275"


class RedirectDict:
    _redirect_dict: Dict[str, str] = {}

    @classmethod
    def get_or_add(cls, start_url: str) -> Optional[str]:
        if not cls._redirect_dict:
            cls._load()
        if start_url in cls._redirect_dict:
            return cls._redirect_dict[start_url]
        else:
            try:
                response = requests.head(start_url, allow_redirects=True, timeout=6.1)
            except RequestException:
                return None
            end_url = response.url
            cls._redirect_dict[start_url] = end_url
            cls._save()
            return end_url

    @classmethod
    def _load(cls):
        if os.path.isfile("redirects.json"):
            with open("redirects.json") as fp:
                cls._redirect_dict = json.load(fp)
        else:
            cls._redirect_dict = dict()

    @classmethod
    def _save(cls):
        with open("redirect.json", "w") as fp:
            json.dump(cls._redirect_dict, fp)


@dataclass
class Release:
    version: str
    date: WbTime
    page: str
    release_type: str


@dataclass
class ReleaseTag:
    version: str
    page: str
    release_type: str
    tag_url: str
    tag_type: str


@dataclass
class Project:
    project: str
    stable_release: List[Release]
    website: Optional[str]
    license: Optional[str]
    repo: str
    retrieved: WbTime


def get_filter_list(page_title: str) -> List[str]:
    site = pywikibot.Site()
    page = pywikibot.Page(site, page_title)
    return parse_filter_list(page.text)


def string_to_wddate(iso_timestamp: str) -> WbTime:
    """
    Create a wikidata compatible wikibase date from an ISO 8601 timestamp
    """
    date = WbTime.fromTimestr(iso_timestamp, calendarmodel=Settings.calendar_model)
    date.hour = 0
    date.minute = 0
    date.second = 0
    date.precision = WbTime.PRECISION["day"]
    return date


def get_summary(edit_group_hash: str) -> str:
    """https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots"""
    return f"Update with GitHub data ([[:toollabs:editgroups/b/CB/{edit_group_hash}|details]])"


def get_or_create_claim(
    item: ItemPage, p_value: str, value: Any, edit_group_hash: str
) -> Tuple[Claim, bool]:
    """
    Gets or creates a claim with `value` under the property `p_value` to `item`
    """
    all_claims = item.claims.get(p_value, [])

    for claim in all_claims:
        if claim.target_equals(value):
            return claim, False

    claim = Claim(Settings.wikidata_repo, p_value)
    claim.setTarget(value)
    item.addClaim(claim, summary=get_summary(edit_group_hash))

    return claim, True


def get_or_create_qualifiers(
    claim: Claim, p_value: str, value: Any, edit_group_hash: str
) -> Claim:
    """
    Gets or creates a `qualifier` under the property `p_value` to `claim`
    """
    all_qualifiers = claim.qualifiers.get(p_value, [])

    for qualifier in all_qualifiers:
        if qualifier.target_equals(value):
            break
    else:
        qualifier = Claim(Settings.wikidata_repo, p_value)
        qualifier.setTarget(value)
        summary = get_summary(edit_group_hash)
        claim.addQualifier(qualifier, summary=summary)

    return qualifier


def get_or_create_sources(
    claim: Claim,
    url: str,
    retrieved,
    edit_group_hash: str,
    title: Optional[str] = None,
    date: Optional[WbTime] = None,
):
    """
    Gets or creates a `source` under the property `claim` to `url`
    """
    all_sources = []

    src_p = Properties.reference_url

    for i in claim.sources or []:
        if src_p in i:
            all_sources.append(i[src_p][0])

    for src_url in all_sources:
        if src_url.target_equals(url):
            break
    else:
        src_url = Claim(Settings.wikidata_repo, src_p)
        src_url.setTarget(url)
        src_retrieved = Claim(Settings.wikidata_repo, Properties.retrieved)
        src_retrieved.setTarget(retrieved)

        sources = [src_url, src_retrieved]

        if title:
            src_title = Claim(Settings.wikidata_repo, Properties.title)
            src_title.setTarget(pywikibot.WbMonolingualText(title, "en"))
            sources.append(src_title)
        if date:
            src_date = Claim(Settings.wikidata_repo, Properties.publication_date)
            src_date.setTarget(date)
            sources.append(src_date)
        claim.addSources(sources, summary=get_summary(edit_group_hash))

    return src_url


def get_json_cached(url: str) -> dict:
    """
    Get JSON from an API and cache the result
    """
    response = Settings.cached_session.get(url)
    response.raise_for_status()
    try:
        return response.json()
    except JSONDecodeError as e:
        logger.error("JSONDecodeError for {}: {}".format(url, e))
        return {}


def query_projects(
    project_filter: Optional[str] = None, ignore_blacklist: bool = False
) -> List[Dict[str, str]]:
    """
    Queries for all software projects and returns them as an array of simplified dicts
    :return: the data splitted into projects with and without github
    """
    wikidata_sparql = sparql.SparqlQuery()
    sparql_free_software_items = "".join(open(Settings.sparql_file).readlines())
    response = wikidata_sparql.query(sparql_free_software_items)

    projects = []
    logger.info(
        "{} projects were found by the sparql query".format(
            len(response["results"]["bindings"])
        )
    )
    for project in response["results"]["bindings"]:
        # Remove bloating type information

        project = {
            "projectLabel": project["projectLabel"]["value"],
            "project": project["project"]["value"],
            "repo": project["repo"]["value"],
        }

        if (
            project_filter
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


def get_all_pages(url: str) -> List[dict]:
    """Gets all pages of the release/tag information"""
    page_number = 1
    results: List[dict] = []
    while True:
        page = get_json_cached(url + "?page=" + str(page_number))
        if not page:
            break
        page_number += 1
        results += page
    return results


def analyse_release(release: dict, project_info: dict) -> Optional[Release]:
    """
    Heuristics to find the version number and according meta-data for a release
    marked with github's release-feature
    """
    project_name = project_info["name"]
    match_tag_name = extract_version(release.get("tag_name") or "", project_name)
    match_name = extract_version(release.get("name") or "", project_name)
    if (
        match_tag_name is not None
        and match_name is not None
        and match_tag_name != match_name
    ):
        logger.warning(
            "Conflicting versions {} and {} for {} and {} in {}".format(
                match_tag_name,
                match_name,
                release["tag_name"],
                release["name"],
                project_name,
            )
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
        logger.info("Diverting release type: " + original_version)
        release["prerelease"] = True
    elif release["prerelease"] and release_type == "stable":
        release_type = "unstable"

    # Convert github's timestamps to wikidata dates
    date = string_to_wddate(release["published_at"])

    return Release(
        version=version, date=date, page=release["html_url"], release_type=release_type
    )


def analyse_tag(
    release: dict, project_info: dict, invalid_version_strings: List[str]
) -> Optional[ReleaseTag]:
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
    html_url = project_info["html_url"] + "/releases/tag/" + quote_plus(tag_name)

    return ReleaseTag(
        version=version,
        page=html_url,
        release_type=release_type,
        tag_type=tag_type,
        tag_url=tag_url,
    )


def get_date_from_tag_url(release: ReleaseTag) -> Optional[Release]:
    tag_details = get_json_cached(release.tag_url)
    if release.tag_type == "tag":
        # For some weird reason the api might not always have a date
        if not tag_details["tagger"]["date"]:
            logger.warning("No tag date for {}".format(release.tag_url))
            return None
        date = string_to_wddate(tag_details["tagger"]["date"])
    elif release.tag_type == "commit":
        if not tag_details["committer"]["date"]:
            logger.warning("No tag date for {}".format(release.tag_url))
            return None
        date = string_to_wddate(tag_details["committer"]["date"])
    else:
        raise NotImplementedError(f"Unknown type of tag: {release.tag_type}")

    return Release(
        version=release.version,
        release_type=release.release_type,
        date=date,
        page=release.page,
    )


def get_data_from_github(url: str, properties: Dict[str, str]) -> Project:
    """
    Retrieve the following data from github:
     - website / homepage
     - version number string and release date of all stable releases

    Version marked with github's own release-function are received primarily.
    Only if a project has none releases marked that way this function will fall
    back to parsing the tags of the project.

    All data is preprocessed, i.e. the version numbers are extracted and
    unmarked prereleases are discovered

    :param url: The url of the github repository
    :param properties: The already gathered information
    :return: dict of dicts
    """
    # "retrieved" does only accept dates without time, so create a timestamp with no date
    iso_timestamp = pywikibot.Timestamp.utcnow().isoformat()
    retrieved = string_to_wddate(iso_timestamp)

    # General project information
    project_info = get_json_cached(github_repo_to_api(url))

    if project_info.get("homepage"):
        website = project_info["homepage"]
    else:
        website = None

    if project_info.get("license"):
        spdx_id = project_info["license"]["spdx_id"]
    else:
        spdx_id = None

    api_url = github_repo_to_api_releases(url)
    q_value = properties["project"].replace("http://www.wikidata.org/entity/", "")
    releases = get_all_pages(api_url)

    invalid_releases = []
    extracted: List[Optional[Release]] = []
    for release in releases:
        result = analyse_release(release, project_info)
        if result:
            extracted.append(result)
        else:
            invalid_releases.append((release["tag_name"], release["name"]))

    if invalid_releases:
        logger.warning(
            f"{len(invalid_releases)} invalid releases: {invalid_releases[:10]}"
        )

    if Settings.read_tags and (len(extracted) == 0 or q_value in Settings.whitelist):
        logger.info("Falling back to tags")
        api_url = github_repo_to_api_tags(url)
        try:
            tags = get_json_cached(api_url)
        except HTTPError as e:
            # Github raises a 404 if there are no tags
            if e.response.status_code == 404:
                tags = {}
            else:
                raise e

        invalid_version_strings: List[str] = []
        extracted_tags = [
            analyse_tag(release, project_info, invalid_version_strings)
            for release in tags
        ]
        filtered = [v for v in extracted_tags if v is not None]
        filtered.sort(key=lambda x: LooseVersion(re.sub(r"[^0-9.]", "", x.version)))
        if len(filtered) > 300:
            logger.warning(
                "Limiting {} to 300 of {} tags for performance reasons.".format(
                    q_value, len(filtered)
                )
            )
            filtered = filtered[-300:]
        extracted = list(map(get_date_from_tag_url, filtered))
        if invalid_version_strings:
            logger.warning(
                f"Invalid version strings in tags of {q_value}: {invalid_version_strings}"
            )

    stable_release = []
    for extract in extracted:
        if extract and extract.release_type == "stable":
            stable_release.append(extract)

    return Project(
        stable_release=stable_release,
        website=website,
        license=spdx_id,
        retrieved=retrieved,
        repo=properties["repo"],
        project=properties["project"],
    )


def normalize_repo_url(
    item: ItemPage,
    url_normalized: str,
    url_raw: str,
    q_value: str,
    edit_group_hash: str,
):
    """Canonicalize the github url
    This use the format https://github.com/[owner]/[repo]

    Note: This apparently only works with a bot account
    """
    if url_raw == url_normalized:
        return

    logger.info("Normalizing {} to {}".format(url_raw, url_normalized))

    source_p = Properties.source_code_repository
    urls = item.claims[source_p]
    if source_p in item.claims and len(urls) == 2:
        if urls[0].getTarget() == url_normalized and urls[1].getTarget() == url_raw:
            logger.info("The old and the new url are already set, removing the old")
            item.removeClaims(urls[1], summary=get_summary(edit_group_hash))
            return
        if urls[0].getTarget() == url_raw and urls[1].getTarget() == url_normalized:
            logger.info("The old and the new url are already set, removing the old")
            item.removeClaims(urls[0], summary=get_summary(edit_group_hash))
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

    # Editing is in this case actually remove the old value and adding the new one
    claim = Claim(Settings.wikidata_repo, source_p)
    claim.setTarget(url_normalized)
    claim.setSnakType("value")
    item.addClaim(claim, summary=get_summary(edit_group_hash))
    item.removeClaims(urls[0], summary=get_summary(edit_group_hash))
    # Add git as protocol
    git = ItemPage(Settings.wikidata_repo, "Q186055")
    get_or_create_qualifiers(claim, Properties.protocol, git, edit_group_hash)


def set_claim_rank(
    claim: Claim, latest_version: Optional[str], release: Release, edit_group_hash: str
):
    if latest_version is None:
        return
    if release.version == latest_version:
        if claim.getRank() == "normal":
            logger.info("Setting preferred rank for {}".format(claim.getTarget()))
            claim.changeRank("preferred", summary=get_summary(edit_group_hash))
    else:
        if claim.getRank() == "preferred":
            logger.info("Setting normal rank for {}".format(claim.getTarget()))
            claim.changeRank("normal", summary=get_summary(edit_group_hash))


def set_website(
    item: ItemPage, project: Project, url_normalized: str, edit_group_hash: str
):
    """Add the website if does not already exists"""
    if not project.website or not project.website.startswith("http"):
        return

    # There's a single value constraint on official website (which is not yet reflected below)
    if Properties.official_website in item.claims:
        return

    redirected = RedirectDict.get_or_add(project.website)

    websites = [x.getTarget() for x in item.claims.get(Properties.official_website, [])]
    if project.website in websites or redirected in websites:
        return

    url = redirected or project.website

    claim, created = get_or_create_claim(
        item, Properties.official_website, url, edit_group_hash
    )
    if created:
        logger.info("Added the website: {}".format(url))
    get_or_create_sources(
        claim, github_repo_to_api(url_normalized), project.retrieved, edit_group_hash
    )


def set_license(
    item: ItemPage, project: Project, url_normalized: str, edit_group_hash: str
):
    """Add the license if does not already exists"""
    if project.license and Properties.license not in item.claims:
        if project.license in Settings.licenses:
            project_license = Settings.licenses[project.license]
            claim, created = get_or_create_claim(
                item,
                Properties.license,
                pywikibot.ItemPage(Settings.wikidata_repo, project_license),
                edit_group_hash,
            )
            if created:
                logger.info("Added the license: {}".format(project_license))
            get_or_create_sources(
                claim,
                github_repo_to_api(url_normalized),
                project.retrieved,
                edit_group_hash,
            )


def update_wikidata(project: Project, edit_group_hash: str):
    """Update wikidata entry with data from github"""
    # Wikidata boilerplate
    wikidata = Settings.wikidata_repo
    q_value = project.project.replace("http://www.wikidata.org/entity/", "")
    item = ItemPage(wikidata, title=q_value)
    item.get()

    url_raw = project.repo
    url_normalized = str(normalize_url(url_raw))
    if Settings.normalize_repo_url:
        normalize_repo_url(item, url_normalized, url_raw, q_value, edit_group_hash)

    set_website(item, project, url_normalized, edit_group_hash)
    set_license(item, project, url_normalized, edit_group_hash)

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
        claim, created = get_or_create_claim(
            item, Properties.software_version, release.version, edit_group_hash
        )
        if created:
            logger.info("Added '{}'".format(release.version))

        # Assumption: A preexisting publication date is more reliable than the one from github
        date_p = Properties.publication_date
        if date_p not in claim.qualifiers:
            get_or_create_qualifiers(claim, date_p, release.date, edit_group_hash)

        title = "Release %s" % release.version
        get_or_create_sources(
            claim, release.page, project.retrieved, edit_group_hash, title, release.date
        )

        # Give the latest release the preferred rank
        # And work around a bug in pywikibot
        try:
            set_claim_rank(claim, latest_version, release, edit_group_hash)
        except AssertionError:
            logger.warning(
                f"Using the fallback for setting the preferred rank of {q_value}"
            )

            item.get(force=True)

            claim, created = get_or_create_claim(
                item, Properties.software_version, release.version, edit_group_hash
            )
            assert not created
            set_claim_rank(claim, latest_version, release, edit_group_hash)


def configure_logging(quiet: bool, http_debug: bool):
    """
    In cron jobs you do not want logging to stdout / stderr,
    therefore the quiet option allows disabling that.
    """
    if quiet:
        handlers = ["all", "error"]
    else:
        handlers = ["console", "all", "error"]

    conf = {
        "version": 1,
        "formatters": {"extended": {"format": "%(levelname)-8s %(message)s"}},
        "handlers": {
            "console": {"class": "logging.StreamHandler"},
            "all": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": "all.log",
                "formatter": "extended",
                "maxBytes": 8 * 1024 * 1024,
                "backupCount": 2,
            },
            "error": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": "error.log",
                "formatter": "extended",
                "level": "WARN",
                "maxBytes": 8 * 1024 * 1024,
                "backupCount": 2,
            },
        },
        "loggers": {__name__: {"handlers": handlers, "level": "INFO"}},
    }

    logging.config.dictConfig(conf)

    if http_debug:
        from http.client import HTTPConnection

        HTTPConnection.debuglevel = 1

        requests_log = logging.getLogger("urllib3")
        requests_log.setLevel(logging.DEBUG)
        requests_log.propagate = True


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

    configure_logging(args.quiet, args.debug_http)
    # https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots
    edit_group_hash = "{:x}".format(random.randrange(0, 2 ** 48))

    if args.github_oauth_token:
        github_oath_token = args.github_oauth_token
    else:
        with open("config.json") as config:
            github_oath_token = json.load(config)["github-oauth-token"]
    Settings.cached_session.headers.update(
        {"Authorization": "token " + github_oath_token}
    )

    sparql_license_items = "".join(open(Settings.license_sparql_file).readlines())
    response = sparql.SparqlQuery().query(sparql_license_items)
    Settings.licenses = {
        row["spdx"]["value"]: row["license"]["value"][31:]
        for row in response["results"]["bindings"]
    }

    Settings.blacklist = get_filter_list(Settings.blacklist_page)
    Settings.whitelist = get_filter_list(Settings.whitelist_page)

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
                update_wikidata(properties, edit_group_hash)
            except Exception as e:
                logger.error("Failed to update {}: {}".format(properties.project, e))
                continue

    logger.info("# Finished successfully")


if __name__ == "__main__":
    main()
