from utils import parse_filter_list

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
