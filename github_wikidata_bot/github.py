import logging
import re
from dataclasses import dataclass
from distutils.version import LooseVersion
from json import JSONDecodeError
from typing import List, Optional, Dict
from urllib.parse import quote_plus

import pywikibot
from pywikibot import WbTime
from requests import HTTPError

from .settings import Settings
from .utils import (
    github_repo_to_api,
    github_repo_to_api_releases,
    github_repo_to_api_tags,
)
from .versionhandler import extract_version

logger = logging.getLogger(__name__)


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
