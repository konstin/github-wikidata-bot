#!/usr/bin/env python3
import argparse
import json
import logging.config
import re
from distutils.version import LooseVersion
from json.decoder import JSONDecodeError

import mwparserfromhell
import pywikibot
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache
from cachecontrol.heuristics import LastModified
from pywikibot.data import sparql

LOGGING = {
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
    "loggers": {__name__: {"handlers": ["console", "all", "error"], "level": "INFO"}},
}

logging.config.dictConfig(LOGGING)

logger = logging.getLogger(__name__)


class Settings:
    do_update_wikidata = False
    # Don't activate this, it's most likely broken
    do_update_wikipedia = False

    normalize_url = True

    sparql_file = "free_software_items.rq"

    # pywikibot is too stupid to cache the calendar model, so let's do this manually
    calendarmodel = pywikibot.Site().data_repository().calendarmodel()
    wikidata_repo = pywikibot.Site("wikidata", "wikidata").data_repository()

    repo_regex = re.compile(r"^[a-z]+://github.com/[^/]+/[^/]+/?$")
    version_regex = re.compile(
            r"\d+(\.\d+)+([a-z]|-\d|-?(alpha|beta|preview|rc)[.-]?\d*)?(\s|$)",
            re.IGNORECASE
    )
    # Often prereleases aren't marked as such, so we need manually catch those cases
    unmarked_prerelease_regex = re.compile(
        r"[ -._\d](r|rc|beta|alpha)([ .\d].*)?$", re.IGNORECASE
    )

    cached_session = CacheControl(
        requests.Session(),
        cache=FileCache("cache", forever=True),
        heuristic=LastModified(),
    )

    properties = {
        "software version": "P348",
        "publication date": "P577",
        "retrieved": "P813",
        "reference URL": "P854",
        "official website": "P856",
        "source code repository": "P1324",
        "title": "P1476",
    }


def github_repo_to_api(url):
    """Converts a github repoository url to the api entry with the general information"""
    url = normalize_url(url)
    url = url.replace("https://github.com/", "https://api.github.com/repos/")
    return url


def github_repo_to_api_releases(url):
    """Converts a github repoository url to the api entry with the releases"""
    url = github_repo_to_api(url)
    url += "/releases"
    return url


def normalize_url(url):
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


def normalize_version(version, name):
    """
    Removes some of the bloat in the version strings. Note that this function
    is mostly useless as it has become superseeded by the regex.
    """
    if not version:
        return ""

    for i in [re.escape(name), "release", "stable", "version", "patch", r"  +"]:
        insensitive = re.compile(i, re.IGNORECASE)
        version = insensitive.sub("", version)
    version = version.strip()
    if len(version) > 0 and version[0] == "v":
        version = version[1:]
    version = version.strip(" -_")
    return version


def _get_or_create(method, all_objects, repo, p_value, value):
    """
    Helper method that adds a value `value` with the property `p_value` if it
    doesn't exist, otherwise retrieves it.
    """
    for requested in all_objects:
        if requested.target_equals(value):
            break
    else:
        requested = pywikibot.Claim(repo, p_value)
        requested.setTarget(value)
        method(requested)

    return requested


def get_or_create_claim(repo, item, p_value, value):
    """
    Gets or creates a claim with `value` under the property `p_value` to `item`
    """
    if p_value in item.claims:
        all_objects = item.claims[p_value]
    else:
        all_objects = []

    return _get_or_create(item.addClaim, all_objects, repo, p_value, value)


def get_or_create_qualifiers(repo, claim, p_value, qualifier):
    """
    Gets or creates a `qualifier` under the property `p_value` to `claim`
    """
    if p_value in claim.qualifiers:
        all_objects = claim.qualifiers[p_value]
    else:
        all_objects = []

    return _get_or_create(claim.addQualifier, all_objects, repo, p_value, qualifier)


def get_or_create_sources(repo, claim, url, retrieved, title=None, date=None):
    """
    Gets or creates a `source` under the property `claim` to `url`
    """
    all_sources = []

    src_p = Settings.properties["reference URL"]
    retrieved_p = Settings.properties["retrieved"]
    title_p = Settings.properties["title"]
    date_p = Settings.properties["publication date"]

    # We could have many sources, so let's
    if claim.sources:
        for i in claim.sources:
            if src_p in i:
                all_sources.append(i[src_p][0])
    else:
        all_sources = []

    for src_url in all_sources:
        if src_url.target_equals(url):
            break
    else:
        src_url = pywikibot.Claim(repo, src_p)
        src_url.setTarget(url)
        src_retrieved = pywikibot.Claim(repo, retrieved_p)
        src_retrieved.setTarget(retrieved)

        sources = [src_url, src_retrieved]

        if title:
            src_title = pywikibot.Claim(repo, title_p)
            src_title.setTarget(pywikibot.WbMonolingualText(title, "en"))
            sources.append(src_title)
        if date:
            src_date = pywikibot.Claim(repo, date_p)
            src_date.setTarget(date)
            sources.append(src_date)
        claim.addSources(sources)

    return src_url


def get_json_cached(url):
    response = Settings.cached_session.get(url)
    response.raise_for_status()
    try:
        return response.json()
    except JSONDecodeError as e:
        logger.error("JSONDecodeError for {}: {}".format(url, e))
        return {}


def query_projects():
    """
    Queries for all software projects and returns them as an array of simplified dicts
    :return: the data splitted into projects with and without github
    """
    wikdata_sparql = sparql.SparqlQuery()
    sparql_free_software_items = "".join(open(Settings.sparql_file).readlines())
    response = wikdata_sparql.query(sparql_free_software_items)

    projects = []
    for project in response["results"]["bindings"]:
        # Remove bloating type information
        for key in project.keys():
            project[key] = project[key]["value"]

        projects.append(project)

    return projects


def get_data_from_github(url, properties):
    """
    Retrieve the following data from github. Sets it to None if none was given by github
     - website / homepage
     - version number string and release date of all stable releases
     - version number string and release date of all prereleases

    All data is preprocessed, i.e. the version numbers are extracted and
    unmarked prereleases are discovered

    :param url: The url of the github repository
    :param properties: The already gathered information
    :return: dict of dicts
    """
    # "retrieved" does only accept dates without time, so create a timestamp with no date
    # noinspection PyUnresolvedReferences
    isotimestamp = pywikibot.Timestamp.utcnow().toISOformat()
    date = pywikibot.WbTime.fromTimestr(
        isotimestamp, calendarmodel=Settings.calendarmodel
    )
    date.hour = 0
    date.minute = 0
    date.second = 0
    date.precision = pywikibot.WbTime.PRECISION["day"]
    properties["retrieved"] = date

    # General project information
    project_info = get_json_cached(github_repo_to_api(url))

    if project_info.get("homepage"):
        properties["website"] = project_info["homepage"]

    # Get all pages of the release information
    url = github_repo_to_api_releases(url)
    page_number = 1
    releases = []
    while True:
        page = get_json_cached(url + "?page=" + str(page_number))
        if not page:
            break
        page_number += 1
        releases += page

    properties["stable_release"] = []
    properties["pre_release"] = []

    # (pre)release versions and dates
    for release in releases:
        # Heuristics to find the version number
        release_name = normalize_version(release["name"], project_info["name"])
        release_tag_name = normalize_version(release["tag_name"], project_info["name"])

        match_name = list(re.finditer(Settings.version_regex, release_name))
        match_tag_name = list(re.finditer(Settings.version_regex, release_tag_name))
        if len(match_tag_name) == 1:
            version = match_tag_name[0].group(0).strip()
            original_version = release_tag_name
        elif len(match_name) == 1:
            version = match_name[0].group(0).strip()
            original_version = release_name
        else:
            logger.warning("Invalid version string '{}'".format(release["name"]))
            continue

        # Fix missing "Release Candidate" annotation on github
        if not release["prerelease"] and re.search(
            Settings.unmarked_prerelease_regex, original_version
        ):
            logger.info("Assuming Release Candidate: " + original_version)
            release["prerelease"] = True
            continue

        # Convert github's timestamps to wikidata dates
        date = pywikibot.WbTime.fromTimestr(
            release["published_at"], calendarmodel=Settings.calendarmodel
        )
        date.hour = 0
        date.minute = 0
        date.second = 0
        date.precision = pywikibot.WbTime.PRECISION["day"]

        if release["prerelease"]:
            prefix = "pre_release"
        else:
            prefix = "stable_release"
        properties[prefix].append(
            {"version": version, "date": date, "page": release["html_url"]}
        )

    return properties


def do_normalize_url(item, repo, url_normalized, url_raw, q_value):
    """ Canonicalize the github url
    This use the format https://github.com/[owner]/[repo]

    Note: This apparently only works with a bot account
    """
    if url_raw == url_normalized:
        return

    logger.info("Normalizing {} to {}".format(url_raw, url_normalized))

    source_p = Settings.properties["source code repository"]
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
    claim = pywikibot.Claim(repo, source_p)
    claim.setTarget(url_normalized)
    claim.setSnakType("value")
    item.addClaim(claim)
    item.removeClaims(urls[0])


def set_claim_rank(claim, latest_version, release):
    if release["version"] == latest_version:
        if claim.getRank() != "preferred":
            claim.changeRank("preferred")
    else:
        if claim.getRank() != "normal":
            claim.changeRank("normal")


def update_wikidata(properties):
    """ Update wikidata entry with data from github """
    # Wikidata boilerplate
    wikidata = Settings.wikidata_repo
    q_value = properties["project"].replace("http://www.wikidata.org/entity/", "")
    item = pywikibot.ItemPage(wikidata, title=q_value)
    item.get()

    url_raw = properties["repo"]
    url_normalized = normalize_url(url_raw)
    if Settings.normalize_url:
        do_normalize_url(item, wikidata, url_normalized, url_raw, q_value)

    # Add the website if doesn not already exists
    if (
        properties.get("website", "").startswith("http")
        and Settings.properties["official website"] not in item.claims
    ):
        logger.info("Adding the website: {}".format(properties["website"]))
        claim = get_or_create_claim(
            wikidata,
            item,
            Settings.properties["official website"],
            properties["website"],
        )
        get_or_create_sources(
            wikidata, claim, github_repo_to_api(url_normalized), properties["retrieved"]
        )

    # Add all stable releases
    stable_releases = properties["stable_release"]
    stable_releases.sort(key=lambda x: LooseVersion(x["version"]))
    stable_releases.reverse()

    if len(stable_releases) == 0:
        logger.info("No stable releases")
        return

    versions = [i["version"] for i in stable_releases]
    if len(versions) != len(set(versions)):
        duplicates = [
            {"version": release["version"], "page": release["page"]}
            for release in stable_releases
            if versions.count(release["version"]) > 1
                ]
        logger.error("There are duplicate releases in {}: {}".format(q_value, duplicates))
        return

    latest_version = stable_releases[0]["version"]
    logger.info("Latest version: {}".format(latest_version))

    existing_versions = item.claims.get(Settings.properties["software version"], [])
    github_version_names = [i["version"] for i in stable_releases]

    for i in existing_versions:
        if i.getRank() == "preferred" and i.getTarget() not in github_version_names:
            logger.warning(
                "There's a preferred rank for a version which is not in the github page: {}".format(
                    i.getTarget()
                )
            )
            latest_version = None

    if len(stable_releases) > 100:
        logger.warning("Adding only 100 stable releases of ", len(stable_releases))
        stable_releases = stable_releases[-100:]
    else:
        logger.info("Adding {} stable releases:".format(len(stable_releases)))

    for release in stable_releases:
        logger.info(" - '{}'".format(release["version"]))
        claim = get_or_create_claim(
            wikidata, item, Settings.properties["software version"], release["version"]
        )

        # Assumption: A preexisting publication date is more reliable than the one from github
        date_p = Settings.properties["publication date"]
        if date_p not in claim.qualifiers:
            get_or_create_qualifiers(wikidata, claim, date_p, release["date"])

        title = "Release %s" % release["version"]
        get_or_create_sources(
            wikidata,
            claim,
            release["page"],
            properties["retrieved"],
            title,
            release["date"],
        )

        # Give the latest release the preferred rank
        # And work around a bug in pywikibot
        try:
            set_claim_rank(claim, latest_version, release)
        except AssertionError:
            logger.warning("Using the fallback for setting the preferred rank")

            item.get(force=True)

            claim = get_or_create_claim(
                wikidata,
                item,
                Settings.properties["software version"],
                release["version"],
            )
            set_claim_rank(claim, latest_version, release)


def update_wikipedia(combined_properties):
    """
    Updates the software info boxes of wikipedia articles according to github data.
    Most likely BROKEN
    """
    if "article" not in combined_properties:
        return
    q_value = combined_properties["article"].replace(
        "https://en.wikipedia.org/wiki/", ""
    )
    page = pywikibot.Page(pywikibot.Site("en", "wikipedia"), q_value)
    text = page.text
    wikitext = mwparserfromhell.parse(text)
    templates = wikitext.filter_templates(recursive=True)

    # Find the software info box
    for template in templates:
        if template.name.matches("Infobox software"):
            break
    else:
        logger.info("No 'Infobox software' found! Skipping {}".format(q_value))
        return

    template_before_edit = str(template)
    logger.info(template)

    if combined_properties["stable_release"]:
        srv = " " + combined_properties["stable_release"][0]["version"] + "\n"
        if template.has("latest release version"):
            template.get("latest release version").value = srv
        else:
            template.add("latest release version", srv)

        date = combined_properties["stable_release"][0]["date"]
        date_text = "{{{{release date|{}|{}|{}}}}}".format(
            date.year, date.month, date.day
        )
        if template.has("latest release date"):
            template.get("latest release date").value = " " + date_text + "\n"
        else:
            template.add("latest release date", date_text)

    if combined_properties["website"]:
        srv = " {{URL|" + combined_properties["website"] + "}}\n"
        if template.has("website"):
            template.get("website").value = srv
        else:
            template.add("website", srv)

    if str(template) != template_before_edit:
        logger.info("\nThe template has been edited:\n")
        logger.info(template)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default="")
    parser.add_argument("--github-oauth-token")
    args = parser.parse_args()

    if args.github_oauth_token:
        github_oath_token = args.github_oauth_token
    else:
        with open("config.json") as config:
            github_oath_token = json.load(config)["github-oauth-token"]
    Settings.cached_session.headers.update(
        {"Authorization": "token " + github_oath_token}
    )

    logger.info("# Querying Projects")
    projects = query_projects()
    logger.info("{} projects were found".format(len(projects)))

    logger.info("# Filtering Projects")
    projects_filtered = []
    for project in projects:
        if args.filter not in project["projectLabel"]:
            continue

        if not Settings.repo_regex.match(project["repo"]):
            logger.info(
                " - {}: {} {}".format(
                    project["projectLabel"], project["project"], project["repo"]
                )
            )
            continue

        projects_filtered.append(project)

    logger.info("# Processing projects")
    for project in projects_filtered:
        logger.info("## " + project["projectLabel"] + ": " + project["project"])

        try:
            properties = get_data_from_github(project["repo"], project)
        except requests.exceptions.HTTPError:
            logger.error("HTTP request for {} failed".format(project["projectLabel"]))
            continue

        if Settings.do_update_wikidata:
            update_wikidata(properties)
        if Settings.do_update_wikipedia:
            update_wikipedia(properties)

    logger.info("# Finished successfully")


if __name__ == "__main__":
    main()
