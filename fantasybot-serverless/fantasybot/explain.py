"""Why the bot did what it did, in a sentence you can read on a phone.

A decision you cannot audit is a decision you have to take on faith, and an
autonomous bot that spends your money on faith is not one you will leave running.
So every plan carries its own reasoning: not a log line, a sentence.

Two rules shape this module:

  It is DETERMINISTIC. The explanations come from the same numbers the decision
  came from — the trend, the projection, the starting probability, the reserve —
  so they are true by construction. They do not need the LLM, they cost nothing,
  and they cannot hallucinate a reason for something that happened for another
  one. When the LLM IS on, its own judgement appears alongside these, clearly
  attributed, rather than replacing them.

  It is PURE. Facts in, a string out. No I/O, no clock, no formatting surprises
  when a field is missing — an unknown number is simply left out of the sentence
  rather than printed as "None".

Spanish, because that is the language of the panel and of the game.
"""


def _m(n):
    """A money figure the way you would say it out loud: 5,2M, 850k, 300."""
    if n is None or n == "":
        return None
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    a = abs(n)
    if a >= 1_000_000:
        txt = f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{txt}M"
    if a >= 1_000:
        return f"{round(n / 1_000)}k"
    return f"{round(n)}"


def _join(parts):
    """Assemble the sentence, dropping everything we could not compute."""
    return " ".join(p for p in parts if p)


def _upto(n):
    """" hasta 5.2M", or "" when the figure is unknown.

    The whole point of dropping unknowns is defeated if `None` reaches the phrase
    anyway — a panel that says "Pujo por X hasta None" is worse than one that
    says nothing about the amount.
    """
    money = _m(n)
    return f" hasta {money}" if money else ""


def bid(flip, cap, rival_reach=None):
    """Why we are bidding on a market listing, and why for that much."""
    name = flip.get("nombre") or "el jugador"
    margin = flip.get("margin_pct")
    trend = flip.get("tendencia")
    official = flip.get("oficial_trend_pct")

    head = f"Pujo por {name}{_upto(cap)}."
    why = []
    # The reason it was chosen goes first, because it is what decided: how many
    # points the eleven gains by owning him. The resale margin comes after — it
    # funds the next signing, it does not win the gameweek.
    gain = flip.get("gain")
    if gain is not None:
        per_m = flip.get("gain_per_million")
        why.append(f"Mi once sube {gain:+.2f} puntos por jornada con él"
                   + (f" ({per_m} por millón gastado)." if per_m else "."))
    if margin is not None:
        proj = _m(flip.get("proyeccion"))
        why.append(f"Vale {_m(flip.get('valor_actual'))} y proyecta {proj} "
                   f"a 7 días ({margin:+}%).")
    if trend:
        why.append("Viene subiendo." if trend > 0 else "Viene bajando, "
                   "pero el precio lo compensa.")
    if official is not None:
        why.append(f"El valor oficial se movió {official:+}% en la semana.")
    if rival_reach:
        why.append(f"Techo puesto en la caja del rival más rico ({_m(rival_reach)}): "
                   f"nadie puede pagar más.")
    return _join([head, *why])


def gap_signing(pos, cand, cap, have=None, want=None):
    """Why we are buying for a short position, profitable or not.

    It used to open with "No tengo ningún POR" whatever the squad held, because
    a gap means "below the recommended minimum" and the minimum for keepers is
    two — not one. So a squad with a goalkeeper was told it had none, and the
    sentence was read, reasonably, as a bug in the counting rather than in the
    wording. A claim about the squad now carries the numbers it was made from.
    """
    name = cand.get("nombre") or "un jugador"
    prob = cand.get("prob")
    if have is None:
        opening = f"Me falta un {pos}, así que ficho a {name}{_upto(cap)}."
    elif have == 0:
        opening = f"No tengo ningún {pos}, así que ficho a {name}{_upto(cap)}."
    else:
        opening = (f"Tengo {have} {pos} y quiero {want or have + 1}, así que "
                   f"ficho a {name}{_upto(cap)}.")
    reason = ("Un hueco en la alineación cuesta puntos todas las jornadas, "
              "así que aquí no busco margen: busco tapar el agujero."
              if not have else
              f"Con {have} me quedo sin recambio si se lesiona o le toca "
              f"rotación, y un puesto vacío cuesta puntos toda la jornada.")
    parts = [opening, reason]
    if prob is not None:
        parts.append(f"Tiene {prob}% de ser titular.")
    return _join(parts)


def clause(target, amount):
    """Why we are paying a rival's buyout clause."""
    name = target.get("nombre") or "el jugador"
    amount_txt = _m(amount)
    parts = [f"Pago la cláusula de {name}"
             + (f": {amount_txt}." if amount_txt else ".")]
    pos = target.get("pos")
    if pos:
        parts.append(f"Me tapa el hueco en {pos}.")
    if target.get("prob") is not None:
        parts.append(f"Titularidad {target['prob']}%.")
    saving = target.get("saving_vs_clause")
    if saving:
        parts.append(f"Aun así sale {_m(saving)} más barato que la otra vía.")
    parts.append("La cláusula se abre a una hora exacta y es del primero que "
                 "paga, por eso está programada al segundo.")
    return _join(parts)


def listing(row):
    """Why a player is on the market at that price."""
    name = row.get("nombre") or "el jugador"
    premium = row.get("premium_pct")
    days = row.get("days_listed") or 0
    price, value = _m(row.get("price")), _m(row.get("value"))
    parts = [f"{name} en venta" + (f" a {price}" if price else "")
             + (f" (vale {value})" if value else "") + "."]
    if row.get("in_xi"):
        parts.append("Es titular: solo lo suelto con una prima alta.")
    elif premium is not None and premium <= 0:
        parts.append("Ya quería venderlo, así que lo dejo a precio de mercado.")
    else:
        parts.append("Es suplente útil: lo vendo si me pagan de más.")
    if days >= 2:
        parts.append(f"Lleva {int(days)} días sin ofertas, así que le fui "
                     f"bajando la prima.")
    parts.append("Listarlo no es venderlo: no se va hasta que alguien pague "
                 "la reserva.")
    return _join(parts)


def offer(decision):
    """Why an offer was taken or turned down."""
    name = decision.get("nombre") or "el jugador"
    amount, reserve = decision.get("amount"), decision.get("reserve")
    amount_txt, reserve_txt = _m(amount), _m(reserve)
    if decision.get("action") == "accept":
        parts = [f"Acepto{(' ' + amount_txt) if amount_txt else ' la oferta'} "
                 f"por {name}"
                 + (f": supera su reserva de {reserve_txt}." if reserve_txt
                    else ".")]
        if decision.get("in_xi"):
            parts.append("Era titular, así que me pagaron la prima que pedía.")
        return _join(parts)
    head = f"Rechazo{(' ' + amount_txt) if amount_txt else ' la oferta'} por {name}"
    if "outbid" in str(decision.get("reason", "")):
        return f"{head}: hay otra oferta mejor sobre la mesa."
    parts = [head + (f": está por debajo de su reserva de {reserve_txt}."
                     if reserve_txt else ": no llega a su reserva.")]
    if decision.get("in_xi"):
        parts.append("Es titular; para soltarlo tienen que pagar más.")
    return _join(parts)


def shield(cand):
    name = cand.get("nombre") or "el jugador"
    clause_txt, value_txt = _m(cand.get("clause")), _m(cand.get("value"))
    detail = None
    if clause_txt:
        detail = (f"Su cláusula ({clause_txt}) está al alcance de algún rival"
                  + (f" y vale {value_txt}." if value_txt else "."))
    return _join([f"Blindo a {name}.", detail,
                  "El blindaje es gratis y dura 48h."])


def lineup(result, report_lineup):
    formation = (report_lineup or {}).get("formation")
    if not formation:
        return "No puedo armar un once completo todavía: falta cubrir posiciones."
    shape = "-".join(str(x) for x in formation)
    if not (result or {}).get("changed"):
        return f"Dejo el {shape}: ya era la mejor alineación posible."
    parts = [f"Cambio a {shape}."]
    if (result or {}).get("incomplete"):
        parts.append("Ojo: quedó incompleta, falta fichar para esa línea.")
    else:
        parts.append("Elegida por probabilidad de ser titular y forma actual.")
    return _join(parts)


def skipped_bid(flip, reason):
    name = flip.get("nombre") or "ese jugador"
    return f"No pujo por {name}: {reason}."
