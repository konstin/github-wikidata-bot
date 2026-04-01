#!/usr/bin/env python3
import argparse
import asyncio
from pathlib import Path
from random import sample

from pydantic import TypeAdapter

from github_wikidata_bot.github import (
    fetch_json,
    get_releases,
    analyse_release,
    GitHubRepo,
)
from github_wikidata_bot.main import logger
from github_wikidata_bot.settings import Settings, cache_root
from github_wikidata_bot.sparql import (
    filter_projects,
    cached_sparql_query,
    WikidataProject,
)
from httpx import AsyncClient


def safe_sample[T](population: list[T], size: int) -> list[T]:
    try:
        return sample(population, size)
    except ValueError:
        return population


async def debug_version_handling(
    settings: Settings, threshold: int = 50, size: int = 20, no_sampling: bool = False
):
    logger.setLevel(40)
    async with AsyncClient() as client:
        response = cached_sparql_query("free_software_items", False, settings)
        project_list = TypeAdapter(list[WikidataProject]).validate_python(response)
        projects = filter_projects(False, None, project_list, response, settings)
        if not no_sampling:
            projects = safe_sample(projects, threshold)
        for project in projects:
            project_info, _ = await fetch_json(
                GitHubRepo(project.repo).api_base(), client, settings
            )
            assert project_info is not None  # For the type checker
            org_name = project.repo.removeprefix("https://github.com/")
            if not org_name.count("/") == 1:
                raise ValueError(f"Invalid repo URL: {project.repo}")
            repo_cache_root = cache_root().joinpath(org_name)
            github_releases = await get_releases(
                GitHubRepo(project.repo), repo_cache_root, client, False, settings
            )
            if not no_sampling:
                github_releases = safe_sample(github_releases, size)
            for github_release in github_releases:
                release = analyse_release(github_release, project_info, settings)
                print(
                    "{:15} | {:10} | {:20} | {:25} | {}".format(
                        release.version if release else "---",
                        release.release_type if release else "---",
                        github_release["tag_name"],
                        repr(project.projectLabel),
                        github_release["name"],
                    )
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", default=50, type=int)
    parser.add_argument("--maxsize", default=20, type=int)
    parser.add_argument("--no-sampling", action="store_true", default=False)
    parser.add_argument("--github-oauth-token")
    args = parser.parse_args()

    settings = Settings(args.github_oauth_token)
    asyncio.run(
        debug_version_handling(settings, args.threshold, args.maxsize, args.no_sampling)
    )


if __name__ == "__main__":
    main()
