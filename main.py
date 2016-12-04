"""
Update Wikidata and Wikipedia entries using data from github
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
        return url.replace("https://github.com/", "https://api.github.com/repos/")


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
    github_properties = {}

    # General project information
    project_info = get_request_cached(url)
    github_properties["website"] = project_info["homepage"]

    # Latest stable release
    releases = get_request_cached(url + "/releases")

    github_properties["stable_release_version"] = None
    github_properties["stable_release_date"] = None
    for i in releases:
        if not i["prerelease"]:
            github_properties["stable_release_version"] = i["name"]
            github_properties["stable_release_date"] = i["published_at"]
            break

    # Find the latest prelease
    github_properties["pre_release_version"] = None
    github_properties["pre_release_date"] = None
    for i in releases:
        if i["prerelease"]:
            github_properties["pre_release_version"] = i["name"]
            github_properties["pre_release_date"] = i["published_at"]
            break

    # Parse release date
    for key in ["stable_release", "pre_release"]:
        if not github_properties[key + "_date"]:
            continue

        print(" --- " + key)

        date = pywikibot.WbTime.fromTimestr(github_properties[key + "_date"])
        date.hour = 0
        date.minute = 0
        date.second = 0
        date.precision = pywikibot.WbTime.PRECISION["day"]
        github_properties[key + "_date"] = date

    return github_properties


def update_wikidata(combined_properties):
    """
    Update wikidata entry with data from github

    :param combined_properties: dict
    :return:
    """
    print(" - " + combined_properties["projectLabel"])
    url_raw = combined_properties["repo"]

    # Canonical urls be like: no slash, no file extension
    url_normalized = url_raw.strip("/")
    if url_normalized.endswith('.git'):
        url_normalized = url_normalized[:-4]

    site = Settings.get_wikidata()
    repo = site.data_repository()
    q_value = combined_properties["project"].replace("http://www.wikidata.org/entity/", "")
    item = pywikibot.ItemPage(repo, title=q_value)
    item.get()

    url_api = url_normalized.replace("https://github.com/", "https://api.github.com/repos/")

    # Canonicalize the github url
    if url_raw != url_normalized:
        print("Adding GitHub url")

        if Settings.source_code_repo_p in item.claims and \
                        len(item.claims[Settings.source_code_repo_p]) != 1:
            print("Error: Multiple source code repositories")
            return

        # Altering = remove -> edit -> add
        claim = pywikibot.Claim(repo, Settings.source_code_repo_p)
        claim.setTarget(url_normalized)
        claim.setSnakType('value')
        print(claim)
        item.addClaim(claim)
        if len(item.claims[Settings.source_code_repo_p]) > 1:
            print("Removing old item")
            item.removeClaims(item.claims[Settings.source_code_repo_p][0])

    # Add website from github
    if Settings.website_p in item.claims:
        websites = [i.getTarget() for i in item.claims[Settings.website_p]]
    else:
        websites = []

    if combined_properties["website"] not in websites:
        print("Adding Website")
        # Don't remove existing website, they might also be true
        claim = pywikibot.Claim(repo, Settings.website_p)
        claim.setTarget(combined_properties["website"])
        print(claim)
        item.addClaim(claim)

        source_claim = pywikibot.Claim(repo, Settings.reference_url_p)
        source_claim.setTarget(url_api)
        claim.addSources([source_claim])

    # Add latest release
    if combined_properties["stable_release_version"]:
        print("Adding latest release")
        claim = pywikibot.Claim(repo, Settings.software_version_p)
        claim.setTarget(combined_properties["stable_release_version"])
        item.addClaim(claim)
        print(claim)

        # Add release data
        qualifier = pywikibot.Claim(repo, Settings.release_date_p)
        pprint(combined_properties["stable_release_date"])
        qualifier.setTarget(combined_properties["stable_release_date"])
        print(qualifier)
        claim.addQualifier(qualifier)

        # Add github as source
        source_claim = pywikibot.Claim(repo, Settings.reference_url_p)
        source_claim.setTarget(url_api)
        print(source_claim)
        claim.addSources([source_claim])


def update_wikipedia(combined_properties):
    """
    Updates the software info boxes of wikipedia articles according to github data

    :param combined_properties: dict
    :return:
    """
    wikipedia_tag = combined_properties["article"].replace("https://en.wikipedia.org/wiki/", "")
    site = Settings.get_wikipedia()
    page = pywikibot.Page(site, wikipedia_tag)
    text = page.text
    wikitext = mwparserfromhell.parse(text)
    templates = wikitext.filter_templates(recursive=True)

    for template in templates:
        print(template.name)
        if template.name.matches("Infobox software"):
            break
    else:
        print("No 'Infobox software' found! Skipping {}".format(wikipedia_tag))
        return

    template_before_edit = str(template)
    print(template)

    if combined_properties["stable_release_version"]:
        srv = " " + combined_properties["stable_release_version"] + "\n"
        if template.has("latest release version"):
            template.get("latest release version").value = srv
        else:
            template.add("latest release version", srv)

        date = combined_properties["stable_release_date"]
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
        print("\n --- Template has been edited --- \n")
        print(template)


def main():
    projects_github, projects_no_github = query_projects()
    pprint(projects_github)

    print("Processing projects without github link:")
    for project in projects_no_github:
        print(" - " + project["projectLabel"])

    print()
    print("Processing projects with github link:")

    pprint(projects_github)

    for project in projects_github:
        # For test wikidata
        project["project"] = "http://www.wikidata.org/entity/Q33832"
        print(" - " + project["projectLabel"])

        repo = Settings.github_repo_to_api(project["repo"])
        project_github = get_data_from_github(repo)
        combined_properties = {**project, **project_github}
        pprint(combined_properties)

        update_wikidata(combined_properties)
        update_wikipedia(combined_properties)


if __name__ == '__main__':
    main()
