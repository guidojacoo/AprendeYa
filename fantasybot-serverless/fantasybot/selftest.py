"""A diagnosis, not a heartbeat.

`/api` answers "is the process up", which is almost never the question. The
question is "which of the eight things this bot depends on is the broken one" —
and when a deployment goes quiet, guessing at that from the outside is miserable:
an expired token, a paused database, a scraper whose HTML moved and a scheduler
that never fired all look identical from the dashboard.

So each dependency is checked on its own, and each one fails on its own. One
broken check must never hide the seven that are fine, which is why every probe is
individually guarded and why the result is a list rather than a boolean.

Checks are READ-ONLY. Running a diagnosis must never place a bid, pay a clause or
sell anybody — you run this when things are already strange.
"""

import time

from . import config, net, notify
from .storage import get_storage, parse_iso, to_iso, utcnow

OK, WARN, FAIL = "ok", "warn", "fail"


# A diagnosis runs in the same 60-second box everything else does, and a probe
# that scrapes or calls an LLM can eat most of it. Running out of time must not
# turn the report into Vercel's HTML error page — the whole point is to SEE what
# is wrong.
BUDGET_SECONDS = 40

_deadline = [0.0]


def _out_of_time(margin=2.0):
    return time.monotonic() + margin >= _deadline[0]


def _check(name, fn, optional=False):
    """Run one probe. Its failure is data, not an exception."""
    if _out_of_time():
        return {"check": name, "status": WARN, "ms": 0,
                "detail": "Sin tiempo para comprobarlo en esta pasada."}
    started = time.monotonic()
    try:
        status, detail = fn()
    except Exception as e:                       # noqa: BLE001
        status = WARN if optional else FAIL
        detail = f"{type(e).__name__}: {e}"
    return {"check": name, "status": status, "detail": detail,
            "ms": round((time.monotonic() - started) * 1000)}


def _storage():
    store = get_storage()
    if store.kind != "supabase":
        return WARN, (f"Usando almacenamiento '{store.kind}'. En Vercel debería "
                      f"ser 'supabase' o el estado se pierde entre ejecuciones.")
    store.get_settings()          # a real query, not a ping
    return OK, f"Supabase responde (scope '{config.STORAGE_SCOPE}')."


def _tokens():
    from . import auth
    tokens = get_storage().get_doc("tokens", None)
    if not tokens:
        if config.FANTASY_REFRESH_TOKEN:
            return WARN, ("Sin tokens guardados; arrancaría desde "
                          "FANTASY_REFRESH_TOKEN.")
        return FAIL, ("No hay sesión de LaLiga. Hacé `python -m fantasybot login` "
                      "y subila con `python scripts/migrate-state.py --tokens`.")
    exp = auth.jwt_exp(tokens.get("refresh_token") or "")
    if not exp:
        return OK, "Sesión guardada (sin fecha de caducidad legible)."
    days = (exp - utcnow().timestamp()) / 86400
    if days <= 0:
        return FAIL, "La sesión de LaLiga caducó. Hay que repetir el login."
    status = WARN if days <= config.TOKEN_WARN_DAYS else OK
    return status, f"Sesión válida {days:.0f} días más."


def _api(ctx):
    me = ctx["client"].me() or {}
    name = me.get("managerName") or me.get("name") or me.get("id")
    return OK, f"API de LaLiga responde. Sos '{name}'."


def _league(ctx):
    from .tick import league_ids
    lid, tid = league_ids(ctx["client"])
    return OK, f"Liga {lid}, equipo {tid}."


def _squad(ctx):
    from .tick import league_ids
    lid, tid = league_ids(ctx["client"])
    team = ctx["client"].team(lid, tid)
    n = len(team.get("players") or [])
    money = team.get("teamMoney")
    if n == 0:
        return FAIL, "La plantilla vino vacía."
    status = WARN if n < 11 else OK
    return status, (f"{n} jugadores, saldo {int(money or 0):,} €."
                    + ("  Menos de 11: no se puede alinear." if n < 11 else ""))


def _market(ctx):
    from .tick import league_ids
    lid, _ = league_ids(ctx["client"])
    rows = ctx["client"].market(lid) or []
    mine = sum(1 for r in rows if r.get("discr") == "marketPlayerTeam")
    return OK, f"{len(rows)} elementos en el mercado ({mine} de equipos)."


def _sources():
    from .sources.lineups import probable_lineups
    from .sources.market_trends import trends_index
    trends, lineups = len(trends_index() or {}), len(probable_lineups() or {})
    if trends < 100 or lineups < 100:
        return WARN, (f"Fuentes externas flojas: {trends} tendencias, {lineups} "
                      f"alineaciones. El bot sigue, pero decide con menos datos.")
    return OK, f"futbolfantasy OK: {trends} tendencias, {lineups} alineaciones."


def _scheduler():
    rows = get_storage().recent_executions(limit=1)
    if not rows:
        return FAIL, ("Ninguna ejecución registrada. GitHub Actions nunca llamó "
                      "a /api/tick — revisá BOT_CRON_SECRET y VERCEL_APP_URL en "
                      "Settings > Secrets and variables > Actions.")
    last = rows[0]
    at = parse_iso(last.get("started_at"))
    mins = (utcnow() - at).total_seconds() / 60 if at else None
    if mins is None:
        return WARN, "Hay ejecuciones, pero sin fecha legible."
    if mins > 30:
        return WARN, (f"La última ejecución fue hace {mins:.0f} min. El cron "
                      f"debería correr cada 5.")
    return OK, f"Última ejecución hace {mins:.0f} min ({last.get('status')})."


def _db_scheduler():
    """Whether the database is waking the bot itself (migration 0002).

    Worth its own line because it is the difference between depending on
    GitHub's best-effort scheduler and owning your clock.
    """
    store = get_storage()
    if store.kind != "supabase":
        return WARN, "Sin Supabase no hay reloj en la base."
    try:
        rows = store._request("GET", "scheduler_config",
                              params={"select": "app_url,enabled", "limit": "1"})
    except Exception:
        return WARN, ("No está aplicado 0002_scheduler.sql. El bot depende del "
                      "cron de GitHub, que es best-effort. Aplicalo para que la "
                      "propia base lo despierte cada minuto.")
    if not rows:
        return WARN, "scheduler_config existe pero está vacía."
    if not rows[0].get("enabled"):
        return WARN, "El reloj de la base está desactivado (enabled = false)."
    return OK, f"La base despierta al bot cada minuto ({rows[0].get('app_url')})."


def _llm():
    from .llm import client as llm_client
    if not llm_client.enabled():
        return WARN, "Sin LLM. El bot funciona igual, con reglas deterministas."
    info = llm_client.describe()
    llm_client.complete("Responde solo: ok", "ok", max_tokens=5, timeout=12)
    return OK, f"{info['provider']} responde ({info['model']})."


def _notifications():
    if not notify.enabled():
        return WARN, ("Sin avisos. Si se rompe, te enterás sólo abriendo el "
                      "panel.")
    info = notify.describe()
    via = "Telegram" if info["telegram"] else "webhook"
    return OK, f"Configurados por {via}. Probalos con el botón de al lado."


def _autonomy():
    flags = {"alineación": config.AUTO_LINEUP, "pujas": config.AUTO_BIDS,
             "ventas": config.AUTO_SELLS, "cláusulas": config.AUTO_CLAUSES,
             "blindaje": config.AUTO_SHIELD}
    if not config.AUTO_EXECUTE:
        return WARN, "FANTASYBOT_AUTO_EXECUTE está en false: decide pero no actúa."
    off = [k for k, v in flags.items() if not v]
    if off:
        return WARN, f"Activo, pero sin: {', '.join(off)}."
    return OK, "Autonomía completa: alinea, puja, vende, clausula y blinda."


def run(budget_seconds=BUDGET_SECONDS):
    """Every check, in dependency order. Returns a JSON-serialisable report."""
    _deadline[0] = time.monotonic() + budget_seconds
    # Scrapes obey the same clock, so a cold cache cannot spend the whole
    # diagnosis being polite to a website.
    net.set_deadline(_deadline[0])
    try:
        return _run()
    finally:
        net.clear_deadline()


def _run():
    results = [_check("Almacenamiento (Supabase)", _storage),
               _check("Sesión de LaLiga", _tokens)]

    # The API checks all need a client, and building one needs a valid session —
    # so if the token check already failed, say so once instead of five times.
    ctx = {}
    if results[-1]["status"] != FAIL:
        def _build():
            from .api import FantasyClient
            ctx["client"] = FantasyClient()
            return OK, "Cliente autenticado."
        built = _check("Cliente de la API", _build)
        results.append(built)
        if built["status"] == OK:
            for name, fn in (("Tu usuario", _api), ("Liga y equipo", _league),
                             ("Plantilla", _squad), ("Mercado", _market)):
                results.append(_check(name, lambda f=fn: f(ctx)))

    results += [_check("Fuentes externas", _sources, optional=True),
                _check("Scheduler", _scheduler),
                _check("Reloj en la base", _db_scheduler, optional=True),
                _check("LLM", _llm, optional=True),
                _check("Avisos", _notifications, optional=True),
                _check("Autonomía", _autonomy)]

    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in (OK, WARN, FAIL)}
    return {"at": to_iso(utcnow()), "checks": results, "counts": counts,
            "ok": counts[FAIL] == 0,
            "summary": (f"{counts[OK]} bien, {counts[WARN]} con avisos, "
                        f"{counts[FAIL]} rotas")}
