import json
import sys
from pathlib import Path

family = "wikidata"
mylang = "wikidata"  # Needed for editing of userpages
config_json = Path("config.json")
if config_json.is_file():
    username = json.loads(config_json.read_text())["username"]
else:
    # Mainly used for pytest
    username = "unknown"
# noinspection PyUnresolvedReferences
usernames["wikidata"]["wikidata"] = username  # noqa: F821
# noinspection PyUnresolvedReferences
usernames["wikipedia"]["en"] = username  # noqa: F821

console_encoding = "utf-8"
put_throttle = 1
minthrottle = 1

# adapt to pywikibot's horrible configuration system
del config_json
del json
del username

# See https://github.com/konstin/github-wikidata-bot/issues/115#issuecomment-644403350
# Maxlag. Higher values are more aggressive in seeking access to the wiki.
maxlag = 8

# Maximum number of times to retry an API request before quitting.
max_retries = 30
# Minimum time to wait before resubmitting a failed API request.
retry_wait = 5
# Maximum time to wait before resubmitting a failed API request.
retry_max = 360
