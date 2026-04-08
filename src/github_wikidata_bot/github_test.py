from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient

from github_wikidata_bot.github import GitHubClient, get_data_from_github
from github_wikidata_bot.project import GitHubRepo, WikidataProject
from github_wikidata_bot.settings import Secrets, Settings
from github_wikidata_bot.wikidata_api import WikidataClient
from github_wikidata_bot.wikidata_update import update_wikidata


def test_url_editing_with_fragment():
    url = (
        "https://github.com/data2health/contributor-role-ontology"
        "#relevant-publications-and-scholarly-products"
    )
    actual = GitHubRepo.from_url(url).api_releases()
    expected = (
        "https://api.github.com/repos/data2health/contributor-role-ontology/releases"
    )
    assert actual == expected


def test_repo_normalization():
    url = "git://github.com/certbot/certbot.git"
    actual = str(GitHubRepo.from_url(url))
    expected = "https://github.com/certbot/certbot"
    assert actual == expected


def _json_response(body: dict | list) -> httpx.Response:
    return httpx.Response(status_code=200, json=body, headers={"etag": '"fake-etag"'})


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(status_code=301, headers={"Location": location})


class SequentialTransport(httpx.AsyncBaseTransport):
    """Returns canned responses in order, asserting each request URL matches."""

    def __init__(self, responses: list[tuple[str, httpx.Response]]):
        self.responses = responses
        self.requests: list[httpx.Request] = []
        self._index = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        expected_url, resp = self.responses[self._index]
        self._index += 1
        assert str(request.url) == expected_url, (
            f"Request #{self._index}: expected {expected_url}, got {request.url}"
        )
        return resp


@pytest.mark.anyio
async def test_repo_rename_updates_wikidata(tmp_path, monkeypatch):
    """End-to-end: GitHub reports perl6/nqp -> Raku/nqp rename, Wikidata P1324 is updated."""
    monkeypatch.setattr("github_wikidata_bot.github.cache_root", lambda: tmp_path)

    # Responses in request order. httpx follows 301s automatically, so each
    # redirect is immediately followed by the response at the new URL.
    transport = SequentialTransport(
        [
            # get project info: perl6/nqp -> 301 -> /repositories/1342470
            # (GitHub redirects to numeric ID URLs, not /repos/new-org/new-name)
            (
                "https://api.github.com/repos/perl6/nqp",
                _redirect("https://api.github.com/repositories/1342470"),
            ),
            (
                "https://api.github.com/repositories/1342470",
                _json_response(
                    {
                        "name": "nqp",
                        "owner": {"login": "Raku"},
                        "homepage": None,
                        "license": None,
                    }
                ),
            ),
            # get releases page 1: 301 -> empty list
            (
                "https://api.github.com/repos/perl6/nqp/releases?page=1&per_page=100",
                _redirect(
                    "https://api.github.com/repositories/1342470/releases?page=1&per_page=100"
                ),
            ),
            (
                "https://api.github.com/repositories/1342470/releases?page=1&per_page=100",
                _json_response([]),
            ),
            # get tags (fallback): 301 -> empty list
            (
                "https://api.github.com/repos/perl6/nqp/git/refs/tags",
                _redirect("https://api.github.com/repositories/1342470/git/refs/tags"),
            ),
            (
                "https://api.github.com/repositories/1342470/git/refs/tags",
                _json_response([]),
            ),
            # Wikidata: get_entity with old P1324 URL
            (
                "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q123&format=json",
                _json_response(
                    {
                        "entities": {
                            "Q123": {
                                "type": "item",
                                "id": "Q123",
                                "claims": {
                                    "P1324": [
                                        {
                                            "mainsnak": {
                                                "snaktype": "value",
                                                "property": "P1324",
                                                "datavalue": {
                                                    "type": "string",
                                                    "value": "https://github.com/perl6/nqp",
                                                },
                                            },
                                            "type": "statement",
                                            "id": "Q123$repo-claim",
                                            "rank": "normal",
                                        }
                                    ]
                                },
                            }
                        }
                    }
                ),
            ),
            # Wikidata: CSRF token
            (
                "https://www.wikidata.org/w/api.php?action=query&meta=tokens&format=json",
                _json_response({"query": {"tokens": {"csrftoken": "tok"}}}),
            ),
            # Wikidata: save_claims (POST)
            ("https://www.wikidata.org/w/api.php", _json_response({"success": 1})),
        ]
    )

    settings = Settings()
    secrets = Secrets(
        username="test", bot_name="test", password="test", github_oauth_token="fake"
    )
    async with AsyncClient(transport=transport, follow_redirects=True) as client:
        github_client = GitHubClient(secrets, client, settings)
        secrets = Secrets("bot", "bot", "secret", "secret_github_token", None)
        wikidata = WikidataClient(client, secrets, settings)

        project = await get_data_from_github(
            WikidataProject(
                q_value="Q123", label="NQP", repo=GitHubRepo("perl6", "nqp")
            ),
            allow_stale=False,
            client=github_client,
            settings=settings,
            tags_over_releases=[],
        )
        assert project.canonical_repo == GitHubRepo("Raku", "nqp")

        await update_wikidata(project, settings, wikidata)

    # The final POST should contain the updated repo URL
    body = dict(httpx.QueryParams(transport.requests[-1].content.decode()))
    claims = json.loads(body["data"])["claims"]
    assert claims[0]["mainsnak"]["datavalue"]["value"] == "https://github.com/Raku/nqp"
