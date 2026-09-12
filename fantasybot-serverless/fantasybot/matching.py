"""Name normalization and matching across sources.

The LaLiga API uses nicknames ("Etta Eyong") and futbolfantasy uses full names
("karl etta eyong"). This module centralizes the matching, previously duplicated
in several places.
"""

import itertools
import unicodedata

# API positions (positionId -> abbreviation). Shared by the strategies.
# 5 = "ENT" (Entrenador/coach), a premium-league slot — labelled so a coach never shows "?".
POS = {1: "POR", 2: "DEF", 3: "MED", 4: "DEL", 5: "ENT"}


def num(value, default=0):
    """A number out of whatever LaLiga sent: int, float, numeric string, or junk.

    Their API returns numeric fields as strings on some endpoints and as numbers
    on others, in the same payload — the squad carries positionId "4" and
    marketValue "2683751" side by side. Every read that then does arithmetic is
    one TypeError away from taking a whole review down, and every read that only
    compares is one silent wrong answer away, which is worse.

    This cost the selling half of the bot: `round(value * 1.15)` on a string
    raises, so no reserve price could be computed, so nobody was ever listed, so
    no offer ever arrived, so nothing ever sold.
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def position_id(pm) -> int:
    """`positionId` as an int, whatever LaLiga felt like sending.

    The API returns numeric fields as strings on some endpoints and as numbers on
    others — `all_players()` does it with marketValue, verified live. A dict keyed
    by int then misses every lookup silently: `{1: "POR"}.get("1")` is None, so a
    squad with three goalkeepers counts zero of them, every line reads as short,
    and the optimiser announces "No goalkeeper in the squad" while three of them
    sit in it. That is precisely what happened, and it cost a bid on a keeper
    nobody needed.

    Anything unreadable becomes 0, which matches no line — the same as today's
    behaviour for a genuinely unknown position, and never a wrong position.
    """
    try:
        return int((pm or {}).get("positionId") or 0)
    except (TypeError, ValueError):
        return 0


def position_of(pm, default=None):
    """The POR/DEF/MED/DEL/ENT label for a player payload."""
    return POS.get(position_id(pm), default)


def normalize(name: str) -> str:
    """lowercase + accent-stripped, to match names across sources."""
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    return n.lower().strip()


def index_by_name(items, key="nombre"):
    """Dict normalized_name -> item."""
    return {normalize(it[key]): it for it in items}


def match_name(nickname: str, full_name: str, index: dict):
    """Finds the `index` entry for a player from the LaLiga API.

    Tries an exact match by nickname and by full name; if that fails, by tokens
    (every significant token of the nickname is found in the key, in order).
    Returns the entry or None.
    """
    nick = normalize(nickname)
    full = normalize(full_name)
    if nick in index:
        return index[nick]
    if full in index:
        return index[full]
    tokens = [t for t in nick.split() if len(t) > 2 or _is_initial(t)]
    if tokens:
        # Only trust a token match if it's UNIQUE. A short/common nickname
        # ("Pedro", "Álvarez") is a subset of several names; returning an arbitrary
        # one is a false positive (what SANITY_MAX_DIFF in flip.py papers over).
        # Ambiguous -> no confident match. The ID crosswalk is the real fix.
        hits = [value for key, value in index.items()
                if _tokens_match(tokens, key.split())]
        if len(hits) == 1:
            return hits[0]
    return None


def _is_initial(token):
    return len(token) == 2 and token[1] == "." and token[0].isalpha()


def _tokens_match(tokens, words):
    """True if the nickname tokens are found in the key's words, in order.

    A name token equals a word or, from 4 chars, is a prefix of one ("Javi" ->
    javier, "Alti" -> altimira). Substrings are not trusted: "Oso" is inside
    cardOSO and OSOrio, and "pedro" inside pedrosa turned unique hits into
    ambiguous ones. Leading initials ("C. Alvarez") must agree with a word
    before the first name match and trailing ones ("John C.") with a word after
    the last; a middle initial ("Pathé I. Ciss") says nothing. A single-word
    key has nothing to check initials against ("R. Terrats" -> terrats)."""
    names = [t for t in tokens if not _is_initial(t)]
    if not names:
        return False
    pos = 0
    first = None
    for t in names:
        while pos < len(words) and not (
                t == words[pos] or (len(t) >= 4 and words[pos].startswith(t))):
            pos += 1
        if pos == len(words):
            return False
        if first is None:
            first = pos
        pos += 1
    if len(words) == 1:
        return True
    lead = list(itertools.takewhile(_is_initial, tokens))
    trail = list(itertools.takewhile(_is_initial, reversed(tokens)))[::-1]
    return (_initials_in_order(lead, words[:first])
            and _initials_in_order(trail, words[pos:]))


def _initials_in_order(initials, words):
    """Each initial takes a distinct word, in order ("D. C." needs a d-word and
    then a c-word after it)."""
    it = iter(words)
    return all(any(w[0] == t[0] for w in it) for t in initials)
