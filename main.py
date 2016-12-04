"""
Update Wikidata and Wikipedia entries using data from github

For free software projects with a github repository listed in wikidata, this script collects will collect the following
metadata from the github API
 - latest release + release date
 - project website
"""

import json
import os
from pprint import pprint
from urllib.parse import urlparse, parse_qs, urlencode

import mwparserfromhell
import pywikibot
import requests
from pywikibot.data import sparql


class Settings:
    cachedir = "/home/konsti/wikidata-github/cache"
    source_code_repo_p = "P11081"
    website_p = "P31"
    reference_url_p = "P93"
    software_version_p = "P16316"
    release_date_p = "P776"
    sparql_free_software_items = "".join(open("free_software_items.rq").readlines())

    @staticmethod
    def get_wikidata():
        return pywikibot.Site("test", "wikidata")

    @staticmethod
    def get_wikipedia():
        return pywikibot.Site("en", "wikipedia")

    @staticmethod
    def github_repo_to_api(url):
        """Converts a github repoository url to the api entry with the general information"""
        return Settings.normalize_url(url) \
            .replace("https://github.com/", "https://api.github.com/repos/")

    @staticmethod
    def github_repo_to_api_releases(url):
        """Converts a github repoository url to the api entry with the releases"""
        return Settings.normalize_url(url) \
                   .replace("https://github.com/", "https://api.github.com/repos/") + "/releases"

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
    def normalize_version(version):
        if version[0] == "v":
            version = version[1:]
        return version

    @staticmethod
    def get_or_create_claim(repo, item, p_value, new_value):
        if p_value in item.claims:
            all_claims = item.claims[p_value]
        else:
            all_claims = []

        for claim in all_claims:
            if claim.getTarget() == new_value:
                break
        else:
            print("Adding {}".format(p_value))
            claim = pywikibot.Claim(repo, p_value)
            claim.setTarget(new_value)
            item.addClaim(claim)

        return claim

    @staticmethod
    def get_or_create_qualifiers(repo, claim, p_value, new_value):
        if p_value in claim.qualifiers:
            all_qualifiers = claim.qualifiers[p_value]
        else:
            all_qualifiers = []

        for qualifier in all_qualifiers:
            if qualifier.getTarget() == new_value:
                break
        else:
            print("Adding {}".format(p_value))
            qualifier = pywikibot.Claim(repo, p_value)
            qualifier.setTarget(new_value)
            claim.addQualifier(qualifier)

        return qualifier

    @staticmethod
    def get_or_create_sources(repo, claim, p_value, new_value):
        if claim.sources and p_value in claim.sources[0]:
            all_sources = claim.sources[0][p_value]
        else:
            all_sources = []

        print(all_sources)
        for source in all_sources:
            print(source.getTarget())
            print(new_value)
            if source.getTarget() == new_value:
                break
        else:
            print("Adding {}".format(p_value))
            source = pywikibot.Claim(repo, p_value)
            source.setTarget(new_value)
            claim.addSource(source)

        return source


def get_path_from_url(url_raw):
    """
    :param url_raw:
    :return: the path to where the corresponding url is cached
    """
    url = urlparse(url_raw)
    url_options = url.params
    query = parse_qs(url.query)
    if query != {}:
        url_options += "?" + urlencode(query)
    if url.fragment != "":
        url_options += "#" + url.fragment
    x = url.scheme + ":" + url.netloc
    y = url.path[1:] + url_options + ".json"
    return os.path.join(Settings.cachedir, x, y)


def get_request_cached(url):
    print("Loading from cache {}".format(url))
    filepath = get_path_from_url(url)
    if os.path.isfile(filepath):
        with open(filepath) as f:
            return json.load(f)

    response = requests.get(url)
    response.raise_for_status()
    response_json = response.json()
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w') as f:
        json.dump(response_json, f)

    return response_json


def query_projects():
    """
    Queries for all software projects and returns them as an array of simplified dicts
    :return: the data splitted into projects with and without github
    """
    wikdata_sparql = sparql.SparqlQuery()
    response = wikdata_sparql.query(Settings.sparql_free_software_items)

    # Split the data into those with repository and those without
    github = []
    no_github = []
    for project in response["results"]["bindings"]:
        # Ŕemove bloating type information
        for key in project.keys():
            project[key] = project[key]["value"]

        if "repo" in project:
            github.append(project)
        else:
            no_github.append(project)

    return github, no_github


def get_data_from_github(url):
    """
    Retrieve the following data from github. Sets it to None if none was given
     - website / homepage
     - version number string and release date of all stable releases
     - version number string and release date of all prereleases

    :param url: The url of the github repository
    :return: array with dicts
    """
    github_properties = {}

    # General project information
    project_info = get_request_cached(Settings.github_repo_to_api(url))
    github_properties["website"] = project_info["homepage"]

    # Get all pages of the release information
    url = Settings.github_repo_to_api_releases(url)
    page_number = 1
    releases = []
    while 1:
        page = get_request_cached(url + "?page=" + str(page_number))
        if not page:
            break
        page_number += 1
        releases += page

    github_properties["stable_release"] = []
    github_properties["pre_release"] = []

    # (pre)release versions and dates
    for release in releases:
        if release["prerelease"]:
            prefix = "pre_release"
        else:
            prefix = "stable_release"

        version = release["name"]
        if version == "":
            version = release["tag_name"]
        version = Settings.normalize_version(version)

        # Convert github's timestamps to wikidata dates
        date = pywikibot.WbTime.fromTimestr(release["published_at"])
        date.hour = 0
        date.minute = 0
        date.second = 0
        date.precision = pywikibot.WbTime.PRECISION["day"]

        github_properties[prefix].append({"version": version, "date": date})

    return github_properties


def update_wikidata(combined_properties):
    """
    Update wikidata entry with data from github

    :param combined_properties: dict
    :return:
    """
    print("### Wikidata")
    url_raw = combined_properties["repo"]
    url_normalized = Settings.normalize_url(url_raw)

    site = Settings.get_wikidata()
    repo = site.data_repository()
    q_value = combined_properties["project"].replace("http://www.wikidata.org/entity/", "")
    item = pywikibot.ItemPage(repo, title=q_value)
    item.get()

    # Canonicalize the github url
    if url_raw != url_normalized:
        print("Normalizing GitHub url")

        if Settings.source_code_repo_p in item.claims and \
                        len(item.claims[Settings.source_code_repo_p]) != 1:
            print("Error: Multiple source code repositories")
            return

        # Altering = remove -> edit -> add
        claim = pywikibot.Claim(repo, Settings.source_code_repo_p)
        claim.setTarget(url_normalized)
        claim.setSnakType('value')
        item.addClaim(claim)
        if len(item.claims[Settings.source_code_repo_p]) > 1:
            print("Removing old item")
            item.removeClaims(item.claims[Settings.source_code_repo_p][0])

    if combined_properties["website"] != "":
        claim = Settings.get_or_create_claim(repo, item, Settings.website_p,
                                             combined_properties["website"])
        Settings.get_or_create_sources(repo, claim, Settings.reference_url_p,
                                       Settings.github_repo_to_api(url_normalized))

    # Add latest stable release
    for release in combined_properties["stable_release"]:
        claim = Settings.get_or_create_claim(repo, item, Settings.software_version_p,
                                             release["version"])

        Settings.get_or_create_qualifiers(repo, claim, Settings.release_date_p,
                                          release["date"])
        Settings.get_or_create_sources(repo, claim, Settings.reference_url_p,
                                       Settings.github_repo_to_api_releases(url_normalized))


def update_wikipedia(combined_properties):
    """
    Updates the software info boxes of wikipedia articles according to github data

    :param combined_properties: dict
    :return:
    """
    print("### Wikipedia")

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
    projects_github, projects_no_github = query_projects()

    print()
    print("Fetching data from github:")
    combined_properties = []

    for project in projects_github:
        # For test wikidata
        project["project"] = "http://www.wikidata.org/entity/Q33832"
        print(" - " + project["projectLabel"])

        project_github = get_data_from_github(project["repo"])
        combined_properties.append({**project, **project_github})
        pprint(combined_properties, indent=4)

    print("Updating wikidata and wikipedia")
    for combined_property in combined_properties:
        update_wikidata(combined_property)
        update_wikipedia(combined_property)

    print("Processing projects without github link:")
    for project in projects_no_github:
        print("## " + project["projectLabel"])

if __name__ == '__main__':
    main()
