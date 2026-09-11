"""Market value trends from futbolfantasy.com.

A signal the LaLiga API doesn't provide: who's rising and who's falling in value,
with history at 1/2/3/7/14/30 days, trend and acceleration. The basis for deciding flips.
"""

import re

from .. import config, net, cache
from ..matching import index_by_name

NUM_FIELDS = ["valor", "valor1", "valor2", "valor3", "valor7", "valor14",
              "valor30", "tendencia", "aceleracion"]

CACHE_TTL = 1800  # 30 min: market value changes on daily cycles


def _to_num(s):
    for cast in (int, float):
        try:
            return cast(s)
        except (TypeError, ValueError):
            continue
    return None


def _parse(html):
    players = []
    for tag in re.findall(r"<tr\b[^>]*elemento_jugador[^>]*>", html):
        attrs = dict(re.findall(r'data-([\w-]+)="([^"]*)"', tag))
        if "nombre" not in attrs:
            continue
        p = {k: attrs.get(k) for k in ("id", "nombre", "posicion", "equipo")}
        for f in NUM_FIELDS:
            p[f] = _to_num(attrs.get(f))
        players.append(p)
    return players


def market_trends(html=None):
    """List of players with current value, history and trend (cached 30 min)."""
    if html is not None:
        return _parse(html)
    return cache.cached("market_trends", CACHE_TTL,
                        lambda: _parse(net.get(config.FF_MARKET_URL)))


def trends_index(players=None):
    """Dict normalized_name -> player."""
    return index_by_name(players if players is not None else market_trends())
