import logging

import sentry_sdk
from pydantic import TypeAdapter, BaseModel
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
    wikidata_sparql = sparql.SparqlQuery()
    response = wikidata_sparql.select(Settings.sparql_file.read_text())
    assert response is not None  # Type cast

    project_list_ta = TypeAdapter(list[WikidataProject])
    project_list = project_list_ta.validate_python(response)

    projects = []
    logger.info(f"{len(response)} projects were found by the sparql query")
    for project in project_list:
        if (
            project_filter
            and project_filter.lower() not in project.project.lower()
            and project_filter.lower() not in project.projectLabel.lower()
        ):
            continue
        if project.project[31:] in Settings.blacklist and not ignore_blacklist:
            logger.info(
                f"{project.projectLabel} ({project.wikidata_id}) is blacklisted"
            )
            continue

        if not Settings.repo_regex.match(project.repo):
            logger.info(
                f" - Removing {project.projectLabel}: {project.project} {project.repo}"
            )
            continue

        projects.append(project)

    logger.info(f"{len(projects)} projects remained after filtering")

    return projects
