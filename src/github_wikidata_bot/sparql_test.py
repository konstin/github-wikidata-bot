from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from github_wikidata_bot.session import parse_filter_list
from github_wikidata_bot.sparql import cached_projects_query


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

    session = MagicMock()
    session.denylist = parse_filter_list(exceptions_page)
    session.retries = 1

    with patch(
        "github_wikidata_bot.sparql.cached_sparql_query",
        new_callable=AsyncMock,
        return_value=sparql_response,
    ):
        projects = await cached_projects_query(
            use_cache=False, session=session, project_filter=None
        )

    assert [project.q_value for project in projects] == ["Q99999"]
