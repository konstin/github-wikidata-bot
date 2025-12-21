import asyncio
import json
import logging
from asyncio import Semaphore
from pathlib import Path

import tqdm
from github_wikidata_bot.settings import Settings
from httpx import AsyncClient

from github_wikidata_bot.sparql import query_projects
from github_wikidata_bot.utils import github_repo_to_api

logger = logging.getLogger(__name__)

config = json.loads(Path("config.json").read_text())
github_oauth_token = config.get("github-oauth-token")


async def query(
    client: AsyncClient, semaphore: Semaphore, url: str, wikidata_id: str
) -> tuple[str, str, int]:
    async with semaphore:
        response = await client.head(
            url, headers={"Authorization": "token " + github_oauth_token}
        )
    return url, wikidata_id, response.status_code


async def main():
    logger.info("# Querying Projects")
    projects = query_projects(Settings(None), None, False)
    logger.info(f"{len(projects)} projects were found")
    semaphore = Semaphore(50)
    async with AsyncClient() as client:
        tasks = [
            query(
                client, semaphore, github_repo_to_api(project.repo), project.wikidata_id
            )
            for project in projects
        ]
        for finished in tqdm.tqdm(asyncio.as_completed(tasks), total=len(projects)):
            url, wikidata_id, status_code = await finished
            if status_code == 404:
                tqdm.tqdm.write(f"{wikidata_id} {url}")


if __name__ == "__main__":
    asyncio.run(main())
