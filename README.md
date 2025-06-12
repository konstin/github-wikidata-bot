# GitHub to Wikidata bot

![Tests](https://github.com/konstin/github-wikidata-bot/workflows/Tests/badge.svg)

Update Wikidata entries using metadata from GitHub.

For free software projects with a GitHub repository listed in Wikidata,
this script will perform the following steps,
using metadata collected from the GitHub API:

- Import all stable releases and the release dates, including release data, source, and a source title
- Update the project website
- Normalize the GitHub link

It is possible to [exclude items](https://www.wikidata.org/wiki/User:Github-wiki-bot/Exceptions) from being edited by the bot, and also to [allow using tags](https://www.wikidata.org/w/index.php?title=User:Github-wiki-bot/Whitelist) for projects without GitHub releases.

## Setup and usage

First install python 3.12 and [uv][uv], then run `uv sync`.

[Generate a personal access token on GitHub][github-token]. Create a `config.json` file with that token and your Wikidata username:

```json
{
  "username": "my-wikidata-username",
  "github-oauth-token": "abcdedf1234567"
}
```

Then run `main.py` in a terminal and enter the password for your bot account.

Run `pytest`, `ruff format` and `ruff check` after making code changes.

## Implementation notes

First, a SPARQL query gathers all the free software projects in Wikidata which have a GitHub repository specified in the [source code repository][repo-property] property. For each entry, a cached request to the GitHub API is made, which is authenticated by the OAuth key. The wikidata entries are then inserted using a "exists or insert" logic. For each entry, the GitHub api link is added as reference.

## Why does the bot not work for item Q…?

- Does the entity already have a VCS repository set? Use [this query][no-repo-query]
  to determine entities without a repository.

## Statistics

You can find detailed statistics on [wmflabs][wmflabs].

[uv]: https://docs.astral.sh/uv/
[github-token]: https://help.github.com/articles/creating-a-personal-access-token-for-the-command-line/
[repo-property]: https://www.wikidata.org/wiki/Property:P1324
[no-repo-query]: https://github.com/konstin/github-wikidata-bot/blob/main/src/free_software_without_repository.rq
[wmflabs]: https://xtools.wmflabs.org/ec/wikidata/Github-wiki-bot
[logs]: https://gist.github.com/konstin/9b90ae895ad9a270102415474a56e613
