from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
from httpx import AsyncClient

from github_wikidata_bot.session import Session
from github_wikidata_bot.wikidata_api import (
    APIError,
    Claim,
    Item,
    ItemValue,
    ServerError,
    WikibaseMonolingualText,
    WikibaseTime,
    WikidataClient,
    WikidataError,
    parse_claim,
)

API = "https://www.wikidata.org/w/api.php"


@dataclass
class RecordedRequest:
    method: str
    url: str
    params: dict[str, str]
    data: dict[str, str]


class MockTransport(httpx.AsyncBaseTransport):
    """Records requests and returns canned responses."""

    def __init__(self, responses: list[httpx.Response] | None = None):
        self.responses = responses or []
        self.requests: list[RecordedRequest] = []
        self._call_index = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.content:
            data = dict(httpx.QueryParams(request.content.decode()))
        else:
            data = {}
        self.requests.append(
            RecordedRequest(
                method=request.method,
                url=str(request.url.copy_with(params=None)),
                params=params,
                data=data,
            )
        )
        resp = self.responses[self._call_index]
        self._call_index += 1
        return resp


def _json_response(
    body: dict, status_code: int = 200, headers: dict | None = None
) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=body, headers=headers or {})


@asynccontextmanager
async def _make_session(transport: MockTransport) -> AsyncIterator[WikidataClient]:
    async with AsyncClient(
        timeout=Session.http_timeout,
        headers={"User-Agent": "test-agent"},
        transport=transport,
    ) as client:
        yield WikidataClient(client=client, edit_throttle=0, retries=2)


@pytest.mark.anyio
async def test_login():
    transport = MockTransport(
        responses=[
            _json_response({"query": {"tokens": {"logintoken": "abc123+\\"}}}),
            _json_response(
                {"login": {"result": "Success", "lgusername": "TestUser@TestBot"}}
            ),
        ]
    )
    async with _make_session(transport) as session:
        await session.login("TestUser", "TestBot", "secret-password")

    assert transport.requests == [
        RecordedRequest(
            method="GET",
            url=API,
            params={
                "action": "query",
                "meta": "tokens",
                "type": "login",
                "format": "json",
            },
            data={},
        ),
        RecordedRequest(
            method="POST",
            url=API,
            params={},
            data={
                "action": "login",
                "lgname": "TestUser@TestBot",
                "lgpassword": "secret-password",
                "lgtoken": "abc123+\\",
                "format": "json",
            },
        ),
    ]


@pytest.mark.anyio
async def test_login_failure():
    transport = MockTransport(
        responses=[
            # Fetching the login token
            _json_response({"query": {"tokens": {"logintoken": "tok"}}}),
            # The login
            _json_response({"login": {"result": "Failed", "reason": "bad password"}}),
        ]
    )
    async with _make_session(transport) as session:
        with pytest.raises(WikidataError, match="Login failed"):
            await session.login("User", "Bot", "wrong")


@pytest.mark.anyio
async def test_get_entity():
    entity_response = {
        "entities": {
            "Q42": {
                "type": "item",
                "id": "Q42",
                "claims": {
                    "P348": [
                        {
                            "mainsnak": {
                                "snaktype": "value",
                                "property": "P348",
                                "datavalue": {"type": "string", "value": "1.0.0"},
                            },
                            "type": "statement",
                            "id": "Q42$abc-123",
                            "rank": "preferred",
                            "qualifiers": {
                                "P577": [
                                    {
                                        "snaktype": "value",
                                        "property": "P577",
                                        "datavalue": {
                                            "type": "time",
                                            "value": {
                                                "time": "+2023-06-15T00:00:00Z",
                                                "timezone": 0,
                                                "before": 0,
                                                "after": 0,
                                                "precision": 11,
                                                "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
                                            },
                                        },
                                    }
                                ]
                            },
                            "qualifiers-order": ["P577"],
                            "references": [
                                {
                                    "snaks": {
                                        "P854": [
                                            {
                                                "snaktype": "value",
                                                "property": "P854",
                                                "datavalue": {
                                                    "type": "string",
                                                    "value": "https://example.com/release",
                                                },
                                            }
                                        ]
                                    },
                                    "snaks-order": ["P854"],
                                }
                            ],
                        }
                    ],
                    "P856": [
                        {
                            "mainsnak": {
                                "snaktype": "value",
                                "property": "P856",
                                "datavalue": {
                                    "type": "string",
                                    "value": "https://example.com",
                                },
                            },
                            "type": "statement",
                            "id": "Q42$def-456",
                            "rank": "normal",
                        }
                    ],
                },
            }
        }
    }

    transport = MockTransport(responses=[_json_response(entity_response)])
    async with _make_session(transport) as session:
        item = await session.get_entity("Q42")

    assert item.id == "Q42"
    assert "P348" in item.claims
    assert "P856" in item.claims

    version_claim = item.claims["P348"][0]
    assert version_claim.value == "1.0.0"
    assert version_claim.rank == "preferred"
    assert version_claim.id == "Q42$abc-123"

    assert "P577" in version_claim.qualifiers
    date_qual = version_claim.qualifiers["P577"][0]
    assert isinstance(date_qual.value, WikibaseTime)
    assert date_qual.value.time == "+2023-06-15T00:00:00Z"

    assert len(version_claim.sources) == 1
    ref_claim = version_claim.sources[0][0]
    assert ref_claim.property == "P854"
    assert ref_claim.value == "https://example.com/release"


@pytest.mark.anyio
async def test_get_page_text():
    transport = MockTransport(
        responses=[
            _json_response(
                {
                    "query": {
                        "pages": {
                            "12345": {
                                "pageid": 12345,
                                "title": "User:TestBot/Config",
                                "revisions": [
                                    {"slots": {"main": {"*": "Q123\nQ456\nQ789"}}}
                                ],
                            }
                        }
                    }
                }
            )
        ]
    )
    async with _make_session(transport) as session:
        text = await session.get_page_text("User:TestBot/Config")
        assert text == "Q123\nQ456\nQ789"


@pytest.mark.anyio
async def test_save_claim():
    transport = MockTransport(
        responses=[
            _json_response({"query": {"tokens": {"csrftoken": "csrf+\\"}}}),
            _json_response({"success": 1}),
        ]
    )
    async with _make_session(transport) as session:
        claim = Claim(property="P348", value="2.0.0")
        claim.add_qualifier("P577", WikibaseTime.from_iso("2024-01-01T00:00:00Z"))
        claim.add_sources(
            [
                Claim("P854", "https://example.com"),
                Claim("P813", WikibaseTime.from_iso("2024-03-15T00:00:00Z")),
            ]
        )

        await session.save_claims("Q42", [claim], summary="test edit")

    assert transport.requests == [
        RecordedRequest(
            method="GET",
            url=API,
            params={"action": "query", "meta": "tokens", "format": "json"},
            data={},
        ),
        RecordedRequest(
            method="POST",
            url=API,
            params={},
            data={
                "action": "wbeditentity",
                "id": "Q42",
                "data": '{"claims": [{"type": "statement", "mainsnak": {"snaktype": "value", "property": "P348", "datavalue": {"type": "string", "value": "2.0.0"}}, "rank": "normal", "qualifiers": {"P577": [{"snaktype": "value", "property": "P577", "datavalue": {"type": "time", "value": {"time": "+2024-01-01T00:00:00Z", "timezone": 0, "before": 0, "after": 0, "precision": 11, "calendarmodel": "http://www.wikidata.org/entity/Q1985727"}}}]}, "qualifiers-order": ["P577"], "references": [{"snaks": {"P854": [{"snaktype": "value", "property": "P854", "datavalue": {"type": "string", "value": "https://example.com"}}], "P813": [{"snaktype": "value", "property": "P813", "datavalue": {"type": "time", "value": {"time": "+2024-03-15T00:00:00Z", "timezone": 0, "before": 0, "after": 0, "precision": 11, "calendarmodel": "http://www.wikidata.org/entity/Q1985727"}}}]}, "snaks-order": ["P854", "P813"]}]}]}',
                "token": "csrf+\\",
                "summary": "test edit",
                "bot": "1",
                "format": "json",
                "maxlag": "8",
            },
        ),
    ]


def test_has_claim_duplicate():
    item = Item(
        "Q42",
        {
            "claims": {
                "P856": [
                    {
                        "mainsnak": {
                            "snaktype": "value",
                            "property": "P856",
                            "datavalue": {
                                "type": "string",
                                "value": "https://example.com",
                            },
                        },
                        "type": "statement",
                        "id": "Q42$existing",
                        "rank": "normal",
                    }
                ]
            }
        },
    )

    claim = Claim(property="P856", value="https://example.com")
    assert item.has_claim(claim)


def test_has_claim_different_value():
    """Different values for the same property are not considered duplicates."""
    item = Item(
        "Q42",
        {
            "claims": {
                "P348": [
                    {
                        "mainsnak": {
                            "snaktype": "value",
                            "property": "P348",
                            "datavalue": {"type": "string", "value": "1.0.0"},
                        },
                        "type": "statement",
                        "id": "Q42$v1",
                        "rank": "normal",
                    }
                ]
            }
        },
    )

    claim = Claim(property="P348", value="2.0.0")
    assert not item.has_claim(claim)


def test_has_claim_single_valued_skips_different_value():
    """single_valued=True matches if the property has any existing claim."""
    item = Item(
        "Q42",
        {
            "claims": {
                "P856": [
                    {
                        "mainsnak": {
                            "snaktype": "value",
                            "property": "P856",
                            "datavalue": {
                                "type": "string",
                                "value": "https://old-site.com",
                            },
                        },
                        "type": "statement",
                        "id": "Q42$w1",
                        "rank": "normal",
                    }
                ]
            }
        },
    )

    claim = Claim(property="P856", value="https://new-site.com")
    assert item.has_claim(claim, single_valued=True)


@pytest.mark.anyio
async def test_add_claim():
    transport = MockTransport(
        responses=[
            _json_response({"query": {"tokens": {"csrftoken": "tok"}}}),
            _json_response({"success": 1}),
        ]
    )
    async with _make_session(transport) as session:
        item = Item("Q42", {"claims": {}})
        claim = Claim(property="P856", value="https://example.com")
        await session.add_claim(item, claim, summary="add website")

    assert "P856" in item.claims
    assert item.claims["P856"][0].value == "https://example.com"


@pytest.mark.anyio
async def test_remove_claim():
    transport = MockTransport(
        responses=[
            _json_response({"query": {"tokens": {"csrftoken": "tok"}}}),
            _json_response({"success": 1}),
        ]
    )
    async with _make_session(transport) as session:
        claim = Claim(
            property="P856", value="https://old.example.com", id="Q42$old-claim"
        )
        await session.remove_claim(claim, summary="cleanup")

    assert transport.requests == [
        RecordedRequest(
            method="GET",
            url=API,
            params={"action": "query", "meta": "tokens", "format": "json"},
            data={},
        ),
        RecordedRequest(
            method="POST",
            url=API,
            params={},
            data={
                "action": "wbremoveclaims",
                "claim": "Q42$old-claim",
                "token": "tok",
                "summary": "cleanup",
                "bot": "1",
                "format": "json",
                "maxlag": "8",
            },
        ),
    ]


@pytest.mark.anyio
async def test_sparql_query():
    transport = MockTransport(
        responses=[
            _json_response(
                {
                    "results": {
                        "bindings": [
                            {
                                "project": {
                                    "type": "uri",
                                    "value": "http://www.wikidata.org/entity/Q42",
                                },
                                "repo": {
                                    "type": "uri",
                                    "value": "https://github.com/example/repo",
                                },
                            },
                            {
                                "project": {
                                    "type": "uri",
                                    "value": "http://www.wikidata.org/entity/Q100",
                                },
                                "repo": {
                                    "type": "uri",
                                    "value": "https://github.com/other/repo",
                                },
                            },
                        ]
                    }
                }
            )
        ]
    )
    async with _make_session(transport) as session:
        results = await session.sparql_query("SELECT ?project ?repo WHERE { ... }")

    assert len(results) == 2
    assert results[0]["project"] == "http://www.wikidata.org/entity/Q42"
    assert results[1]["repo"] == "https://github.com/other/repo"


@pytest.mark.anyio
async def test_sparql_server_error():
    transport = MockTransport(responses=[httpx.Response(status_code=500)])
    async with _make_session(transport) as session:
        with pytest.raises(ServerError):
            await session.sparql_query("SELECT ...")


@pytest.mark.anyio
async def test_api_error():
    transport = MockTransport(
        responses=[
            _json_response(
                {
                    "error": {
                        "code": "no-such-entity",
                        "info": "Could not find entity Q99999999",
                    }
                }
            )
        ]
    )

    async with _make_session(transport) as session:
        with pytest.raises(APIError) as exc_info:
            await session.get_entity("Q99999999")
    assert exc_info.value.code == "no-such-entity"


@pytest.mark.anyio
async def test_maxlag_retry():
    transport = MockTransport(
        responses=[
            _json_response({"query": {"tokens": {"csrftoken": "tok"}}}),
            _json_response(
                {"error": {"code": "maxlag", "info": "Waiting for ..."}},
                headers={"Retry-After": "0"},
            ),
            _json_response({"success": 1}),
        ]
    )
    async with _make_session(transport) as session:
        claim = Claim(property="P348", value="1.0.0")
        await session.save_claims("Q42", [claim])

    assert len(transport.requests) == 3


@pytest.mark.anyio
async def test_badtoken_retry():
    transport = MockTransport(
        responses=[
            _json_response({"query": {"tokens": {"csrftoken": "old-tok"}}}),
            _json_response(
                {"error": {"code": "badtoken", "info": "Invalid CSRF token"}}
            ),
            _json_response({"query": {"tokens": {"csrftoken": "new-tok"}}}),
            _json_response({"success": 1}),
        ]
    )
    async with _make_session(transport) as session:
        claim = Claim(property="P348", value="1.0.0")
        await session.save_claims("Q42", [claim])

    last_post = transport.requests[-1]
    assert last_post.data["token"] == "new-tok"


def test_claim_round_trip():
    """Parse a claim from API JSON, then serialize it back."""
    api_json = {
        "mainsnak": {
            "snaktype": "value",
            "property": "P348",
            "datavalue": {"type": "string", "value": "3.0.0"},
        },
        "type": "statement",
        "id": "Q42$round-trip",
        "rank": "preferred",
        "qualifiers": {
            "P577": [
                {
                    "snaktype": "value",
                    "property": "P577",
                    "datavalue": {
                        "type": "time",
                        "value": {
                            "time": "+2024-01-15T00:00:00Z",
                            "timezone": 0,
                            "before": 0,
                            "after": 0,
                            "precision": 11,
                            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
                        },
                    },
                }
            ],
            "P548": [
                {
                    "snaktype": "value",
                    "property": "P548",
                    "datavalue": {
                        "type": "wikibase-entityid",
                        "value": {"entity-type": "item", "id": "Q2804309"},
                    },
                }
            ],
        },
        "qualifiers-order": ["P577", "P548"],
        "references": [
            {
                "snaks": {
                    "P854": [
                        {
                            "snaktype": "value",
                            "property": "P854",
                            "datavalue": {
                                "type": "string",
                                "value": "https://github.com/example/repo/releases/tag/v3.0.0",
                            },
                        }
                    ],
                    "P1476": [
                        {
                            "snaktype": "value",
                            "property": "P1476",
                            "datavalue": {
                                "type": "monolingualtext",
                                "value": {"text": "Release 3.0.0", "language": "en"},
                            },
                        }
                    ],
                },
                "snaks-order": ["P854", "P1476"],
            }
        ],
    }

    claim = parse_claim(api_json)
    output = claim.to_json()

    assert output["id"] == "Q42$round-trip"
    assert output["rank"] == "preferred"
    assert output["mainsnak"]["property"] == "P348"
    assert output["mainsnak"]["datavalue"]["value"] == "3.0.0"

    assert output["qualifiers-order"] == ["P577", "P548"]
    assert output["qualifiers"]["P548"][0]["datavalue"]["value"]["id"] == "Q2804309"

    ref = output["references"][0]
    assert ref["snaks-order"] == ["P854", "P1476"]
    assert ref["snaks"]["P1476"][0]["datavalue"]["value"]["text"] == "Release 3.0.0"


def test_claim_build_and_serialize():
    """Build a claim from scratch and check the JSON output."""
    claim = Claim(property="P348", value="2.0.0")
    claim.add_qualifier(
        property="P577", value=WikibaseTime.from_iso("2024-06-01T00:00:00Z")
    )
    claim.add_qualifier("P548", ItemValue("Q2804309"))
    claim.add_sources(
        [
            Claim("P854", "https://example.com/release"),
            Claim("P1476", WikibaseMonolingualText("Release 2.0.0", "en")),
        ]
    )
    claim.rank = "preferred"

    assert claim.to_json() == {
        "type": "statement",
        "mainsnak": {
            "snaktype": "value",
            "property": "P348",
            "datavalue": {"type": "string", "value": "2.0.0"},
        },
        "rank": "preferred",
        "qualifiers": {
            "P577": [
                {
                    "snaktype": "value",
                    "property": "P577",
                    "datavalue": {
                        "type": "time",
                        "value": {
                            "time": "+2024-06-01T00:00:00Z",
                            "timezone": 0,
                            "before": 0,
                            "after": 0,
                            "precision": 11,
                            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
                        },
                    },
                }
            ],
            "P548": [
                {
                    "snaktype": "value",
                    "property": "P548",
                    "datavalue": {
                        "type": "wikibase-entityid",
                        "value": {"entity-type": "item", "id": "Q2804309"},
                    },
                }
            ],
        },
        "qualifiers-order": ["P577", "P548"],
        "references": [
            {
                "snaks": {
                    "P854": [
                        {
                            "snaktype": "value",
                            "property": "P854",
                            "datavalue": {
                                "type": "string",
                                "value": "https://example.com/release",
                            },
                        }
                    ],
                    "P1476": [
                        {
                            "snaktype": "value",
                            "property": "P1476",
                            "datavalue": {
                                "type": "monolingualtext",
                                "value": {"text": "Release 2.0.0", "language": "en"},
                            },
                        }
                    ],
                },
                "snaks-order": ["P854", "P1476"],
            }
        ],
    }
