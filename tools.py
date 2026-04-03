#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from random import sample

from httpx import AsyncClient

from github_wikidata_bot.github import analyse_release, fetch_json, get_releases
from github_wikidata_bot.main import logger
from github_wikidata_bot.session import Config, Session, cache_root
from github_wikidata_bot.sparql import cached_projects_query


def safe_sample[T](population: list[T], size: int) -> list[T]:
    try:
        return sample(population, size)
    except ValueError:
        return population


async def debug_version_handling(
    session: Session, threshold: int = 50, size: int = 20, no_sampling: bool = False
):
    logger.setLevel(40)
    await session.connect()
    projects = await cached_projects_query(False, session, None)
    if not no_sampling:
        projects = safe_sample(projects, threshold)
    for project in projects:
        project_info, _ = await fetch_json(
            project.repo.api_base(), session.wikidata.client, session
        )
        assert project_info is not None  # For the type checker
        repo_cache_root = (
            cache_root().joinpath(project.repo.org).joinpath(project.repo.project)
        )
        github_releases = await get_releases(
            project.repo, repo_cache_root, session.wikidata.client, False, session
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

    config = Config.load()
    async with AsyncClient(
        timeout=Session.http_timeout, headers={"User-Agent": Session.user_agent}
    ) as client:
        settings = Session(config, client)
        await debug_version_handling(
            settings, args.threshold, args.maxsize, args.no_sampling
        )


if __name__ == "__main__":
    asyncio.run(main())
