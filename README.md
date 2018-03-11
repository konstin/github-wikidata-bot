## Github to wikidata bot

Update Wikidata and Wikipedia entries using metadata from GitHub.

For free software projects with a GitHub repository listed in Wikidata,
this script will perform the following steps,
using metadata collected from the GitHub API:

* Import all stable releases and the release dates
* Update the project website
* Normalize the GitHub link
* [Disabled] Update the wikipedia software info box with the new information


## Setup and usage

It is recommended to install dependecies using pipenv (`pipenv install`),
though you can still use pip (`pip install -r requirements.txt`).

[Generate a personal access token on GitHub][github-token]
and paste it to a file called `github_oauth_token.txt`.
Then run this script in a terminal and enter the password for your bot account.

Note that this script uses a http cache for GitHub responses
with the "LastModified" heuristic, so you might need to clear the cache manually
if you want the really latest version.

## Implementation notes

First, a SPARQL query gathers all the free software projects in Wikidata
which have a GitHub repository specified in the [source code repository][repo-property] property.
For each entry, a cached request to the GitHub API is made,
which is authenticated by the OAuth key.
The wikidata entries are then inserted using a "exists or insert" logic.
For each entry, the GitHub api link is added as reference.

## Why does the bot not work for item Q…?

* Does the entity already have a VCS repository set? Use [this query][no-repo-query]
  to determine entities without a repository.
* Does the project use GitHub releases? If not, no automatic update is possible at the moment. (See #5)

## Statistics

You can find detailed statistics on [wmflabs][wmflabs].

[github-token]: https://help.github.com/articles/creating-a-personal-access-token-for-the-command-line/
[repo-property]: https://www.wikidata.org/wiki/Property:P1324
[no-repo-query]: https://github.com/konstin/github-wikidata-bot/blob/master/free_software_without_repository.rq
[wmflabs]: https://xtools.wmflabs.org/ec/wikidata/Github-wiki-bot
