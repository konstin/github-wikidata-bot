"""Direct Wikidata API client, replacing pywikibot."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

import httpx
import sentry_sdk
from httpx import AsyncClient

from github_wikidata_bot.settings import Settings, Secrets, sparql_dir

logger = logging.getLogger(__name__)


class WikidataError(Exception):
    """Base error for Wikidata operations."""


class APIError(WikidataError):
    """Wikidata API returned an error."""

    def __init__(self, code: str, info: str, **kwargs: Any):
        self.code = code
        self.info = info
        self.other = kwargs

    def is_edit_conflict(self) -> bool:
        """Check if this error is an edit conflict."""
        if messages := self.other.get("messages"):
            for message in messages:
                if message.get("name") == "edit-conflict":
                    return True
        return False

    def __str__(self) -> str:
        base = f"{self.code}: {self.info}"
        if self.other:
            details = "\n  ".join(f"{key}={val}" for key, val in self.other.items())
            return f"{base}\n  {details}"
        return base


class MaxLagError(WikidataError):
    """Server is lagging beyond tolerance."""


class ServerError(WikidataError):
    """SPARQL or API server error."""


@dataclass
class WikibaseTime:
    """Wikibase time value."""

    PRECISION: ClassVar[dict[str, int]] = {
        "billion": 0,
        "hundred_million": 1,
        "ten_million": 2,
        "million": 3,
        "hundred_thousand": 4,
        "ten_thousand": 5,
        "millennium": 6,
        "century": 7,
        "decade": 8,
        "year": 9,
        "month": 10,
        "day": 11,
        "hour": 12,
        "minute": 13,
        "second": 14,
    }

    # Ex) "+2023-01-15T00:00:00Z"
    time: str
    calendarmodel: str = "http://www.wikidata.org/entity/Q1985727"
    precision: int = PRECISION["day"]
    timezone: int = 0
    before: int = 0
    after: int = 0

    @classmethod
    def from_datetime(cls, timestamp: datetime.datetime, calendarmodel: str) -> Self:
        """Create a day-precision WbTime from an ISO 8601 timestamp."""
        time_str = (
            f"+{timestamp.year:04d}-{timestamp.month:02d}-{timestamp.day:02d}T00:00:00Z"
        )
        return cls(time=time_str, precision=cls.PRECISION["day"])

    @classmethod
    def from_iso(cls, iso_timestamp: str) -> Self:
        """Create a day-precision WbTime from an ISO 8601 timestamp."""
        dt = datetime.datetime.fromisoformat(iso_timestamp)
        time_str = f"+{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T00:00:00Z"
        return cls(time=time_str, precision=cls.PRECISION["day"])

    def to_json(self) -> dict[str, Any]:
        return {
            "time": self.time,
            "timezone": self.timezone,
            "before": self.before,
            "after": self.after,
            "precision": self.precision,
            "calendarmodel": self.calendarmodel,
        }


@dataclass
class WikibaseMonolingualText:
    """Wikibase monolingual text value."""

    text: str
    language: str

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "language": self.language}


@dataclass
class ItemValue:
    """Reference to a wikidata item (Q-value)."""

    id: str


@dataclass
class RawValue:
    """A value type we don't know how to handle natively."""

    datatype: str
    value: Any


type SnakValue = str | WikibaseTime | WikibaseMonolingualText | ItemValue | RawValue
ClaimValue = SnakValue | None


def make_datavalue(value: SnakValue) -> dict[str, Any]:
    """Convert a Python value to a Wikibase datavalue dict."""
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, WikibaseTime):
        return {"type": "time", "value": value.to_json()}
    if isinstance(value, WikibaseMonolingualText):
        return {"type": "monolingualtext", "value": value.to_json()}
    if isinstance(value, ItemValue):
        return {
            "type": "wikibase-entityid",
            "value": {"entity-type": "item", "id": value.id},
        }
    if isinstance(value, RawValue):
        return {"type": value.datatype, "value": value.value}
    raise TypeError(f"Unsupported value type: {type(value)}")


def parse_value(datavalue: dict[str, Any]) -> SnakValue:
    """Parse a Wikibase datavalue into a Python value."""
    dtype = datavalue["type"]
    val = datavalue["value"]
    if dtype == "string":
        return val
    if dtype == "time":
        return WikibaseTime(
            time=val["time"],
            precision=val["precision"],
            timezone=val.get("timezone", WikibaseTime.timezone),
            before=val.get("before", WikibaseTime.before),
            after=val.get("after", WikibaseTime.after),
            calendarmodel=val.get("calendarmodel", WikibaseTime.calendarmodel),
        )
    if dtype == "monolingualtext":
        return WikibaseMonolingualText(text=val["text"], language=val["language"])
    if dtype == "wikibase-entityid":
        return ItemValue(id=val["id"])
    return RawValue(datatype=dtype, value=val)


def snak_to_json(
    prop: str, value: ClaimValue, snaktype: str = "value"
) -> dict[str, Any]:
    """Build a snak dict."""
    snak: dict[str, Any] = {"snaktype": snaktype, "property": prop}
    if snaktype == "value":
        assert value is not None
        snak["datavalue"] = make_datavalue(value)
    return snak


@dataclass
class Claim:
    """A Wikidata claim (statement)."""

    property: str
    value: ClaimValue
    qualifiers: dict[str, list[Claim]] = field(default_factory=dict)
    sources: list[list[Claim]] = field(default_factory=list)
    rank: str = "normal"
    id: str | None = None
    snaktype: str = "value"

    def add_qualifier(self, property: str, value: ClaimValue):
        """Add a qualifier to this claim."""
        qualifier = Claim(property, value)
        self.qualifiers.setdefault(property, []).append(qualifier)

    def add_sources(self, sources: list[Claim]):
        """Add a sources group to this claim."""
        self.sources.append(sources)

    def target_equals(self, target: ClaimValue) -> bool:
        """Check if this claim's value matches the given target."""
        if self.value is None or target is None:
            return self.value is target
        return self.value == target

    def to_json(self) -> dict[str, Any]:
        """Convert to wikibase api json format."""
        result: dict[str, Any] = {
            "type": "statement",
            "mainsnak": snak_to_json(self.property, self.value, self.snaktype),
            "rank": self.rank,
        }

        if self.qualifiers:
            quals: dict[str, list[dict]] = {}
            quals_order: list[str] = []
            for property, qualifier_claims in self.qualifiers.items():
                quals[property] = [
                    snak_to_json(property, qualifier.value, qualifier.snaktype)
                    for qualifier in qualifier_claims
                ]
                quals_order.append(property)
            result["qualifiers"] = quals
            result["qualifiers-order"] = quals_order

        if self.sources:
            references = []
            for source_group in self.sources:
                snaks: dict[str, list[dict]] = {}
                snaks_order: list[str] = []
                for source in source_group:
                    snaks.setdefault(source.property, []).append(
                        snak_to_json(source.property, source.value, source.snaktype)
                    )
                    if source.property not in snaks_order:
                        snaks_order.append(source.property)
                references.append({"snaks": snaks, "snaks-order": snaks_order})
            result["references"] = references

        if self.id:
            result["id"] = self.id

        return result


def parse_claim(data: dict[str, Any]) -> Claim:
    """Parse a claim from Wikibase API JSON."""
    mainsnak = data["mainsnak"]
    prop = mainsnak["property"]
    snaktype = mainsnak.get("snaktype", "value")

    if snaktype == "value":
        value = parse_value(mainsnak["datavalue"])
    else:
        value = None

    claim = Claim(
        property=prop,
        value=value,
        rank=data.get("rank", "normal"),
        id=data.get("id"),
        snaktype=snaktype,
    )

    for qualifier_prop, qualifier_snaks in data.get("qualifiers", {}).items():
        for snak in qualifier_snaks:
            snak_type = snak.get("snaktype", "value")
            if snak_type == "value":
                q_value = parse_value(snak["datavalue"])
            else:
                q_value = None
            claim.qualifiers.setdefault(qualifier_prop, []).append(
                Claim(property=qualifier_prop, value=q_value, snaktype=snak_type)
            )

    for ref in data.get("references", []):
        source_group = []
        for ref_prop, ref_snaks in ref.get("snaks", {}).items():
            for snak in ref_snaks:
                snak_type = snak.get("snaktype", "value")
                if snak_type == "value":
                    r_value = parse_value(snak["datavalue"])
                else:
                    r_value = None
                source_group.append(
                    Claim(property=ref_prop, value=r_value, snaktype=snak_type)
                )
        if source_group:
            claim.sources.append(source_group)

    return claim


class Item:
    """A Wikidata item loaded from the API."""

    id: str
    claims: dict[str, list[Claim]]

    def __init__(self, entity_id: str, data: dict[str, Any]):
        self.id = entity_id
        self.claims = {}
        for prop, claim_list in data.get("claims", {}).items():
            self.claims[prop] = [parse_claim(c) for c in claim_list]

    def get_claim(self, property: str, target: ClaimValue) -> Claim | None:
        """Returns an existing claim for this property and the given target value."""
        if property not in self.claims:
            return None
        for claim in self.claims[property]:
            if claim.target_equals(target):
                return claim
        return None

    def has_claim(self, claim: Claim, single_valued: bool = False) -> bool:
        """Check if a claim already exists on this item.

        When `single_valued` is True, returns True if the property already has any
        claim. Otherwise, returns True only if a claim with the exact same value exists.
        """
        existing_claims = self.claims.get(claim.property, [])
        if single_valued and existing_claims:
            return True
        return any(existing.target_equals(claim.value) for existing in existing_claims)


class WikidataClient:
    """Manages authentication and API calls to Wikidata."""

    # Mutable
    client: httpx.AsyncClient
    last_edit_time: float
    request_counter = 0

    # Constants (settings)
    api_url: str
    sparql_url: str
    edit_throttle: float
    max_lag: int
    retries: int
    csrf_token: str | None

    # Session-dynamic
    # https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots
    edit_group_hash: str
    edit_summary: str

    # Loaded from remote.
    denylist: list[str]
    tags_over_releases: list[str]
    licenses: dict[str, str]

    def __init__(self, *, client: AsyncClient, settings: Settings):
        self.client = client
        self.api_url = settings.api_url
        self.sparql_url = settings.sparql_url
        self.edit_throttle = settings.edit_throttle
        self.max_lag = settings.max_lag
        self.retries = settings.retries
        self.csrf_token = None
        self.last_edit_time = 0

        # https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool#For_custom_bots
        self.edit_group_hash = f"{random.randrange(0, 2**48):x}"
        self.edit_summary = f"Update with GitHub data ([[:toollabs:editgroups/b/CB/{self.edit_group_hash}|details]])"

    @sentry_sdk.trace
    async def login(self, username: str, bot_name: str, bot_password: str) -> None:
        """Log in with a bot password."""
        logger.info("Logging in")
        self.request_counter += 1
        response = await self.client.get(
            self.api_url,
            params={
                "action": "query",
                "meta": "tokens",
                "type": "login",
                "format": "json",
            },
        )
        response.raise_for_status()
        login_token = response.json()["query"]["tokens"]["logintoken"]

        login_user = f"{username}@{bot_name}"
        self.request_counter += 1
        response = await self.client.post(
            self.api_url,
            data={
                "action": "login",
                "lgname": login_user,
                "lgpassword": bot_password,
                "lgtoken": login_token,
                "format": "json",
            },
        )
        response.raise_for_status()
        result = response.json()
        if result.get("login", {}).get("result") != "Success":
            raise WikidataError(f"Login failed: {result}")

        logger.info(f"Logged in as {login_user}")

    @sentry_sdk.trace
    async def connect(self, secrets: Secrets, settings: Settings) -> None:
        """Login and fetch initial data."""
        await self.login(secrets.username, secrets.bot_name, secrets.password)

        response = await self.sparql_query(
            sparql_dir().joinpath("free_licenses.rq").read_text()
        )
        self.licenses = {row["spdx"]: row["license"][31:] for row in response}

        logger.info("Fetching allow and deny lists")
        self.denylist = parse_filter_list(
            await self.get_page_text(settings.denylist_page)
        )
        self.tags_over_releases = parse_filter_list(
            await self.get_page_text(settings.tags_over_releases_page)
        )

    @sentry_sdk.trace
    async def _get_csrf_token(self) -> str:
        """Get or reuse a CSRF token for editing."""
        if self.csrf_token:
            return self.csrf_token
        logger.info("Fetching CSRF token")
        self.request_counter += 1
        response = await self.client.get(
            self.api_url, params={"action": "query", "meta": "tokens", "format": "json"}
        )
        response.raise_for_status()
        self.csrf_token = response.json()["query"]["tokens"]["csrftoken"]
        return self.csrf_token

    @sentry_sdk.trace
    async def _throttle(self) -> None:
        """Wait the backoff time to respect edit throttle."""
        now = time.time()
        elapsed = now - self.last_edit_time
        if elapsed < self.edit_throttle:
            logger.info(f"Throttled, sleeping {self.edit_throttle - elapsed:.1f}s")
            await asyncio.sleep(self.edit_throttle - elapsed)
        self.last_edit_time = time.time()

    async def _api_post(self, params: dict[str, str]) -> dict[str, Any]:
        """Make a POST API call with maxlag handling and retries."""
        params["format"] = "json"
        params["maxlag"] = str(self.max_lag)

        last_error = WikidataError("no retries")
        for attempt in range(self.retries):
            self.request_counter += 1
            response = await self.client.post(self.api_url, data=params)

            if 500 <= response.status_code < 600:
                wait = 2**attempt + 1
                logger.warning(
                    f"Server error {response.status_code}, retrying in {wait}s"
                )
                await asyncio.sleep(wait)
                continue

            response.raise_for_status()
            data = response.json()

            if error := data.get("error"):
                if error.get("code") == "maxlag":
                    # Recommendation from https://www.mediawiki.org/wiki/Manual:Maxlag_parameter is 5s, but that's
                    # not enough in my experience.
                    retry_after = int(
                        response.headers.get("Retry-After", str(self.max_lag))
                    )
                    logger.warning(
                        f"Server is lagging behind too much, retrying in {retry_after}s"
                    )
                    await asyncio.sleep(retry_after)
                    last_error = MaxLagError("Max retries exceeded due to server lag")
                    continue
                elif error.get("code") == "badtoken":
                    self.csrf_token = None
                    if "token" in params:
                        params["token"] = await self._get_csrf_token()
                    last_error = WikidataError("bad token")
                    continue
                else:
                    raise APIError(
                        code=error.get("code", "unknown"),
                        info=error.get("info", ""),
                        messages=error.get("messages"),
                        help=error.get("*", ""),
                    )

            return data

        raise last_error

    async def _api_get(self, params: dict[str, str]) -> dict[str, Any]:
        """Make a GET API call with retries."""
        params["format"] = "json"

        for attempt in range(self.retries):
            self.request_counter += 1
            resp = await self.client.get(self.api_url, params=params)

            if 500 <= resp.status_code < 600:
                wait = 2**attempt + 1
                logger.warning(f"Server error {resp.status_code}, retrying in {wait}s")
                await asyncio.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                err = data["error"]
                raise APIError(
                    code=err.get("code", "unknown"),
                    info=err.get("info", ""),
                    messages=err.get("messages"),
                )

            return data

        raise ServerError("Max retries exceeded")

    @sentry_sdk.trace
    async def get_entity(self, entity_id: str) -> Item:
        """Fetch a Wikidata entity by Q-id."""
        logger.info(f"Fetching {entity_id}")
        data = await self._api_get({"action": "wbgetentities", "ids": entity_id})
        entity_data = data["entities"][entity_id]
        return Item(entity_id, entity_data)

    @sentry_sdk.trace
    async def get_page_text(self, title: str) -> str:
        """Get the wikitext content of a page."""
        logger.info(f"Fetching page text for {title}")
        data = await self._api_get(
            {
                "action": "query",
                "titles": title,
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
            }
        )
        pages = data["query"]["pages"]
        page = next(iter(pages.values()))
        return page["revisions"][0]["slots"]["main"]["*"]

    @sentry_sdk.trace
    async def save_claims(
        self, entity_id: str, claims: list[Claim], summary: str = ""
    ) -> None:
        """Create or update claims on an entity via wbeditentity.

        Accepts a single claim or a list of claims to save in one API call.
        If a claim has an `id`, it updates the existing claim.
        Otherwise, it creates a new claim.
        """
        await self._throttle()
        claims_json = [claim.to_json() for claim in claims]
        csrf_token = await self._get_csrf_token()
        await self._api_post(
            {
                "action": "wbeditentity",
                "id": entity_id,
                "data": json.dumps({"claims": claims_json}),
                "token": csrf_token,
                "summary": summary,
                "bot": "1",
            }
        )

    @sentry_sdk.trace
    async def add_claim(self, item: Item, claim: Claim, summary: str = "") -> None:
        """Add a claim to an entity and update the in-memory item."""
        await self.save_claims(item.id, [claim], summary=summary)
        item.claims.setdefault(claim.property, []).append(claim)

    @sentry_sdk.trace
    async def remove_claim(self, claim: Claim, summary: str = "") -> None:
        """Remove a claim by its GUID."""
        assert claim.id, "Claim must have an ID to remove"
        await self._throttle()
        await self._api_post(
            {
                "action": "wbremoveclaims",
                "claim": claim.id,
                "token": await self._get_csrf_token(),
                "summary": summary,
                "bot": "1",
            }
        )

    @sentry_sdk.trace
    async def sparql_query(self, query: str) -> list[dict[str, str]]:
        """Execute a SPARQL query against Wikidata."""
        self.request_counter += 1
        resp = await self.client.get(
            self.sparql_url,
            params={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=120,
        )
        if 500 <= resp.status_code < 600:
            raise ServerError(f"SPARQL server error: {resp.status_code}")
        resp.raise_for_status()

        data = resp.json()
        return [
            {var: val["value"] for var, val in binding.items()}
            for binding in data["results"]["bindings"]
        ]


def parse_filter_list(text: str) -> list[str]:
    q_value_regex = re.compile(r"(Q\d+)\s*(#.*)?")
    filterlist = []
    for line in text.splitlines():
        line = line.strip()
        fullmatch = q_value_regex.fullmatch(line)
        if fullmatch:
            filterlist.append(fullmatch.group(1))
    return filterlist
