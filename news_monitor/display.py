"""Replace the internal search identity in public text, not in source links."""

import re
import unicodedata


def name_patterns(config):
    target = config["target"]
    aliases = [target["name"], *target["person_aliases"]]
    patterns = []
    for alias in sorted(set(aliases), key=len, reverse=True):
        letters = re.findall(r"[^\W_]", unicodedata.normalize("NFKC", alias))
        if not letters:
            raise ValueError("empty internal name alias")
        pattern = r"[\W_]*".join(re.escape(letter) for letter in letters)
        patterns.append(re.compile(pattern, re.IGNORECASE))
    return patterns


def display_name(config):
    value = config["target"]["display_name"]
    if not isinstance(value, str):
        raise TypeError("display name must be text")
    if (not value.strip() or len(value) > 100 or value != value.strip()
            or any(ord(char) < 32 or char in "<>" for char in value)):
        raise ValueError("invalid display name")
    value = unicodedata.normalize("NFKC", value)
    if any(pattern.search(value) for pattern in name_patterns(config)):
        raise ValueError("display name must not contain an internal name alias")
    return value


def public_text(value, config):
    value = unicodedata.normalize("NFKC", value)
    replacement = display_name(config)
    for pattern in name_patterns(config):
        value = pattern.sub(lambda _: replacement, value)
    # Avoid duplicated honorifics in common headlines, e.g. "회장님 회장, ...".
    pattern = re.escape(replacement) + r"\s*회장(?:님)?(?=\s|[,.!?…，。은는이가의과와을를께도에으]|$)"
    while True:
        shortened = re.sub(pattern, lambda _: replacement, value)
        if shortened == value:
            return value
        value = shortened


def validate_public_names(report, config):
    if report["target"]["name"] != display_name(config):
        raise ValueError("public target must use display name")
    if not report.get("display_policy", {}).get("person_name_replaced"):
        raise ValueError("missing display-name policy")
    for article in report["articles"]:
        values = [article["title"], article["classification"]["reason"], article["classification"]["evidence"]]
        if any(public_text(value, config) != value for value in values):
            raise ValueError("unreplaced internal name in public text")
