"""The agent: a full review, like a human would do when logging in.

`review()` runs an attentive user's cycle:
  1) Looks at WHAT HAS CHANGED since the last connection (signings against you,
     balance...).
  2) Reviews lineup, market, flip opportunities and squad gaps.
  3) Detects buyout targets and works out WHEN to react (reminders).
  4) Keeps the week's task list (adds/completes on its own).

Returns a structured report. Firing the reminders (cronjobs) and the
notifications are built on top (see README / next steps).
"""

from datetime import date, datetime, timedelta

from . import cache, config, state
from .matching import match_name, num, position_of
from .strategy import captain as captain_mod
from .strategy import flip, needs as needs_mod, sell as sell_mod
from .strategy import lineup as lineup_opt
from .strategy import scoring
from .strategy import upgrades
from .strategy import shield as shield_mod
from .sources.lineups import probable_lineups
from .sources.market_trends import trends_index
from .sources import form
from .sources import matchday
from .sources import value_history


def _parse(iso):
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def market_close(market):
    """Market close time = nearest expiration of a system player."""
    times = [e["expirationDate"] for e in market
             if e.get("discr") == "marketPlayerLeague" and e.get("expirationDate")]
    return min(times) if times else None


MIN_CLAUSE_PROB = 40  # don't recommend BUYING a player unlikely to start: a benchwarmer
                      # (e.g. a backup keeper at ~10%) scores 0, so a buyout on him is
                      # wasted money. Unknown prob (name unmatched) is kept, not penalised.


# Six hours: long enough that a gameweek is read once, short enough that a
# postponed fixture is picked up the same day.
FIXTURE_CACHE_TTL = 6 * 3600


def captain_fixture_difficulty(client) -> dict:
    """{team_id: difficulty of the rival THAT team faces this gameweek} for the captain
    picker (see strategy/captain.py). {} on ANY failure (network hiccup, unexpected API
    shape) -- own try/except, separate from `_premium_extras`'s: a captain picked
    without rival-awareness (today's behaviour) is fine, but a crash here must never
    also cost the coach/captain/bench that `_premium_extras` would otherwise still
    build successfully.
    """
    # Cached, because it stopped being a premium extra. It now shapes every XI
    # and every signing, so it runs on every review — and it costs three calls,
    # one of them `all_players()`, the heaviest read the API has. Twice a review
    # (here and in the lineup step) against a function Vercel kills at 60
    # seconds is a bill worth paying once a gameweek instead.
    #
    # The fixtures for a gameweek do not change during it, so a stale entry is
    # not a wrong answer, only an old one — and `cached` returns the default
    # rather than raising, which keeps the old behaviour when anything fails.
    def _compute():
        week = client.current_week() or {}
        fixtures = client.calendar(week.get("weekNumber")) or []
        players = client.all_players() or []
        return captain_mod.fixture_difficulty_by_team(players, fixtures)

    try:
        return cache.cached("fixture_difficulty", FIXTURE_CACHE_TTL,
                            _compute, default={}) or {}
    except Exception:
        return {}


def _clause_reason(pos, valuation):
    if valuation and valuation.get("gain") is not None:
        return (f"suma {valuation['gain']} pts/jornada al once "
                f"({valuation.get('gain_per_million')} por millón)")
    return f"refuerza {pos}"


def clause_targets(market, team, prob_index, upgrades_by_id=None):
    """Other managers' players worth signing via buyout clause when it opens.

    Ranked by what each one ADDS TO THE ELEVEN PER EURO — the same measure the
    market signings use, so the two routes finally compete on one scale.

    It used to consider only positions the squad was SHORT in, and then order
    them by probability of starting. Both were wrong for a league scored on
    points: a brilliant midfielder nobody could reach was invisible because the
    squad already had three midfielders, and among the ones it did see, the
    surest starter won rather than the one who adds the most. Position is not a
    reason to sign somebody; points per euro is.

    `upgrades_by_id` maps player id to his ranked valuation (see
    strategy.upgrades). Without it the old ordering stands, so the CLI and the
    tests keep working.
    """
    upgrades_by_id = upgrades_by_id or {}
    owned = {p["playerMaster"]["id"] for p in team["players"]}
    money = num(team["teamMoney"])
    targets = []
    for el in market:
        # A row whose owner we cannot read is not a clause target: paying one is
        # irreversible, so an unknown shape is skipped rather than assumed.
        if el.get("discr") != "marketPlayerTeam":
            continue
        pm = el["playerMaster"]
        if pm["id"] in owned:
            continue
        pos = position_of(pm)
        pt = el.get("playerTeam", {})
        clause = num(pt.get("buyoutClause")) or None
        unlock = pt.get("buyoutClauseLockedEndTime")
        # His owner may also have him ON SALE, and bidding there would often be
        # cheaper than the ~1.67x clause. We do not take that route: another
        # manager's player is reached by paying his clause, full stop. The sale
        # price is still read, because it is useful context on the page — it is
        # simply never a plan.
        on_sale = ((num(el.get("salePrice")) or None)
                   if el.get("status") == "on_sale" else None)
        if not (clause and unlock and clause <= money):
            continue
        info = match_name(pm.get("nickname", ""), pm.get("name", ""), prob_index)
        prob = info.get("prob") if info else None
        if prob is not None and prob < MIN_CLAUSE_PROB:
            continue  # benchwarmer: signing him by any route is wasted money
        targets.append({
            "nombre": pm.get("nickname") or pm.get("name"),
            "player_id": pm["id"],
            "pos": pos,
            "clause": clause,
            "unlock": unlock,
            "prob": prob,
            "reason": _clause_reason(pos, upgrades_by_id.get(str(pm["id"]))),
            "gain": (upgrades_by_id.get(str(pm["id"])) or {}).get("gain"),
            "gain_per_million": (upgrades_by_id.get(str(pm["id"]))
                                 or {}).get("gain_per_million"),
            # cheaper route, when there is one
            "market_id": el.get("id") if on_sale else None,
            "sale_price": on_sale,
            "sale_expires": el.get("expirationDate") if on_sale else None,
            # Kept for the page, never acted on: a rival's player is a clause.
            "cheaper_via_bid": False,
            "on_sale_at": on_sale,
            "saving_vs_clause": ((clause - on_sale)
                                 if (clause and on_sale and on_sale < clause) else 0),
        })
    # Best points-per-euro first; a target we could not value falls to the back
    # rather than jumping the queue on a probability.
    targets.sort(key=lambda t: (t.get("gain_per_million") is not None,
                                t.get("gain_per_million") or 0,
                                t.get("gain") or 0,
                                t.get("prob") or 0), reverse=True)
    return targets


def _squad_census(team, market):
    """The squad as the bot sees it: how many of each line, and how many of them
    are standing on the market right now.

    The second half is not decoration. If LaLiga ever drops listed players from
    the squad payload, a bot that lists its whole squad would read itself as
    empty and keep buying — and the only way to tell that apart from a counting
    bug is to see both numbers together.
    """
    mine = {str((r.get("playerMaster") or {}).get("id"))
            for r in market or []
            if r.get("discr") == "marketPlayerTeam"}
    players = team.get("players") or []
    return {
        "counts": needs_mod.squad_counts(team),
        "total": len(players),
        # Named for WHEN it was counted. The market is read again later in the
        # review, after the listing phase has run, so a plain "listed_now" next
        # to a later count of 15 reads as a contradiction when it is only the
        # same squad before and after going up for sale.
        "listed_before_review": sum(
            1 for p in players
            if str((p.get("playerMaster") or {}).get("id")) in mine),
        "position_ids": sorted({repr((p.get("playerMaster") or {}).get("positionId"))
                                for p in players}),
        # Whether the squad payload even carries a price. Without one no reserve
        # can be computed, so nobody is ever listed and nothing can ever sell —
        # and that failure looks exactly like the feature being switched off.
        "with_market_value": sum(
            1 for p in players
            if (p.get("playerMaster") or {}).get("marketValue")),
        "value_sample": [repr((p.get("playerMaster") or {}).get("marketValue"))
                         for p in players[:3]],
    }


def _sync_tasks(gaps, targets, sells, lineup_changed):
    """Keeps the task list: creates missing ones, closes resolved ones."""
    # squad gaps
    for pos in ("POR", "DEF", "MED", "DEL"):
        key = f"gap:{pos}"
        if pos in gaps:
            # The count, and the target, in the language the page is written in.
            # "Sign POR: you're short in that position" over a squad holding a
            # goalkeeper reads as a lie; "Tengo 1 POR, quiero 2" reads as a plan.
            short = gaps[pos] if isinstance(gaps, dict) else None
            want = needs_mod.MIN_SQUAD.get(pos)
            have = (want - short) if (short is not None and want) else None
            state.add_task(
                (f"Fichar un {pos}: tengo {have} y quiero {want}."
                 if have is not None else
                 f"Fichar un {pos}: voy corto en ese puesto."), key=key)
        else:
            state.complete_by_key(key)
    # buyout targets (and close the ones that no longer apply)
    for t in targets:
        if t.get("cheaper_via_bid"):
            text = (f"Bid for {t['nombre']} ({t['pos']}): he's ON SALE at "
                    f"{t['sale_price']:,}, {t['saving_vs_clause']:,} less than his "
                    f"{t['clause']:,} clause. Closes {t['sale_expires']}.")
            due = t["sale_expires"]
        else:
            text = (f"Clausular a {t['nombre']} ({t['pos']}) por "
                    f"{t['clause']:,} € cuando abra: "
                    f"{t.get('reason') or 'refuerza el once'}.")
            due = t["unlock"]
        state.add_task(text, due=due, key=f"clause:{t['player_id']}")
    state.complete_missing("clause:", {f"clause:{t['player_id']}" for t in targets})
    # recommended sales
    for s in sells:
        state.add_task(f"Sell {s['nombre']} (~{s['sale_price']:,}): {s['reason']}.",
                       key=f"sell:{s['player_id']}")
    state.complete_missing("sell:", {f"sell:{s['player_id']}" for s in sells})
    # lineup
    if lineup_changed:
        state.add_task("Update lineup (there's a better XI).", key="lineup")
    else:
        state.complete_by_key("lineup")


def _current_lineup(client, team_id):
    """Current lineup as (xi_ids, coach_id, captain_id).

    `coach`/`captain` are premium-only fields on the GET formation (absent -> None). Exposed
    so apply_lineup can detect a captain/coach change that leaves the XI unchanged (else we'd
    never PUT the new captain). One API call; both act paths reuse it.
    """
    lu = client.lineup(team_id)
    f = lu.get("formation", {})
    ids = set()
    for pos in ("goalkeeper", "defender", "midfield", "striker"):
        for p in f.get(pos, []) or []:
            ids.add(p.get("playerTeamId") or p["playerMaster"]["id"])
    coach = None
    for c in f.get("coach", []) or []:
        coach = c.get("playerTeamId") or (c.get("playerMaster") or {}).get("id")
    captain = f.get("captain") or None
    return ids, coach, captain


def _current_xi_ids(client, team_id):
    """Just the current XI ids (back-compat wrapper over `_current_lineup`)."""
    return _current_lineup(client, team_id)[0]


def lineup_lock_reminder(kickoff, now=None):
    """The "set your FINAL LINEUP" reminder — but ONLY on the day the matchday's first
    match is actually played.

    During the odd early-season gameweeks the next kickoff can be several days out, and
    surfacing "set your lineup" that early just confuses (a user saw it for a jornada
    that didn't start until 3 days later). We compare calendar DAYS in Spain time, so the
    notice appears on match day itself and not before. Returns the reminder dict, or None.
    """
    dt = _parse(kickoff) if kickoff else None
    if not dt:
        return None
    tz = matchday.SPAIN_TZ
    now = now or datetime.now(tz)
    if dt.astimezone(tz).date() != now.astimezone(tz).date():
        return None  # first match isn't today -> don't nag about the lineup yet
    return {
        "key": f"lineup_lock:{kickoff}",
        "fire_at": (dt - timedelta(hours=2)).isoformat(),
        "event_at": kickoff,
        "message": "Matchday is today: set your FINAL LINEUP.",
    }


def league_allows_premium_formations(client, lid) -> bool:
    """True when this league is premium AND unlocks the extra formations (so the optimizer
    may use the 2-midfielder shapes). Reads config.premiumFeatures.formations from leagues().
    Any error / unknown league / lid None -> False (safe default: standard formations only)."""
    if not lid:
        return False
    try:
        for lg in client.leagues():
            if str(lg.get("id")) == str(lid):
                return bool((lg.get("config") or {}).get("premiumFeatures", {}).get("formations"))
    except Exception:
        return False
    return False


def review(client, days_to_matchday=None):
    lid, tid = client.default_ids()
    team = client.team(lid, tid)
    market = client.market(lid)
    prob_index = probable_lineups()
    # The last few gameweeks, from LaLiga's own stats. Two signals the bot could
    # not see: whether a player is in form right now rather than on the season
    # average, and whether he is actually being PICKED — which the scraped
    # probability cannot tell us about a man his manager has quietly dropped,
    # and which keeps standing when that scrape breaks. Never fatal: a failure
    # here leaves every estimate exactly where it was before.
    form_index = {}

    # date of the next matchday (for urgency and final lineup)
    kickoff = matchday.next_kickoff()
    if days_to_matchday is None:
        days_to_matchday = matchday.days_until_matchday()

    # 1) what has changed
    prev = state.load_snapshot()
    curr = state.snapshot(team)
    events = state.diff_snapshots(prev, curr)
    state.save_snapshot(curr)

    # Bank today's OFFICIAL market values (all_players(), competition-wide — not just our
    # squad) so we build our OWN value history over time, independent of the futbolfantasy
    # scrape. Purely additive collection: a hiccup here must never break the review.
    all_players = []
    try:
        all_players = client.all_players() or []
        state.save_value_snapshot(date.today().isoformat(),
                                  value_history.snapshot_from_players(all_players))
    except Exception:
        pass

    # Built from the competition-wide read above rather than from
    # `/stats/week/{n}`, which the first live run showed is the FIXTURE LIST, not
    # player stats. When the rows carry no per-gameweek breakdown this stays
    # empty and every estimate behaves exactly as it did before — and records the
    # row's keys once so the parser can be aimed instead of guessed at again.
    try:
        form_index = form.from_player_rows(all_players)
        if not form_index and all_players:
            form.record_player_shape(all_players)
    except Exception:                            # noqa: BLE001
        form_index = {}

    # 2) lineup — a squad that can't field a valid XI (e.g. no goalkeeper mid-rebuild)
    # must not crash the whole review: report it and carry on so gaps/needs still fire.
    try:
        premium = league_allows_premium_formations(client, lid)
        # Computed for every league now, not only premium ones. It used to feed
        # the captain alone — a premium feature — so a league without it threw
        # the fixture away and fielded the same XI against the leaders as
        # against the bottom club.
        fixture_difficulty = captain_fixture_difficulty(client)
        best = lineup_opt.optimize(team, prob_index, premium=premium,
                                   fixture_difficulty=fixture_difficulty,
                                   form_index=form_index)
        best_ids = lineup_opt.payload_ids(best)
        lineup_changed = best_ids != _current_xi_ids(client, tid)
        lineup_section = {"formation": best["formation"], "changed": lineup_changed,
                          "total": best["total"], "watch": best.get("watch", [])}
    except ValueError as e:
        best, lineup_changed = None, False
        lineup_section = {"formation": None, "changed": False, "total": 0,
                          "watch": [], "note": str(e)}

    # 3) flips, needs and sales
    owned = {p["playerMaster"]["id"] for p in team["players"]}
    # One pass over the market, two readings of it. `flips` is what the bot will
    # act on; `market` is everything it looked at, scored, including what it
    # turned down — which is the half that used to be invisible and the half you
    # ask about when a player you wanted goes to somebody else.
    ops = flip.opportunities(client, lid, owned=owned)
    # What the market actually held when we looked. "No analizó el mercado" and
    # "el mercado no tenía a nadie" are completely different statements and the
    # page was showing the first for both — alarming, and wrong. The whole squad
    # stands permanently listed, so most of what comes back is OURS: the number
    # that matters is how many listings belong to somebody else.
    market_census = {
        "anuncios": len(market),
        "mios": sum(1 for el in (market or [])
                    if str(((el.get("playerMaster") or {}).get("id"))) in
                    {str(p) for p in owned}),
        "ajenos": len(ops),
    }
    flips = [o for o in ops
             if o["margin_pct"] > 0 and o["buy_price"] <= team["teamMoney"]][:5]
    # What each signing would ADD to the eleven, which is the only question that
    # decides a league scored on points. Ranked here, where the squad, the market,
    # the probabilities and this week's fixtures are all in scope at once.
    upgrade_list = upgrades.rank(
        ops, team, upgrades.players_by_id(market),
        money=team["teamMoney"], prob_index=prob_index,
        fixture_difficulty=fixture_difficulty, limit=20,
        form_index=form_index)
    market = scoring.rank(ops, prob_index=prob_index, money=team["teamMoney"],
                          # No limit. "Todo el mercado, puntuado" has to mean
                          # all of it: a verdict on one player and silence on
                          # the next is worse than no list, because you cannot
                          # tell a rejection from an omission.
                          limit=None,
                          # Judged against the man he would actually push out of
                          # the XI, not against an abstract average. "Mejor que
                          # un titular corriente" is not a reason to sign
                          # somebody when your own line is already better.
                          replacement=scoring.replacement_from_xi(best))
    gaps = needs_mod.gaps(team)
    needs_report = needs_mod.advise(client, lid, team, days_to_matchday)
    # A missing lineup (incomplete squad) only skips the lineup itself — sells, flips,
    # clauses and reminders still apply. sell_candidates handles best=None.
    sells = sell_mod.sell_candidates(team, best, trends_index(), prob_index=prob_index)
    # What each of ours is worth to the XI, keyed by roster slot — computed ONCE
    # and read by two consumers. It re-optimises the eleven per player to price
    # what losing him costs, so a second call is sixteen more lineup solves; the
    # review paid ten seconds for that before anyone noticed.
    sell_costs = upgrades.sellable(team, prob_index=prob_index,
                                   fixture_difficulty=fixture_difficulty,
                                   form_index=form_index)

    # 4) buyout targets + reminders
    # The same valuations the market signings are ranked by, keyed for lookup:
    # a clause and a bid are two ways to sign a footballer, and they should be
    # compared on one number rather than each having its own idea of "worth it".
    targets = clause_targets(
        market, team, prob_index,
        upgrades_by_id={str(u.get("player_id")): u for u in upgrade_list
                        if u.get("player_id") is not None})
    reminders = []
    close = market_close(market)
    if close:
        dt = _parse(close)
        if dt:
            reminders.append({
                "key": f"market_close:{close}",
                "fire_at": (dt - timedelta(minutes=5)).isoformat(),
                "event_at": close,
                "message": "Market closes in 5 min: review bids and needs.",
            })
    for t in targets:
        if t.get("cheaper_via_bid"):
            continue   # the recommended route is the OPEN SALE; a "prepare the
                       # buyout" alarm for the same player contradicts the task
        dt = _parse(t["unlock"])
        if dt:
            reminders.append({
                "key": f"clause:{t['player_id']}:{t['unlock']}",
                "fire_at": (dt - timedelta(seconds=60)).isoformat(),
                "event_at": t["unlock"],
                # The reason, in the language the page speaks, and with the
                # number that decides it. "(fills a POR gap)" was both English
                # and out of date: position stopped being why we sign anybody.
                "message": (f"Se abre la cláusula de {t['nombre']}: "
                            f"{t['clause']:,} € — {t.get('reason') or ''}"
                            + (f" · esperarlo cuesta "
                               f"{(t.get('timing') or {}).get('cost_of_waiting')} pts"
                               if (t.get("timing") or {}).get("gameweeks_missed")
                               else "")),
            })
    # The lineup lock is about the NEXT gameweek that hasn't started — not today's match
    # if the current jornada is already under way (its lineup is already locked). A
    # scraper hiccup here must never crash the whole daily review.
    try:
        gw_kickoff = matchday.next_gameweek_kickoff()
    except Exception:
        gw_kickoff = None
    lineup_rem = lineup_lock_reminder(gw_kickoff)
    if lineup_rem:
        reminders.append(lineup_rem)
    reminders.sort(key=lambda r: r["fire_at"])

    _sync_tasks(gaps, targets, sells, lineup_changed)
    state.save_reminders(reminders)

    # Rival cash is derived from the league's FULL transfer history — roughly a
    # hundred paginated requests on a fresh install. That is fine from a laptop
    # and fatal inside a function killed at 60 seconds, which is exactly how the
    # first serverless review died. So the history is walked a few pages per run
    # and the cursor is remembered; until it is complete the estimates are
    # marked partial, and callers that spend money on them (the bid capper) know
    # to ignore them rather than act on half a picture.
    rivals_list = []
    try:
        from .strategy import rivals as rivals_mod
        from .storage import get_storage

        store = get_storage()
        key = f"activity_backfill:{lid}"
        cursor = store.get_doc(key, 0) or 0
        pages = config.ACTIVITY_PAGES_PER_RUN
        rivals_list = rivals_mod.analyze_rivals(
            client, lid, backfill_pages=pages, backfill_from=cursor)
        if cursor or not store.get_doc(f"{key}:done", False):
            # A short page returned means we reached the end of the history.
            got = len(state.load_activity_history(lid) or [])
            prev = store.get_doc(f"{key}:seen", 0) or 0
            if got <= prev:
                store.put_doc(f"{key}:done", True)
                store.put_doc(key, 0)
            else:
                store.put_doc(f"{key}:seen", got)
                store.put_doc(key, cursor + pages)
        partial = not store.get_doc(f"{key}:done", False)
        for r in rivals_list:
            r["partial_history"] = partial
    except Exception:
        rivals_list = []

    result = {
        "events": events,
        # Coerced once, here, where the payload is read. Every consumer
        # downstream formats it, compares it or subtracts from it, and the raw
        # field is a string: `f"{money:,}"` raises on one, silently.
        "money": num(team["teamMoney"]),
        "matchday": {"kickoff": kickoff, "days": days_to_matchday},
        "lineup": lineup_section,
        "flips": flips,
        "market": market,
        "market_census": market_census,
        # The three inputs a score is made of, carried so anything downstream
        # scores a player the SAME way the XI did. Reserve prices were computed
        # without them and fell back to a prior, which is how two keepers who
        # never play were priced as assets.
        "prob_index": prob_index,
        "fixture_difficulty": fixture_difficulty,
        "form_index": form_index,
        "upgrades": upgrade_list,
        # Signings the balance alone cannot reach, each paired with the player
        # who would fund it. Without this the bot is capped at whatever cash
        # happens to be lying around, while a bench player worth nine million
        # and scoring nothing sits there paying for nobody.
        "transfers": upgrades.transfers(
            upgrade_list, team, money=team["teamMoney"],
            prob_index=prob_index, fixture_difficulty=fixture_difficulty,
            form_index=form_index, give_up=sell_costs),
        "gaps": gaps,
        # The ones that stop an XI being fielded at all, as opposed to the ones
        # that merely leave you without a substitute. Only these justify buying
        # at any price.
        "blocking_gaps": needs_mod.blocking_gaps(team, premium),
        # What it actually counted, next to what it concluded. "No tengo ningún
        # POR" while three sit in the squad is a claim with no evidence beside
        # it, and chasing that without the counts cost two deploy cycles. If the
        # two ever disagree again, the disagreement is right here.
        "squad": _squad_census(team, market),
        "needs": needs_report,
        "sells": sells,
        # What each of ours is worth to the XI, keyed by roster slot. Built for
        # the defence: "who should I protect" and "who should I sell" are the
        # same question asked in opposite directions, and answering them in the
        # same currency — points per gameweek — is what stops the bot spending
        # money to defend a bench player it was about to list anyway.
        "points_at_risk": {r["player_team_id"]: r["loss"] for r in sell_costs},
        "clause_targets": targets,
        # Whether the per-gameweek stats are actually parsing. A source that
        # returns {} looks exactly like a quiet week, forever.
        "form": form.describe(form_index),
        "rivals": rivals_list,
        "reminders": reminders,
        "tasks": state.pending_tasks(),
    }

    # 5) defensive shield (blindaje): our most clause-vulnerable valuable player. Only when
    # the squad can actually field an XI (best is not None) — if we can't even line up, the
    # focus is elsewhere (fill the gap first). Reach reuses the `rivals` estimate above (the
    # richest rival's cash) instead of a second API call. Fully guarded: any failure just
    # OMITS the key, it must never break the daily review.
    try:
        if best is not None:
            reach = max(
                ((r.get("estimated_balance") or 0) for r in rivals_list if not r.get("is_me")),
                default=0,
            )
            result["shield"] = shield_mod.shield_candidate(team, reach)
    except Exception:
        pass
    return result
