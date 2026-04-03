#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from random import sample

from httpx import AsyncClient

from github_wikidata_bot.github import GitHubClient, analyse_release, get_releases
from github_wikidata_bot.main import logger
from github_wikidata_bot.settings import Secrets, Settings, cache_root
from github_wikidata_bot.sparql import cached_projects_query
from github_wikidata_bot.wikidata_api import WikidataClient


def safe_sample[T](population: list[T], size: int) -> list[T]:
    try:
        return sample(population, size)
    except ValueError:
        return population


async def debug_version_handling(
    github: GitHubClient,
    wikidata: WikidataClient,
    settings: Settings,
    threshold: int = 50,
    size: int = 20,
    no_sampling: bool = False,
):
    logger.setLevel(40)
    projects = await cached_projects_query(False, wikidata, settings, None)
    if not no_sampling:
        projects = safe_sample(projects, threshold)
    for project in projects:
        project_info, _ = await github.fetch_json(project.repo.api_base())
        assert project_info is not None  # For the type checker
        repo_cache_root = (
            cache_root().joinpath(project.repo.org).joinpath(project.repo.project)
        )
        github_releases = await get_releases(
            project.repo, repo_cache_root, github, False
        )
        if not no_sampling:
            github_releases = safe_sample(github_releases, size)
        for github_release in github_releases:
            release = analyse_release(github_release, project_info["name"])
            print(
                "{:15} | {:10} | {:20} | {:25} | {}".format(
                    release.version if release else "---",
                    release.release_type if release else "---",
                    github_release["tag_name"],
                    repr(project.label),
                    github_release["name"],
                )
            )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", default=50, type=int)
    parser.add_argument("--maxsize", default=20, type=int)
    parser.add_argument("--no-sampling", action="store_true", default=False)
    args = parser.parse_args()

    secrets = Secrets.load()
    settings = Settings()
    async with AsyncClient(
        timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent}
    ) as client:
        github = GitHubClient(secrets, client)
        wikidata = WikidataClient(client=client, settings=settings)
        await wikidata.connect(secrets, settings)
        await debug_version_handling(
            github, wikidata, settings, args.threshold, args.maxsize, args.no_sampling
        )


if __name__ == "__main__":
    asyncio.run(main())
