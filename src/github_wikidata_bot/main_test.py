from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from github_wikidata_bot import github, main
from github_wikidata_bot.github import (
    GitHubRelease,
    Project,
    Release,
    RepositorySummary,
)
from github_wikidata_bot.project import GitHubRepo, WikidataProject
from github_wikidata_bot.run_summary import ProjectOutcome, SkipReason
from github_wikidata_bot.settings import Settings
from github_wikidata_bot.wikidata_api import Claim, Item
from github_wikidata_bot.wikidata_update import Property


@pytest.fixture
def project():
    return WikidataProject("Q42", "Example", GitHubRepo("example", "repo"))


@pytest.mark.anyio
async def test_matching_prerelease_is_not_up_to_date(project, monkeypatch, caplog):
    release = GitHubRelease(
        tag_name="v1.0",
        prerelease=True,
        published_at="2026-01-01T00:00:00Z",
        html_url=f"{project.repo}/releases/tag/v1.0",
    )
    monkeypatch.setattr(
        github,
        "fetch_cached",
        AsyncMock(return_value=({"name": "repo"}, project.repo.api_base())),
    )
    monkeypatch.setattr(github, "get_releases", AsyncMock(return_value=[release]))
    wikidata = MagicMock(edit_counter=0, tags_over_releases=[])
    wikidata.get_entity = AsyncMock(return_value=Item(project.q_value, {}))
    caplog.set_level(logging.INFO)

    result = await main.update_project_with_retries(
        project,
        {project.q_value_url: ["1.0"]},
        False,
        Settings(),
        wikidata,
        MagicMock(),
        RepositorySummary([release], None),
    )

    assert result.outcome is ProjectOutcome.SKIPPED
    assert result.reason is SkipReason.PRERELEASES_ONLY
    assert "Project Q42: skipped (only prereleases)" in caplog.messages


@pytest.mark.anyio
@pytest.mark.parametrize("older_version_missing", [False, True])
async def test_unseen_preferred_version_status(
    project, older_version_missing, monkeypatch
):
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    release = Release("1.0", timestamp, f"{project.repo}/releases/tag/v1.0", "stable")
    github_data = Project(project, [release], None, None, timestamp)
    monkeypatch.setattr(main, "check_fast_path", AsyncMock(return_value=False))
    monkeypatch.setattr(
        main, "get_data_from_github", AsyncMock(return_value=github_data)
    )

    version = Property.software_version.value
    item = Item(project.q_value, {})
    item.claims[version] = [Claim(version, "2.0", rank="preferred")]
    if not older_version_missing:
        item.claims[version].append(Claim(version, "1.0"))
    wikidata = MagicMock(edit_counter=0, tags_over_releases=[])
    wikidata.get_entity = AsyncMock(return_value=item)

    async def save_claims(q_value, claims, maxlag, summary):
        wikidata.edit_counter += 1

    wikidata.save_claims = AsyncMock(side_effect=save_claims)

    result = await main.update_project_with_retries(
        project, {}, False, Settings(), wikidata, MagicMock()
    )

    assert result.reason is SkipReason.PREFERRED_VERSION_NOT_ON_GITHUB
    assert item.claims[version][0].rank == "preferred"
    if older_version_missing:
        assert result.outcome is ProjectOutcome.UPDATED
        assert "latest version left unchanged" in str(result)
        wikidata.save_claims.assert_awaited_once()
    else:
        assert result.outcome is ProjectOutcome.SKIPPED
        wikidata.save_claims.assert_not_awaited()
