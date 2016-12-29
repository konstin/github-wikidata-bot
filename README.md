## Github to wikidata bot

Update Wikidata and Wikipedia entries using metadata from github

For free software projects with a github repository listed in wikidata,
this script will collect the following metadata from the github API

* Import all stable releases and the release dates
* Update the project website
* [Disabled] Normalize the github link
* [Disabled] Update the wikipedia software info box with the new information


## Setup

Generate a personal access token on github and paste it to a file called
"github_oath_token.txt". Then run this script in a terminal and enter the
password for the bot account.

Note that this script uses a http cache for github responsres with the "LastModified"
heuristic, so you might need to clear the cache manually if you want the
really latest version.

## Implementation

First, a SPARQL query gathers all the free software projects in wikidata with 
github repository. For each entry a cached request to the github API is made, which
is authenticated by the oauth key. The wikidata entries are then inserted using a
"exists or insert" logic. For each entry the github api link is added as reference.