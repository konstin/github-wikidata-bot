from main import github_repo_to_api_releases


def test_url_editing_with_fragment():
    url = "https://github.com/data2health/contributor-role-ontology#relevant-publications-and-scholarly-products"
    actual = github_repo_to_api_releases(url)
    expected = (
        "https://api.github.com/repos/data2health/contributor-role-ontology/releases"
    )
    assert actual == expected
