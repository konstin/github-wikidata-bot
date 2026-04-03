from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

import sentry_sdk

from github_wikidata_bot.project import WikidataProject
from github_wikidata_bot.session import Session, cache_root, sparql_dir
from github_wikidata_bot.wikidata_api import ServerError

logger = logging.getLogger(__name__)


@sentry_sdk.trace
async def cached_sparql_query(
    query_name: str, use_cache: bool, session: Session
) -> list[dict[str, str]]:
    """Run a SPARQL query against wikidata, reading from a local cache if requested."""
    cache_path = cache_root().joinpath(f"{query_name}.json")
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text())
    query_text = sparql_dir().joinpath(f"{query_name}.rq").read_text()
    for attempt in range(session.retries):
        try:
            response = await session.wikidata.sparql_query(query_text)
        except ServerError as e:
            if attempt < session.retries - 1:
                sleep = 2**attempt * 10
                logger.warning(
                    f"SPARQL query {query_name} failed (attempt {attempt + 1}/{session.retries}), retrying after {sleep}s: {e}"
                )
                await asyncio.sleep(sleep)
                continue
            raise
        cache_root().mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(response))
        return response
    raise ValueError("retries can't be 0")


async def cached_projects_query(
    use_cache: bool, session: Session, project_filter: str | None
) -> list[WikidataProject]:
    """Run a SPARQL query against wikidata, reading from a local cache if requested."""
    response = await cached_sparql_query("free_software_items", use_cache, session)
    logger.info(f"SPARQL query found {len(response)} projects")

    invalid_repo = 0
    repo_filter = 0
    blacklist = 0
    inconsistent = 0
    duplicates = 0
    projects = []
    for project in response:
        try:
            project = WikidataProject.from_sparql(project)
        except ValueError as e:
            logger.debug(e)
            invalid_repo += 1
            continue

        if (
            project_filter
            and project_filter.lower() not in project.q_value_url.lower()
            and project_filter.lower() not in project.label.lower()
        ):
            repo_filter += 1
            continue
        if project.q_value in session.denylist:
            logger.debug(f"{project.label} ({project.q_value}) is blacklisted")
            blacklist += 1
            continue

        if len(projects) > 1 and projects[-1].q_value == project.q_value:
            if projects[-1].repo != project.repo:
                logger.debug(
                    f"Repo mismatch: {project.label} {projects[-1].q_value_url} {project.repo}"
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
        + f"(invalid_repo: {invalid_repo}, repo_filter: {repo_filter}, blacklist: {blacklist}, "
        + f"duplicates: {duplicates}, inconsistent: {inconsistent})"
    )

    return projects


@sentry_sdk.trace
async def query_best_versions(
    use_cache: bool, session: Session
) -> dict[str, list[str]]:
    """Query for all software projects and their best version(s) on wikidata."""
    logger.info("Querying wikidata for project versions")
    response = await cached_sparql_query("free_software_versions", use_cache, session)

    best_versions = defaultdict(list)
    for entry in response:
        best_versions[entry["project"]].append(entry["version"])
    best_versions = dict(best_versions)

    logger.info(
        f"Found {len(response)} best versions for {len(best_versions)} projects"
    )

    return best_versions
