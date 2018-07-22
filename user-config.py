import json

family = "wikidata"
mylang = "wikidata"  # Needed for editing of userpages
with open("config.json") as config:
    username = json.load(config)["username"]
    usernames["wikidata"]["wikidata"] = username
    usernames["wikipedia"]["en"] = username

console_encoding = "utf-8"
put_throttle = 5

# adapt to pywikibot's horrible configuration system
del json
del username
del config
