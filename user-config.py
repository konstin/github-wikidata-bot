import json

family = "wikidata"
mylang = "wikidata"  # Needed for editing of userpages
with open("config.json") as config:
    username = json.load(config)["username"]
    # noinspection PyUnresolvedReferences
    usernames["wikidata"]["wikidata"] = username
    # noinspection PyUnresolvedReferences
    usernames["wikipedia"]["en"] = username

console_encoding = "utf-8"
put_throttle = 1
minthrottle = 1

# adapt to pywikibot's horrible configuration system
del json
del username
del config
