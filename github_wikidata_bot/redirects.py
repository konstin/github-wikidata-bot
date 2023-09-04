import json
from pathlib import Path
from typing import Dict, Final, Optional

import requests
from requests import RequestException


class RedirectDict:
    """Caches HTTP redirects on disk."""

    _redirects_json: Final[Path] = Path("redirects.json")
    _redirects: Dict[str, str] = {}

    @classmethod
    def get_or_add(cls, start_url: str) -> Optional[str]:
        if not cls._redirects:
            cls._load()
        if url := cls._redirects.get(start_url):
            # Cache hit
            return url

        try:
            response = requests.head(start_url, allow_redirects=True, timeout=6.1)
        except RequestException:
            return None
        end_url = response.url
        cls._redirects[start_url] = end_url
        cls._save()
        return end_url

    @classmethod
    def _load(cls):
        if cls._redirects_json.is_file():
            with cls._redirects_json.open() as fp:
                cls._redirects = json.load(fp)
        else:
            cls._redirects = dict()

    @classmethod
    def _save(cls):
        with cls._redirects_json.open("w") as fp:
            json.dump(cls._redirects, fp)
