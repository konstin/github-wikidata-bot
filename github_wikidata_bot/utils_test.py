from pywikibot.exceptions import APIError

from .utils import (
    parse_filter_list,
    github_repo_to_api_releases,
    normalize_url,
    is_edit_conflict,
)

exceptions_page = """
This page defines exceptions for [[User:Github-wiki-bot|Github-wiki-bot]]. You can add an item here to stop Github-wiki-bot to update this item.
It must be added on a new line in the form "Q12345" with nothing else in the line. All lines not matching this pattern are ignored.

<pre>
Add exceptions here:
Q1274326
Q4914654 # Comment on this line
Q17064545
</pre>
"""  # noqa: E501


def test_parse_filter_list():
    expected = ["Q1274326", "Q4914654", "Q17064545"]

    actual = parse_filter_list(exceptions_page)
    assert expected == actual


def test_url_editing_with_fragment():
    url = (
        "https://github.com/data2health/contributor-role-ontology"
        "#relevant-publications-and-scholarly-products"
    )
    actual = github_repo_to_api_releases(url)
    expected = (
        "https://api.github.com/repos/data2health/contributor-role-ontology/releases"
    )
    assert actual == expected


def test_repo_normalization():
    url = "git://github.com/certbot/certbot.git"
    actual = str(normalize_url(url))
    expected = "https://github.com/certbot/certbot"
    assert actual == expected


# This is the cron error message
formatted_edit_conflict = """failed-save: The save has failed.
[messages: [{'name': 'wikibase-api-failed-save', 'parameters': [], 'html': {'*': 'The save has failed.'}}, {'name': 'edit-conflict', 'parameters': [], 'html': {'*': 'Edit conflict.'}}];
 help: See https://www.wikidata.org/w/api.php for API usage. Subscribe to the mediawiki-api-announce mailing list at &lt;https://lists.wikimedia.org/postorius/lists/mediawiki-api-announce.lists.wikimedia.org/&gt; for notice of API deprecations and breaking changes.]
""".strip()  # noqa: E501


def test_is_edit_conflict():
    error = APIError(
        "failed-save",
        "The save has failed.",
        messages=[
            {
                "name": "wikibase-api-failed-save",
                "parameters": [],
                "html": {"*": "The save has failed."},
            },
            {
                "name": "edit-conflict",
                "parameters": [],
                "html": {"*": "Edit conflict."},
            },
        ],
        help="See https://www.wikidata.org/w/api.php for API usage. Subscribe to the "
        "mediawiki-api-announce mailing list at &lt;"
        "https://lists.wikimedia.org/postorius/lists/mediawiki-api-announce.lists.wikimedia.org/"
        "&gt; for notice of API deprecations and breaking changes.",
    )
    # Check that we faithfully represent the cron error message
    assert str(error) == formatted_edit_conflict
    assert is_edit_conflict(error)
