from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from github_wikidata_bot.settings import Settings
from github_wikidata_bot.sparql import cached_projects_query, cached_sparql_query
from github_wikidata_bot.wikidata_api import parse_filter_list


@pytest.mark.anyio
async def test_cached_sparql_query_retries_transport_errors(monkeypatch, tmp_path):
    """Transient network failures are retried before falling back to the caller."""
    query_dir = tmp_path / "sparql"
    query_dir.mkdir()
    query_dir.joinpath("test_query.rq").write_text("SELECT ?project WHERE {}")
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr("github_wikidata_bot.sparql.sparql_dir", lambda: query_dir)
    monkeypatch.setattr("github_wikidata_bot.sparql.cache_root", lambda: cache_dir)
    sleep = AsyncMock()
    monkeypatch.setattr("github_wikidata_bot.sparql.asyncio.sleep", sleep)

    expected_response = [{"project": "http://www.wikidata.org/entity/Q42"}]
    wikidata = MagicMock()
    wikidata.sparql_query = AsyncMock(
        side_effect=[httpx.ReadTimeout("timed out"), expected_response]
    )
    settings = Settings()
    settings.retries = 2

    response = await cached_sparql_query("test_query", False, wikidata, settings)

    assert response == expected_response
    assert wikidata.sparql_query.await_count == 2
    sleep.assert_awaited_once_with(10)
    assert json.loads(cache_dir.joinpath("test_query.json").read_text()) == response


@pytest.mark.anyio
async def test_denylist_excludes_project():
    """A project on the denylist page is excluded from the results."""

    exceptions_page = """
    This page defines exceptions for [[User:Github-wiki-bot|Github-wiki-bot]].

    <pre>
    Add exceptions here:
    Q1274326
    Q4914654 # Comment on this line
    Q17064545
    </pre>
    """

    sparql_response = [
        {
            "project": "http://www.wikidata.org/entity/Q4914654",
            "projectLabel": "https://github.com/org/denied",
            "repo": "https://github.com/org/denied",
        },
        {
            "project": "http://www.wikidata.org/entity/Q99999",
            "projectLabel": "https://github.com/org/allowed",
            "repo": "https://github.com/org/allowed",
        },
    ]

    wikidata = MagicMock()
    wikidata.denylist = parse_filter_list(exceptions_page)
    settings = Settings()

    with patch(
        "github_wikidata_bot.sparql.cached_sparql_query",
        new_callable=AsyncMock,
        return_value=sparql_response,
    ):
        projects = await cached_projects_query(
            use_cache=False, wikidata=wikidata, settings=settings, project_filter=None
        )

    assert [project.q_value for project in projects] == ["Q99999"]
