"""A clause is a way to sign a footballer, not a way to fill a slot.

`clause_targets` used to consider ONLY positions the squad was short in, and
then order what survived by probability of starting. Both are wrong for a league
scored on points:

  * a brilliant midfielder nobody else could reach was invisible, because the
    squad already had three midfielders;
  * among the ones it did see, the surest starter won rather than the one who
    adds the most.

Position is not a reason to sign somebody. Points per euro is — the same measure
the market signings are ranked by, so the two routes compete on one scale.
"""

from fantasybot import agent
from tests.support import StorageTestCase


def _rival(pid, pos_id, clause, nombre):
    return {"discr": "marketPlayerTeam", "id": f"m{pid}",
            "playerTeam": {"buyoutClause": str(clause),
                           "buyoutClauseLockedEndTime": "2026-09-20T21:00:00+02:00",
                           "manager": {"managerName": "Rival"}},
            "playerMaster": {"id": pid, "nickname": nombre, "name": nombre,
                             "positionId": str(pos_id), "marketValue": str(clause // 2),
                             "playerStatus": "ok"}}


_TEAM = {"teamMoney": 60_000_000,
         "players": [{"playerMaster": {"id": "own1", "positionId": "3"}}]}


class PositionIsNotTheFilter(StorageTestCase):
    def test_a_player_in_a_position_we_already_have_is_still_a_target(self):
        market = [_rival("r1", 3, 8_000_000, "MedioCrack")]
        got = agent.clause_targets(market, _TEAM, {})
        self.assertEqual([t["nombre"] for t in got], ["MedioCrack"])

    def test_the_best_points_per_euro_comes_first(self):
        market = [_rival("r1", 3, 8_000_000, "Caro"),
                  _rival("r2", 4, 4_000_000, "Rendidor")]
        got = agent.clause_targets(market, _TEAM, {}, upgrades_by_id={
            "r1": {"gain": 3.0, "gain_per_million": 0.37},
            "r2": {"gain": 2.6, "gain_per_million": 0.65},
        })
        self.assertEqual([t["nombre"] for t in got], ["Rendidor", "Caro"])

    def test_an_unvalued_target_falls_to_the_back(self):
        """Not to the front on a probability, which is what used to happen."""
        market = [_rival("r1", 3, 8_000_000, "SinValorar"),
                  _rival("r2", 4, 4_000_000, "Valorado")]
        got = agent.clause_targets(market, _TEAM, {}, upgrades_by_id={
            "r2": {"gain": 1.2, "gain_per_million": 0.3}})
        self.assertEqual([t["nombre"] for t in got], ["Valorado", "SinValorar"])

    def test_the_reason_names_the_points_not_the_position(self):
        market = [_rival("r1", 3, 8_000_000, "Crack")]
        got = agent.clause_targets(market, _TEAM, {}, upgrades_by_id={
            "r1": {"gain": 3.4, "gain_per_million": 0.42}})
        self.assertIn("3.4 pts/jornada", got[0]["reason"])

    def test_one_we_already_own_is_never_a_target(self):
        market = [_rival("own1", 3, 8_000_000, "Mio")]
        self.assertEqual(agent.clause_targets(market, _TEAM, {}), [])

    def test_a_clause_we_cannot_pay_is_not_a_target(self):
        market = [_rival("r1", 3, 900_000_000, "Impagable")]
        self.assertEqual(agent.clause_targets(market, _TEAM, {}), [])


class TheBarReplacesThePositionFilter(StorageTestCase):
    """With position gone, something has to stop it paying a 1.67x premium for a
    player who barely moves the eleven."""

    def test_a_target_that_adds_almost_nothing_is_refused(self):
        from fantasybot import config, tick
        from fantasybot.scheduler import TickContext
        from fantasybot.storage import to_iso, utcnow
        from datetime import timedelta

        flags = (config.AUTO_CLAUSES, config.AUTO_EXECUTE)
        config.AUTO_CLAUSES = config.AUTO_EXECUTE = True
        self.addCleanup(lambda: setattr(config, "AUTO_CLAUSES", flags[0]))
        self.addCleanup(lambda: setattr(config, "AUTO_EXECUTE", flags[1]))

        target = {"player_id": "r1", "nombre": "Tibio", "pos": "MED",
                  "clause": 8_000_000, "gain": 0.05,
                  "unlock": to_iso(utcnow() + timedelta(hours=4))}
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        got = tick._plan_clauses(ctx, "L1", {"teamMoney": 60_000_000},
                                 {"clause_targets": [target]})
        self.assertEqual(got["queued"], [])
        self.assertIn("no paga la prima", got["skipped"][0]["why"])
