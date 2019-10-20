from utils import parse_filter_list, github_repo_to_api_releases

exceptions_page = """
This page defines exceptions for [[User:Github-wiki-bot|Github-wiki-bot]]. You can add an item here to stop Github-wiki-bot to update this item.
It must be added on a new line in the form "Q12345" with nothing else in the line. All lines not matching this pattern are ignored.

<pre>
Add exceptions here:
Q1274326
Q4914654 # Comment on this line
Q17064545
</pre>
"""


def test_parse_filter_list():
    expected = ["Q1274326", "Q4914654", "Q17064545"]

    actual = parse_filter_list(exceptions_page)
    assert expected == actual


def test_url_editing_with_fragment():
    url = "https://github.com/data2health/contributor-role-ontology#relevant-publications-and-scholarly-products"
    actual = github_repo_to_api_releases(url)
    expected = (
        "https://api.github.com/repos/data2health/contributor-role-ontology/releases"
    )
    assert actual == expected
