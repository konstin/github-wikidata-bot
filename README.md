## Github to wikidata bot

Update Wikidata and Wikipedia entries using metadata from github

For free software projects with a github repository listed in wikidata,
this script will collect the following metadata from the github API
 - all stable releases + release dates
 - the project website
 - [disabled] normalize the github link
 - [WIP] edit the wikipedia entry accordingly

## Setup

Generate a personal access token on github and paste it to a file called
"github_oath_token.txt". Then run this script in a terminal and enter the
password for the bot account.

This script uses an idiomatic file cache, so if you want to get new information
from github, delete the cache folder.
