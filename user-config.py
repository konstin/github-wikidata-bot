import json
from pathlib import Path

family = "wikidata"
mylang = "wikidata"  # Needed for editing of userpages
config_json = Path("config.json")
if config_json.is_file():
    config = json.loads(config_json.read_text())
    username = config["username"]
    test_username = config.get("test-username")
else:
    # Mainly used for pytest
    username = "unknown"
    test_username = None
    config = None
# noinspection PyUnresolvedReferences
usernames["wikidata"]["wikidata"] = username  # type: ignore # noqa: F821
usernames["wikidata"]["test"] = test_username  # type: ignore # noqa: F821
# noinspection PyUnresolvedReferences
usernames["wikipedia"]["en"] = username  # type: ignore # noqa: F821

console_encoding = "utf-8"
put_throttle = 1
minthrottle = 1

# adapt to pywikibot's horrible configuration system
del config
del config_json
del json
del username
del test_username

# See https://github.com/konstin/github-wikidata-bot/issues/115#issuecomment-644403350
# Maxlag. Higher values are more aggressive in seeking access to the wiki.
maxlag = 8

# Maximum number of times to retry an API request before quitting.
max_retries = 30
# Minimum time to wait before resubmitting a failed API request.
retry_wait = 5
# Maximum time to wait before resubmitting a failed API request.
retry_max = 360
