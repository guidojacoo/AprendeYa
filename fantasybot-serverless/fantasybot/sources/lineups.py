"""Probable lineups from futbolfantasy.com.

For each player: probability of starting and status (injury/suspension/
unavailable). A signal for automatic lineup selection.
"""

import re
import sys

from .. import config, net, cache
from ..matching import normalize

_MEMO = None

CACHE_TTL = 1800  # 30 min: probable lineups update as news comes in


def _attr(tag, key):
    m = re.search(key + r'="([^"]*)"', tag)
    return m.group(1) if m else None


def team_slugs():
    """The 20 LaLiga team slugs."""
    html = net.get(config.FF_LINEUPS_INDEX)
    return sorted(set(re.findall(r"/laliga/equipos/([a-z0-9-]+)", html)))


def team_lineup(slug: str):
    """Team players with starting probability and status."""
    html = net.get(config.FF_TEAM_URL.format(slug=slug))
    best = {}
    for tag in re.findall(r'<a class="camiseta[^>]*>', html):
        href = _attr(tag, "href") or ""
        m = re.search(r"/jugadores/([a-z0-9-]+)", href)
        if not m:
            continue
        player_slug = m.group(1)
        prob = (_attr(tag, "data-probabilidad") or "").rstrip("%")
        lesion = _attr(tag, "data-lesion")
        sancionado = _attr(tag, "data-sancionado") == "1"
        nodisponible = _attr(tag, "data-nodisponible") == "1"
        p = {
            "slug": player_slug,
            "nombre": player_slug.replace("-", " "),
            "prob": int(prob) if prob.isdigit() else None,
            "lesionado": lesion not in (None, "-1", "0"),
            "sancionado": sancionado,
            "disponible": not (sancionado or nodisponible),
            "equipo": slug,
        }
        cur = best.get(player_slug)
        if cur is None or (p["prob"] or 0) > (cur["prob"] or 0):
            best[player_slug] = p
    return list(best.values())


def _build_index(slugs):
    idx = {}
    for slug in slugs:
        try:
            for p in team_lineup(slug):
                idx[normalize(p["nombre"])] = p
        except Exception as e:
            print(f"[warning] {slug}: {e}", file=sys.stderr)
    return idx


def probable_lineups(slugs=None):
    """Index normalized_name -> probable lineup info (cached 30 min).

    This is the most expensive read (20 pages). The cache avoids re-scraping on
    every command, and with it the 429s.
    """
    if slugs is not None:
        return _build_index(slugs)
    # Memoised for the life of this process, on top of the TTL cache.
    #
    # The TTL cache stops the SCRAPE repeating; it does not stop the round trip
    # to storage, and that round trip is a full second. `lineup.optimize` reads
    # this index whenever a caller does not hand it one, and the upgrade engine
    # re-solves the eleven once per player — so a single review was paying that
    # second dozens of times over, and a whole-squad search was unusable
    # entirely: ninety evaluations took a hundred and eight seconds, against a
    # function that lives for fifty.
    #
    # A tick is a fresh process and the index cannot change inside one, so this
    # is a cache with no staleness to it.
    global _MEMO
    if _MEMO is None:
        _MEMO = cache.cached("probable_lineups", CACHE_TTL,
                             lambda: _build_index(team_slugs()), default={})
    return _MEMO


def forget_memo():
    """Drop the per-process memo. For tests, and for a long-lived CLI."""
    global _MEMO
    _MEMO = None
