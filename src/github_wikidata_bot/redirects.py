from __future__ import annotations

import json
from pathlib import Path

from httpx import AsyncClient, HTTPError

from github_wikidata_bot.settings import cache_root


class RedirectDict:
    """Caches HTTP redirects on disk."""

    _redirects: dict[str, str] = {}

    @classmethod
    def redirects_json(cls) -> Path:
        return cache_root().joinpath("redirects.json")

    @classmethod
    async def get_or_add(cls, start_url: str, client: AsyncClient) -> str | None:
        if not cls._redirects:
            cls._load()
        if url := cls._redirects.get(start_url):
            # Cache hit
            return url

        try:
            response = await client.head(start_url, follow_redirects=True, timeout=6.1)
        except HTTPError:
            return None
        end_url = str(response.url)
        cls._redirects[start_url] = end_url
        cls._save()
        return end_url

    @classmethod
    def _load(cls):
        if cls.redirects_json().is_file():
            with cls.redirects_json().open() as fp:
                cls._redirects = json.load(fp)
        else:
            cls._redirects = dict()

    @classmethod
    def _save(cls):
        cls.redirects_json().parent.mkdir(parents=True, exist_ok=True)
        with cls.redirects_json().open("w") as fp:
            json.dump(cls._redirects, fp)
