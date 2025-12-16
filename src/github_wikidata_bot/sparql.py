import logging
from collections import defaultdict

import sentry_sdk
from pydantic import BaseModel, TypeAdapter
from pywikibot.data import sparql

from github_wikidata_bot.settings import Settings

logger = logging.getLogger(__name__)


class WikidataProject(BaseModel):
    project: str
    projectLabel: str
    repo: str

    @property
    def wikidata_id(self) -> str:
        return self.project.rsplit("/", 1)[-1]


@sentry_sdk.trace
def query_projects(
    project_filter: str | None = None, ignore_blacklist: bool = False
) -> list[WikidataProject]:
    """
    Queries for all software projects and returns them as an array of simplified dicts
    :return: the data split into projects with and without github
    """

    logger.info("Querying wikidata for projects")
    wikidata_sparql = sparql.SparqlQuery()
    response = wikidata_sparql.select(Settings.query_projects.read_text())
    assert response is not None  # Type cast

    project_list_ta = TypeAdapter(list[WikidataProject])
    project_list = project_list_ta.validate_python(response)

    projects = []
    logger.info(f"{len(response)} projects were found by the sparql query")
    repo_filter = 0
    blacklist = 0
    repo_regex = 0
    inconsistent = 0
    duplicates = 0
    for project in project_list:
        # https://phabricator.wikimedia.org/T407702
        if project.wikidata_id == "Q124831300":
            continue

        if (
            project_filter
            and project_filter.lower() not in project.project.lower()
            and project_filter.lower() not in project.projectLabel.lower()
        ):
            repo_filter += 1
            continue
        if project.project[31:] in Settings.blacklist and not ignore_blacklist:
            logger.debug(
                f"{project.projectLabel} ({project.wikidata_id}) is blacklisted"
            )
            blacklist += 1
            continue

        if not Settings.repo_regex.match(project.repo):
            logger.debug(
                f" - Removing {project.projectLabel}: {project.project} {project.repo}"
            )
            repo_regex += 1
            continue

        if len(projects) > 1 and projects[-1].project == project.project:
            if projects[-1].repo != project.repo:
                logger.debug(
                    f"Repo mismatch: {project.projectLabel} {projects[-1].repo} {project.repo}"
                )
                # TODO: Handle >2 repo entries
                projects.pop(-1)
                inconsistent += 2
                continue

            duplicates += 1
            continue

        projects.append(project)

    logger.info(
        f"{len(projects)} projects remain after filtering "
        + f"(repo_filter: {repo_filter}, blacklist: {blacklist}, repo_regex: {repo_regex}, duplicates: {duplicates}, inconsistent: {inconsistent})"
    )

    return projects


@sentry_sdk.trace
def query_best_versions() -> dict[str, list[str]]:
    """Query for all software projects and their best version(s) on wikidata."""
    logger.info("Querying wikidata for versions")
    wikidata_sparql = sparql.SparqlQuery()
    response = wikidata_sparql.select(Settings.query_versions.read_text())
    assert response is not None  # Type cast

    best_versions = defaultdict(list)
    for entry in response:
        best_versions[entry["project"]].append(entry["version"])
    best_versions = dict(best_versions)

    logger.info(
        f"Found {len(response)} best versions for {len(best_versions)} projects"
    )

    return best_versions
