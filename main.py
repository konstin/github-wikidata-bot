#!/usr/bin/env python3.5

import re

import mwparserfromhell
import pywikibot
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache
from cachecontrol.heuristics import LastModified
from pywikibot.data import sparql


class Settings:
    do_update_wikidata = True
    do_update_wikipedia = False

    wikidata_repo = pywikibot.Site("wikidata", "wikidata").data_repository()

    repo_regex = re.compile(r"https://github.com/[^/]+/[^/]+")
    version_regex = re.compile(r"\d+(\.\d+)+")
    sparql_free_software_items = "".join(open("free_software_items.rq").readlines())
    oauth_token_file = "github_oauth_token.txt"
    # pywikibot is too stupid to cache the calendar model, so let's do this manually
    calendarmodel = pywikibot.Site().data_repository().calendarmodel()

    cached_session = CacheControl(
        requests.Session(),
        cache=FileCache('cache', forever=True),
        heuristic=LastModified()
    )

    properties = {
        "software version": "P348",
        "publication date": "P577",
        "retrieved": "P813",
        "reference URL": "P854",
        "official website": "P856",
        "source code repository": "P1324",
    }

    @staticmethod
    def get_wikipedia():
        return pywikibot.Site("en", "wikipedia")

    @staticmethod
    def github_repo_to_api(url):
        """Converts a github repoository url to the api entry with the general information"""
        url = Settings.normalize_url(url)
        url = url.replace("https://github.com/", "https://api.github.com/repos/")
        return url

    @staticmethod
    def github_repo_to_api_releases(url):
        """Converts a github repoository url to the api entry with the releases"""
        url = Settings.normalize_url(url)
        url = url.replace("https://github.com/", "https://api.github.com/repos/")
        url += "/releases"
        return url

    @staticmethod
    def normalize_url(url):
        """
        Canonical urls be like: no slash, no file extension

        :param url:
        :return:
        """
        url = url.strip("/")
        if url.endswith('.git'):
            url = url[:-4]
        return url

    @staticmethod
    def normalize_version(version, name):
        """
        Removes some of the bloat in the version strings. Note that this function is mostly useless as it has become
        superseeded by the regex.
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

    @staticmethod
    def _get_or_create(method, all_objects, repo, p_value, value):
        """
        Helper method that adds a value `value` with the property `p_value` if it doesn't exist, otherwise retrives it.
        """
        for requested in all_objects:
            if requested.target_equals(value):
                break
        else:
            requested = pywikibot.Claim(repo, p_value)
            requested.setTarget(value)
            method(requested)

        return requested

    @staticmethod
    def get_or_create_claim(repo, item, p_value, value):
        """
        Gets or creates a claim with `value` under the property `p_value` to `item`
        """
        if p_value in item.claims:
            all_objects = item.claims[p_value]
        else:
            all_objects = []

        return Settings._get_or_create(item.addClaim, all_objects, repo, p_value, value)

    @staticmethod
    def get_or_create_qualifiers(repo, claim, p_value, qualifier):
        """
        Gets or creates a `qualfier` under the property `p_value` to `claim`
        """
        if p_value in claim.qualifiers:
            all_objects = claim.qualifiers[p_value]
        else:
            all_objects = []

        return Settings._get_or_create(claim.addQualifier, all_objects, repo, p_value, qualifier)

    @staticmethod
    def get_or_create_sources(repo, qualifier, value, retrieved):
        """
        Gets or creates a `source` under the property `p_value` to `qualifier`
        """
        all_sources = []

        src_p = Settings.properties["reference URL"]
        retrieved_p = Settings.properties["retrieved"]

        # We could have many qualifiers, so let's
        if qualifier.sources:
            for i in qualifier.sources:
                if src_p in i:
                    all_sources.append(i[src_p][0])
        else:
            all_sources = []

        for src_url in all_sources:
            if src_url.target_equals(value):
                break
        else:
            src_url = pywikibot.Claim(repo, src_p)
            src_url.setTarget(value)
            src_retrieved = pywikibot.Claim(repo, retrieved_p)
            src_retrieved.setTarget(retrieved)
            qualifier.addSources([src_url, src_retrieved])

        return src_url


def get_json_cached(url):
    response = Settings.cached_session.get(url)
    response.raise_for_status()
    return response.json()


def query_projects():
    """
    Queries for all software projects and returns them as an array of simplified dicts
    :return: the data splitted into projects with and without github
    """
    wikdata_sparql = sparql.SparqlQuery()
    response = wikdata_sparql.query(Settings.sparql_free_software_items)

    # Split the data into those with repository and those without
    projects = []
    for project in response["results"]["bindings"]:
        # Remove bloating type information
        for key in project.keys():
            project[key] = project[key]["value"]

        projects.append(project)

    return projects


def get_data_from_github(url):
    """
    Retrieve the following data from github. Sets it to None if none was given by github
     - website / homepage
     - version number string and release date of all stable releases
     - version number string and release date of all prereleases

    :param url: The url of the github repository
    :return: dict of dicts
    """
    github_properties = {}

    # "retrieved" does only accept dates without time, so create a timestamp with no date
    isotimestamp = pywikibot.Timestamp.utcnow().toISOformat()
    date = pywikibot.WbTime.fromTimestr(isotimestamp, calendarmodel=Settings.calendarmodel)
    date.hour = 0
    date.minute = 0
    date.second = 0
    date.precision = pywikibot.WbTime.PRECISION["day"]
    github_properties["retrieved"] = date

    # General project information
    project_info = get_json_cached(Settings.github_repo_to_api(url))
    if type(project_info) == list:
        project_info = project_info[0]

    if "homepage" in project_info:
        github_properties["website"] = project_info["homepage"]

    # Get all pages of the release information
    url = Settings.github_repo_to_api_releases(url)
    page_number = 1
    releases = []
    while 1:
        page = get_json_cached(url + "?page=" + str(page_number))
        if not page:
            break
        page_number += 1
        releases += page

    github_properties["stable_release"] = []
    github_properties["pre_release"] = []

    # (pre)release versions and dates
    for release in releases:
        # Heuristics to find the version number
        release_name = Settings.normalize_version(release["name"], project_info["name"])
        release_tag_name = Settings.normalize_version(release["tag_name"], project_info["name"])

        # Workaround for Activiti
        if "Beta" in release_name or "Beta" in release_tag_name:
            release["prerelease"] = True

        match_name = re.search(Settings.version_regex, release_name)
        match_tag_name = re.search(Settings.version_regex, release_tag_name)
        if match_name:
            version = match_name.group(0)
        elif match_tag_name:
            version = match_tag_name.group(0)
        else:
            print(" - Invalid version strings '{}'".format(release["name"]))
            continue

        # Convert github's timestamps to wikidata dates
        date = pywikibot.WbTime.fromTimestr(release["published_at"], calendarmodel=Settings.calendarmodel)
        date.hour = 0
        date.minute = 0
        date.second = 0
        date.precision = pywikibot.WbTime.PRECISION["day"]

        if release["prerelease"]:
            prefix = "pre_release"
        else:
            prefix = "stable_release"
        github_properties[prefix].append({"version": version, "date": date})

    return github_properties


def update_wikidata(combined_properties):
    """
    Update wikidata entry with data from github

    :param combined_properties: dict
    :return:
    """
    url_raw = combined_properties["repo"]
    url_normalized = Settings.normalize_url(url_raw)

    # Wikidata boilerplate
    repo = Settings.wikidata_repo
    q_value = combined_properties["project"].replace("http://www.wikidata.org/entity/", "")
    item = pywikibot.ItemPage(repo, title=q_value)
    item.get()

    # This does not work with a normal account
    """
    # Canonicalize the github url
    if url_raw != url_normalized:
        print("Normalizing GitHub url")

        if Settings.properties["source code repository"] in item.claims and \
                len(item.claims[Settings.properties["source code repository"]]) != 1:
            print("Error: Multiple source code repositories")
            return

        # Altering = remove -> edit -> add
        claim = pywikibot.Claim(repo, Settings.properties["source code repository"])
        claim.setTarget(url_normalized)
        claim.setSnakType('value')
        item.addClaim(claim)
        if len(item.claims[Settings.properties["source code repository"]]) > 1:
            print("Removing old item")
            item.removeClaims(item.claims[Settings.properties["source code repository"]][0])
    """

    # Add the website
    print("Adding the website")
    if combined_properties["website"] and combined_properties["website"].startswith("http"):
        claim = Settings.get_or_create_claim(repo, item, Settings.properties["official website"],
                                             combined_properties["website"])
        Settings.get_or_create_sources(repo, claim, Settings.github_repo_to_api(url_normalized),
                                       combined_properties["retrieved"])

    # Add all stable releases
    if len(combined_properties["stable_release"]) > 0:
        print("Adding all {} stable releases:".format(len(combined_properties["stable_release"])))
    for release in combined_properties["stable_release"]:
        print(" - '{}'".format(release["version"]))
        claim = Settings.get_or_create_claim(repo, item, Settings.properties["software version"],
                                             release["version"])

        Settings.get_or_create_qualifiers(repo, claim, Settings.properties["publication date"],
                                          release["date"])
        Settings.get_or_create_sources(repo, claim, Settings.github_repo_to_api_releases(url_normalized),
                                       combined_properties["retrieved"])

        # TODO give the latest release the preferred rank


def update_wikipedia(combined_properties):
    """
    Updates the software info boxes of wikipedia articles according to github data

    :param combined_properties: dict
    :return:
    """
    q_value = combined_properties["article"].replace("https://en.wikipedia.org/wiki/", "")
    site = Settings.get_wikipedia()
    page = pywikibot.Page(site, q_value)
    text = page.text
    wikitext = mwparserfromhell.parse(text)
    templates = wikitext.filter_templates(recursive=True)

    # Find the software info box
    for template in templates:
        if template.name.matches("Infobox software"):
            break
    else:
        print("No 'Infobox software' found! Skipping {}".format(q_value))
        return

    template_before_edit = str(template)
    print(template)

    if combined_properties["stable_release"]:
        srv = " " + combined_properties["stable_release"][0]["version"] + "\n"
        if template.has("latest release version"):
            template.get("latest release version").value = srv
        else:
            template.add("latest release version", srv)

        date = combined_properties["stable_release"][0]["date"]
        date_text = "{{{{release date|{}|{}|{}}}}}".format(date.year, date.month, date.day)
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
        print("\nThe template has been edited:\n")
        print(template)


def main():
    github_oath_token = open(Settings.oauth_token_file).readline().strip()
    Settings.cached_session.headers.update({"Authorization": "token " + github_oath_token})

    print("# Query Projects")
    projects = query_projects()

    print("# Fetching data from github:")

    print("# Projects with github link")
    for project in projects:
        print("## " + project["projectLabel"])

        if not Settings.repo_regex.match(project["repo"]):
            print("Skipping: {}".format(project["repo"]))
            continue

        try:
            project_github = get_data_from_github(project["repo"])
        except requests.exceptions.HTTPError:
            print("HTTP request for {} failed".format(project["projectLabel"]))
            continue

        combined_property = {**project, **project_github}

        if Settings.do_update_wikidata:
            update_wikidata(combined_property)
        if Settings.do_update_wikipedia:
            update_wikipedia(combined_property)


if __name__ == '__main__':
    main()
