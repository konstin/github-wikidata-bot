import json
import os
from typing import Dict, Optional

import requests
from requests import RequestException


class RedirectDict:
    _redirect_dict: Dict[str, str] = {}

    @classmethod
    def get_or_add(cls, start_url: str) -> Optional[str]:
        if not cls._redirect_dict:
            cls._load()
        if start_url in cls._redirect_dict:
            return cls._redirect_dict[start_url]
        else:
            try:
                response = requests.head(start_url, allow_redirects=True, timeout=6.1)
            except RequestException:
                return None
            end_url = response.url
            cls._redirect_dict[start_url] = end_url
            cls._save()
            return end_url

    @classmethod
    def _load(cls):
        if os.path.isfile("redirects.json"):
            with open("redirects.json") as fp:
                cls._redirect_dict = json.load(fp)
        else:
            cls._redirect_dict = dict()

    @classmethod
    def _save(cls):
        with open("redirect.json", "w") as fp:
            json.dump(cls._redirect_dict, fp)
