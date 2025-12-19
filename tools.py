#!/usr/bin/env python3
import argparse
import asyncio
from random import sample

from github_wikidata_bot.github import get_json_cached, get_all_pages, analyse_release
from github_wikidata_bot.main import logger
from github_wikidata_bot.settings import Settings
from github_wikidata_bot.sparql import query_projects
from github_wikidata_bot.utils import github_repo_to_api, github_repo_to_api_releases
from httpx import AsyncClient


def safe_sample[T](population: list[T], size: int) -> list[T]:
    try:
        return sample(population, size)
    except ValueError:
        return population


async def debug_version_handling(
    threshold: int = 50, size: int = 20, no_sampling: bool = False
):
    logger.setLevel(40)
    async with AsyncClient() as client:
        projects = query_projects()
        if not no_sampling:
            projects = safe_sample(projects, threshold)
        for project in projects:
            project_info = await get_json_cached(github_repo_to_api(project.repo))
            apiurl = github_repo_to_api_releases(project.repo)
            github_releases = await get_all_pages(apiurl, client)
            if not no_sampling:
                github_releases = safe_sample(github_releases, size)
            for github_release in github_releases:
                release = analyse_release(github_release, project_info)
                print(
                    "{:15} | {:10} | {:20} | {:25} | {}".format(
                        release.version if release else "---",
                        release.release_type if release else "---",
                        github_release["tag_name"],
                        repr(project.projectLabel),
                        github_release["name"],
                    )
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", default=50, type=int)
    parser.add_argument("--maxsize", default=20, type=int)
    parser.add_argument("--no-sampling", action="store_true", default=False)
    parser.add_argument("--github-oauth-token")
    args = parser.parse_args()

    settings = Settings(args.github_oauth_token)
    asyncio.run(debug_version_handling(args.threshold, args.maxsize, args.no_sampling))
