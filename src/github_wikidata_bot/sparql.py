import json
import logging
from collections import defaultdict
from typing import Any

import sentry_sdk
from pydantic import BaseModel
from pywikibot.data import sparql

from github_wikidata_bot.settings import Settings, cache_root

logger = logging.getLogger(__name__)


class WikidataProject(BaseModel):
    project: str
    projectLabel: str
    repo: str

    @property
    def wikidata_id(self) -> str:
        return self.project.rsplit("/", 1)[-1]


def cached_sparql_query(
    query_name: str,
    use_cache: bool,
    settings: Settings,
) -> list[dict[str, str]]:
    """Run a SPARQL query against wikidata, reading from a local cache if requested."""
    cache_path = cache_root().joinpath(f"{query_name}.json")
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text())
    with sentry_sdk.start_span(op="sparql", name=f"Query {query_name}"):
        wikidata_sparql = sparql.SparqlQuery()
        response = wikidata_sparql.select(
            settings.sparql_dir.joinpath(f"{query_name}.rq").read_text()
        )
        assert response is not None  # Type cast
    cache_root().mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(response))
    return response


def filter_projects(
    ignore_blacklist: bool,
    project_filter: str | None,
    project_list: list[WikidataProject],
    response: list[dict[str, str]],
    settings: Settings,
) -> list[Any]:
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
        if project.project[31:] in settings.blacklist and not ignore_blacklist:
            logger.debug(
                f"{project.projectLabel} ({project.wikidata_id}) is blacklisted"
            )
            blacklist += 1
            continue

        if not settings.repo_regex.match(project.repo):
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
def query_best_versions(use_cache: bool, settings: Settings) -> dict[str, list[str]]:
    """Query for all software projects and their best version(s) on wikidata."""
    logger.info("Querying wikidata for project versions")
    response = cached_sparql_query("free_software_versions", use_cache, settings)

    best_versions = defaultdict(list)
    for entry in response:
        best_versions[entry["project"]].append(entry["version"])
    best_versions = dict(best_versions)

    logger.info(
        f"Found {len(response)} best versions for {len(best_versions)} projects"
    )

    return best_versions
