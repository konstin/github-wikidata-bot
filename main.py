#!/usr/bin/env python3
import argparse
import json
import logging.config
import os
import re
from distutils.version import LooseVersion
from json.decoder import JSONDecodeError
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import pywikibot
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache
from dataclasses import dataclass

# noinspection PyProtectedMember
from pywikibot import Claim, ItemPage, WbTime
from pywikibot.data import sparql
from requests import HTTPError

from versionhandler import extract_version

logger = logging.getLogger(__name__)


class Settings:
    do_update_wikidata = True

    # Read also tags if a project doesn't use githubs releases
    read_tags = True

    normalize_repo_url = True

    blacklist_page = "User:Github-wiki-bot/Exceptions"
    whitelist_page = "User:Github-wiki-bot/Whitelist"
    blacklist: List[str] = []
    whitelist: List[str] = []
    sparql_file = "free_software_items.rq"

    # pywikibot is too stupid to cache the calendar model, so let's do this manually
    calendarmodel = pywikibot.Site().data_repository().calendarmodel()
    wikidata_repo = pywikibot.Site("wikidata", "wikidata").data_repository()

    repo_regex = re.compile(r"^[a-z]+://github.com/[^/]+/[^/]+/?$")

    cached_session: requests.Session = CacheControl(
        requests.Session(), cache=FileCache("cache")
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


class RedirectDict:
    _redirect_dict: Dict[str, str] = None

    @classmethod
    def get_or_add(cls, start_url: str) -> Optional[str]:
        if not cls._redirect_dict:
            cls._load()
        if start_url in cls._redirect_dict:
            return cls._redirect_dict[start_url]
        else:
            try:
                response = requests.head(start_url, allow_redirects=True)
            except HTTPError:
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
class Project:
    project: str
    stable_release: List[Release]
    website: Optional[str]
    repo: str
    retrieved: WbTime


def get_filter_list(pagetitle: str) -> List[str]:
    site = pywikibot.Site()
    page = pywikibot.Page(site, pagetitle)
    text = page.text
    r = re.compile(r"Q\d+")
    filterlist = []
    for line in text.split():
        if len(line) > 0 and r.fullmatch(line):
            filterlist.append(line)
    return filterlist


def github_repo_to_api(url: str) -> str:
    """Converts a github repository url to the api entry with the general information"""
    url = normalize_url(url)
    url = url.replace("https://github.com/", "https://api.github.com/repos/")
    return url


def github_repo_to_api_releases(url: str) -> str:
    """Converts a github repository url to the api entry with the releases"""
    url = github_repo_to_api(url)
    url += "/releases"
    return url


def github_repo_to_api_tags(url: str) -> str:
    """Converts a github repository url to the api entry with the tags"""
    url = github_repo_to_api(url)
    url += "/git/refs/tags"
    return url


def normalize_url(url: str) -> str:
    """
    Canonical urls be like: https, no slash, no file extension

    :param url:
    :return:
    """
    url = url.strip("/")
    url = "https://" + url.split("://")[1]
    if url.endswith(".git"):
        url = url[:-4]
    return url


def string_to_wddate(isotimestamp: str) -> WbTime:
    """
    Create a wikidata compatible wikibase date from an ISO 8601 timestamp
    """
    date = WbTime.fromTimestr(isotimestamp, calendarmodel=Settings.calendarmodel)
    date.hour = 0
    date.minute = 0
    date.second = 0
    date.precision = WbTime.PRECISION["day"]
    return date


def get_or_create_claim(item: ItemPage, p_value: str, value: Any) -> Tuple[Claim, bool]:
    """
    Gets or creates a claim with `value` under the property `p_value` to `item`
    """
    all_claims = item.claims.get(p_value, [])

    for claim in all_claims:
        if claim.target_equals(value):
            return claim, False

    claim = Claim(Settings.wikidata_repo, p_value)
    claim.setTarget(value)
    item.addClaim(claim)

    return claim, True


def get_or_create_qualifiers(claim: Claim, p_value: str, value: Any) -> Claim:
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
        claim.addQualifier(qualifier)

    return qualifier


def get_or_create_sources(
    claim: Claim,
    url: str,
    retrieved,
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
        claim.addSources(sources)

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


def query_projects(project_filter: Optional[str] = None) -> List[Dict[str, str]]:
    """
    Queries for all software projects and returns them as an array of simplified dicts
    :return: the data splitted into projects with and without github
    """
    wikdata_sparql = sparql.SparqlQuery()
    sparql_free_software_items = "".join(open(Settings.sparql_file).readlines())
    response = wikdata_sparql.query(sparql_free_software_items)

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
        if project["project"][31:] in Settings.blacklist:
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
    """ Gets all pages of the release/tag information """
    page_number = 1
    results = []
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
    marked with githubs release-feature
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
            "Conflicting versions {} and {} for {} and {}".format(
                match_tag_name, match_name, release["tag_name"], release["name"]
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
        logger.warning(
            "Invalid version strings '{}' and '{}'".format(
                release["tag_name"], release["name"]
            )
        )
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


def analyse_tag(release: dict, project_info: dict) -> Optional[Release]:
    """
    Heuristics to find the version number and according meta-data for a release
    not marked with githubs release-feature but tagged with git.

    Compared to analyse_release this needs an extra API-call which makes this
    function considerably slower.
    """
    project_name = project_info["name"]
    tag_name = release.get("ref", "refs/tags/")[10:]
    match_name = extract_version(tag_name, project_name)
    if match_name is not None:
        release_type, version = match_name
    else:
        logger.warning("Invalid version string '{}'".format(tag_name))
        return None

    tag_type = release["object"]["type"]
    tag_url = release["object"]["url"]
    tag_details = get_json_cached(tag_url)
    if tag_type == "tag":
        # For some weird reason the api might not always have a date
        if not tag_details["tagger"]["date"]:
            logger.warning("No tag date for {} {}".format(tag_name, tag_url))
            return None
        date = string_to_wddate(tag_details["tagger"]["date"])
    elif tag_type == "commit":
        if not tag_details["committer"]["date"]:
            logger.warning("No tag date for {} {}".format(tag_name, tag_url))
            return None
        date = string_to_wddate(tag_details["committer"]["date"])
    else:
        raise NotImplementedError("Unknown type of tag: %s" % tag_type)
    html_url = project_info["html_url"] + "/releases/tag/" + quote_plus(tag_name)

    return Release(version=version, date=date, page=html_url, release_type=release_type)


def get_data_from_github(url: str, properties: Dict[str, str]) -> Project:
    """
    Retrieve the following data from github:
     - website / homepage
     - version number string and release date of all stable releases

    Version marked with githubs own release-function are received primarily.
    Only if a project has none releases marked that way this function will fall
    back to parsing the tags of the project.

    All data is preprocessed, i.e. the version numbers are extracted and
    unmarked prereleases are discovered

    :param url: The url of the github repository
    :param properties: The already gathered information
    :return: dict of dicts
    """
    # "retrieved" does only accept dates without time, so create a timestamp with no date
    # noinspection PyUnresolvedReferences
    isotimestamp = pywikibot.Timestamp.utcnow().toISOformat()
    retrieved = string_to_wddate(isotimestamp)

    # General project information
    project_info = get_json_cached(github_repo_to_api(url))

    if project_info.get("homepage"):
        website = project_info["homepage"]
    else:
        website = None

    if project_info.get("license"):
        properties["license"] = project_info["license"]["spdx_id"]
    apiurl = github_repo_to_api_releases(url)
    q_value = properties["project"].replace("http://www.wikidata.org/entity/", "")
    releases = get_all_pages(apiurl)

    extracted = [analyse_release(release, project_info) for release in releases]
    if Settings.read_tags and (len(releases) == 0 or q_value in Settings.whitelist):
        logger.info("Falling back to tags")
        apiurl = github_repo_to_api_tags(url)
        try:
            releases = get_json_cached(apiurl)
        except HTTPError as e:
            # Gitub raises a 404 if there are no releases
            if e.response.status_code == 404:
                releases = []
            else:
                raise e
        if len(releases) > 300:
            logger.warning(
                "To many tags ({}), skipping for performance reasons.".format(
                    len(releases)
                )
            )
            releases = []
        extracted = [analyse_tag(release, project_info) for release in releases]

    stable_release = []
    for extract in extracted:
        if extract and extract.release_type == "stable":
            stable_release.append(extract)

    return Project(
        stable_release=stable_release,
        website=website,
        retrieved=retrieved,
        repo=properties["repo"],
        project=properties["project"],
    )


def normalize_repo_url(item: ItemPage, url_normalized: str, url_raw: str, q_value: str):
    """ Canonicalize the github url
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
            item.removeClaims(urls[1])
            return
        if urls[0].getTarget() == url_raw and urls[1].getTarget() == url_normalized:
            logger.info("The old and the new url are already set, removing the old")
            item.removeClaims(urls[0])
            return

    if source_p in item.claims and len(urls) > 1:
        logger.info(
            "Multiple source code repositories for {} not supported".format(q_value)
        )
        return

    if urls[0].getTarget() != url_raw:
        logger.error(
            "The url on the object doesn't match the url from the sparql query "
            + q_value
        )
        return

    # Editing is in this case actually remove the old value and adding the new one
    claim = Claim(Settings.wikidata_repo, source_p)
    claim.setTarget(url_normalized)
    claim.setSnakType("value")
    item.addClaim(claim)
    item.removeClaims(urls[0])
    # Add git as protocol
    git = ItemPage(Settings.wikidata_repo, "Q186055")
    get_or_create_qualifiers(claim, Properties.protocol, git)


def set_claim_rank(claim: Claim, latest_version: str, release: Release):
    if latest_version is None:
        return
    if release.version == latest_version:
        if claim.getRank() != "preferred":
            logger.info("Setting prefered rank for {}".format(claim.getTarget()))
            claim.changeRank("preferred")
    else:
        if claim.getRank() != "normal":
            logger.info("Setting normal rank for {}".format(claim.getTarget()))
            claim.changeRank("normal")


def set_website(item, properties, url_normalized):
    """ Add the website if does not already exists """
    if not properties.website or not properties.website.startswith("http"):
        return

    redirected = RedirectDict.get_or_add(properties.website)

    websites = [x.getTarget() for x in item.claims[Properties.official_website]]
    if properties.website in websites or redirected in websites:
        return

    url = redirected or properties.website
    print(url, redirected, properties.website)

    claim, created = get_or_create_claim(item, Properties.official_website, url)
    if created:
        logger.info("Added the website: {}".format(url))
    get_or_create_sources(
        claim, github_repo_to_api(url_normalized), properties.retrieved
    )


def update_wikidata(properties: Project):
    """ Update wikidata entry with data from github """
    # Wikidata boilerplate
    wikidata = Settings.wikidata_repo
    q_value = properties.project.replace("http://www.wikidata.org/entity/", "")
    item = ItemPage(wikidata, title=q_value)
    item.get()

    url_raw = properties.repo
    url_normalized = normalize_url(url_raw)
    if Settings.normalize_repo_url:
        normalize_repo_url(item, url_normalized, url_raw, q_value)

    set_website(item, properties, url_normalized)

    # Add all stable releases
    stable_releases = properties.stable_release
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
        logger.error(
            "There are duplicate releases in {}: {}".format(q_value, duplicates)
        )
        return

    latest_version = stable_releases[-1].version

    existing_versions = item.claims.get(Properties.software_version, [])
    github_version_names = [i.version for i in stable_releases]

    for i in existing_versions:
        if i.getRank() == "preferred" and i.getTarget() not in github_version_names:
            logger.warning(
                "There's a preferred rank for a version which is not in the github page: {}".format(
                    i.getTarget()
                )
            )
            latest_version = None

    if len(stable_releases) > 100:
        logger.warning(
            "Limiting to 100 stable releases of {}".format(len(stable_releases))
        )
        stable_releases = stable_releases[-100:]
    else:
        logger.info("There are {} stable releases".format(len(stable_releases)))

    for release in stable_releases:
        claim, created = get_or_create_claim(
            item, Properties.software_version, release.version
        )
        if created:
            logger.info("Added '{}'".format(release.version))

        # Assumption: A preexisting publication date is more reliable than the one from github
        date_p = Properties.publication_date
        if date_p not in claim.qualifiers:
            get_or_create_qualifiers(claim, date_p, release.date)

        title = "Release %s" % release.version
        get_or_create_sources(
            claim, release.page, properties.retrieved, title, release.date
        )

        # Give the latest release the preferred rank
        # And work around a bug in pywikibot
        try:
            set_claim_rank(claim, latest_version, release)
        except AssertionError:
            logger.warning("Using the fallback for setting the preferred rank")

            item.get(force=True)

            claim, created = get_or_create_claim(
                item, Properties.software_version, release.version
            )
            assert not created
            set_claim_rank(claim, latest_version, release)


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
                "class": "logging.FileHandler",
                "filename": "all.log",
                "formatter": "extended",
            },
            "error": {
                "class": "logging.FileHandler",
                "filename": "error.log",
                "formatter": "extended",
                "level": "WARN",
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
    parser.add_argument(
        "--quiet", action="store_true", help="Do not log to stdout/stderr"
    )
    args = parser.parse_args()

    configure_logging(args.quiet, args.debug_http)

    if args.github_oauth_token:
        github_oath_token = args.github_oauth_token
    else:
        with open("config.json") as config:
            github_oath_token = json.load(config)["github-oauth-token"]
    Settings.cached_session.headers.update(
        {"Authorization": "token " + github_oath_token}
    )

    Settings.blacklist = get_filter_list(Settings.blacklist_page)
    Settings.whitelist = get_filter_list(Settings.whitelist_page)

    logger.info("# Querying Projects")
    projects = query_projects(args.filter)
    logger.info("{} projects were found".format(len(projects)))

    logger.info("# Processing projects")
    for project in projects:
        logger.info("## " + project["projectLabel"] + ": " + project["project"])

        try:
            properties = get_data_from_github(project["repo"], project)
        except requests.exceptions.HTTPError:
            logger.error("HTTP request for {} failed".format(project["projectLabel"]))
            continue

        if Settings.do_update_wikidata:
            try:
                update_wikidata(properties)
            except Exception as e:
                logger.error("Failed to update {}: {}".format(properties.project, e))
                raise e

    logger.info("# Finished successfully")


if __name__ == "__main__":
    main()
