from __future__ import annotations

import asyncio
import logging
from asyncio import Semaphore

import tqdm
from httpx import AsyncClient

from github_wikidata_bot.session import Config, Session
from github_wikidata_bot.sparql import cached_projects_query

logger = logging.getLogger(__name__)


async def main():
    logger.info("Querying Projects")
    config = Config.load()
    session = Session(config)
    await session.connect()
    projects = await cached_projects_query(False, session, None)
    semaphore = Semaphore(50)
    async with AsyncClient() as client:

        async def query(url: str, wikidata_id: str) -> tuple[str, str, int]:
            async with semaphore:
                response = await client.head(url, headers=session.github_auth_headers)
            return url, wikidata_id, response.status_code

        tasks = [
            query(project.repo.api_base(), project.q_value_url) for project in projects
        ]
        for finished in tqdm.tqdm(asyncio.as_completed(tasks), total=len(projects)):
            url, wikidata_id, status_code = await finished
            if status_code == 404:
                tqdm.tqdm.write(f"{wikidata_id} {url}")


if __name__ == "__main__":
    asyncio.run(main())
