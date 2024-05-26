# TODO: Also implement the replacements from https://www.wikidata.org/wiki/Property_talk:P856 after checking
# python `re` support the syntax:
# * r"^[Hh][Tt][Tt][Pp](s?)://W[Ww][Ww]\.(.+)$"  # will be automatically replaced to http\1://www.\2.
# * r"^(https?://www\.[a-z]+\.com)/?\.$"  # will be automatically replaced to \1.
# * r"^(https?://www\.[a-z]+\.com)/?,$"  # will be automatically replaced to \1.
# * r"^(https?):///(www\..+)$"  # will be automatically replaced to \1://\2.
import re

# Pattern list from https://www.wikidata.org/wiki/Property_talk:P856 with minor regex syntax edits.
_other_properties_matchers = [
    # Google+ ID (P2847) property.
    r"^https?://(www\.)?plus\.google\.com/(\d{21}|\+[-\w_À-ÿА-я]+|communities/\d{21})(/(about(\?hl=en)?)?)?$",
    # X username (P2002) property.
    r"^https?://(www\.)?twitter\.com/([A-Za-z0-9_]{1,15})/?$",
    # Instagram username (P2003) property.
    r"^https?://(www\.)?instagram\.com/([a-z0-9_\.]+)/?$",
    # YouTube channel ID (P2397) property.
    r"^https?://(www\.)?youtube\.com/channel/(UC([A-Za-z0-9_\-]){22})/?$",
    # VK ID (P3185) property.
    r"^https?://(www\.)?vk\.com/([A-Za-z0-9_\.]{2,32})/?$",
    # LinkedIn personal profile ID (P6634) property.
    r"^https?://(www\.)?linkedin\.com/in/([\-\&%A-Z0-9a-záâãåäăąćčçéèêěëğîıíłńñøóòôöõřśşșšțúůüýž]+)/?$",
    # Ameblo username (P3502) property.
    r"^https?://(www\.)?ameblo\.jp/([a-z0-9-]{3,24})/?$",
    # Medium username (P3899) property.
    r"^https?://(www\.)?medium\.com/([A-Za-z0-9_\.]{1,30})/?$",
    # Facebook username (P2013) property.
    r"^https?://(www\.)?facebook\.com/([.\d.-]+)/?$",
    # Facebook page ID (P4003) property.
    r"^https?://(www\.)?facebook\.com/pages/([.\d.-]+/[1-9][0-9]+)/?(\?f?ref=[a-z_]+)?(\?sk=info&tab=page_info)?$",
    # YerelNet village ID (P2123) property.
    r"^https?://(www\.)?yerelnet\.org\.tr/koyler/koy\.php\?koyid=([2-9]\d{5})$",
    # Line Blog user ID (P7211) property.
    r"^https?://(www\.)?lineblog\.me/([a-z0-9_]+)/?$",
    # Niconico ID (P11176) property.
    r"^https?://(?:sp\.)?seiga\.nicovideo\.jp/(comic/\d+)[^\d]*$",
    # BookWalker series ID (JP version) (P11259) property.
    r"^https?://bookwalker\.jp/series/(\d+)$",
    # ComicWalker content ID (P11501) property.
    r"^https?://comic-walker\.com/contents/detail/(KDCW_[A-Z]{2}\d{2}[012]0\d{4}0[123]0000_68)/?$",
    # Shōsetsuka ni Narō work ID (P11335) property.
    r"^https?://ncode\.syosetu\.com/(n\d{4}[a-z]{1,2})/?$",
    # Shōsetsuka ni Narō user ID (P11441) property.
    r"^https?://x?mypage\.syosetu\.com/([1-9]\d*|x\d{4}[a-z]{2})/?$",
    # pixiv comic work ID (P11543) property.
    r"^https?://comic\.pixiv\.net/works/(\d+)$",
    # note.com user ID (P11401) property.
    r"^https?://note\.com/([\da-z_]+)/?$",
    # Steam application ID (P1733) property.
    r"^https?://store\.steampowered\.com/app/([1-9]\d{0,6})(?:[^\s]+)?/?$",
    # itch.io URL (P7294) property.
    r"^(https?://(?:[a-z\d\-\_]+)\.itch\.io/[a-z\d\-\_]+)/?$",
    # Game Jolt ID (P12072) property.
    r"^https?://gamejolt\.com/games/[\w_-]+/([\d+]+)/?$",
    # Google Play Store app ID (P3418) property.
    r"^https?://play\.google\.com/store/apps/details\?(?:hl=.+&)?id=([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)$",
]
_other_property_matchers = [
    re.compile(i, re.IGNORECASE) for i in _other_properties_matchers
]


def is_website_other_property(url: str) -> False:
    """Check if the github website url should be represented by a different property than official website (P856), see
    <https://www.wikidata.org/wiki/Property_talk:P856>."""
    for pattern in _other_property_matchers:
        if pattern.match(url):
            return True
    return False
