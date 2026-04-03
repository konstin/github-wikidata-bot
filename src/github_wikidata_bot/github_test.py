from __future__ import annotations

from github_wikidata_bot.project import GitHubRepo


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
