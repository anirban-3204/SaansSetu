"""Decides whether to message parents, and writes the message from fixed templates (no free-text model output)."""
from .bands import LABEL_EN, LABEL_HI
from .geo import minutes


def clock(hhmm_text):
    """'07:05' -> '7:05' for people."""
    return hhmm_text.lstrip("0") if not hhmm_text.startswith("00") else hhmm_text


def school_flag(label, school_chance, bad_morning):
    """The PT / assembly flag. It depends only on the forecast for school hours, never on routes or savings."""
    indoors = bool(bad_morning)
    if school_chance is not None:
        why = (f"{round(school_chance * 100)}% chance of {label} air during school hours" if indoors
               else f"Chance of {label} air during school hours is {round(school_chance * 100)}%")
    else:
        why = "Air expected to be bad during school hours" if indoors else "Air expected to stay acceptable during school hours"
    return {"outdoor_pt": "move_indoors" if indoors else "ok", "assembly": "move_indoors" if indoors else "ok", "reason": why}


UNKNOWN_FLAG = {"outdoor_pt": "unknown", "assembly": "unknown", "reason": "No forecast for school hours"}


def decide(town, departures, usual_peak, label, school_chance, bad_morning, already_sent_today, now_iso, recipients=0):
    """
    usual_peak     {'time','pm25','band'} for the usual route
    label          the chance label driving the alert (e.g. 'poor'), or None when the town has no chance model
    school_chance  highest chance of that label during school hours, or None
    bad_morning    True when the morning counts as bad (chance at or above the rule, or by band when there is no chance model)
    """
    rule = {**town["alert_rule"], "label": label}
    best, usual, saving = departures["best"], departures["usual"], departures["saving_vs_usual_pct"]
    chance_txt = (f"the chance of {label} during school hours is {round(school_chance * 100)}%"
                  if school_chance is not None else f"the usual route peaks at {LABEL_EN[usual_peak['band']]}")
    blank = {"rule": rule, "message": None, "sent_at": None, "channel": None, "recipients": 0}

    if best is None:
        out = {"decision": "none", "reason_code": "no_on_time_option",
               "reason": "No route and leaving time reaches school before it starts.", **blank}
    elif saving < rule["min_saving_pct"] or not bad_morning:
        code = "no_meaningful_difference" if saving < rule["min_saving_pct"] else "low_chance"
        needs = f"needs {round(rule['min_chance'] * 100)}%" if school_chance is not None else "needs a bad morning"
        out = {"decision": "none", "reason_code": code,
               "reason": f"Best option cuts dose by {saving}% (needs {rule['min_saving_pct']}%) and {chance_txt} ({needs}).", **blank}
    elif already_sent_today:
        out = {"decision": "none", "reason_code": "already_sent_today",
               "reason": "An alert already went out this morning.", **blank}
    else:
        names = {r["route_id"]: r for r in town["routes"]}
        u, b = names[usual["route_id"]], names[best["route_id"]]
        when, leave = clock(usual_peak["time"]), clock(best["leave"])
        en = f"{u['name']} will likely turn {LABEL_EN[usual_peak['band']]} around {when}. "
        hi = f"{u.get('name_hi', u['name'])} पर लगभग {when} बजे हवा {LABEL_HI[usual_peak['band']]} होने की संभावना है। "
        if best["route_id"] != usual["route_id"]:
            en += f"Take {b['name']}, and leave by {leave}."
            hi += f"{b.get('name_hi', b['name'])} से जाएँ, और {leave} तक निकलें।"
        else:
            en += f"Leave by {leave}."
            hi += f"{leave} तक निकलें।"
        out = {"decision": "send", "reason_code": "saving_and_chance",
               "reason": f"Best option cuts dose by {saving}% and {chance_txt}.",
               "rule": rule, "message": {"en": en, "hi": hi}, "sent_at": now_iso, "channel": "telegram", "recipients": recipients}

    out["school_flag"] = school_flag(label, school_chance, bad_morning)
    return out


def not_before(block, now_minute):
    """Parents are not messaged before alert_rule.earliest. A 'send' reached earlier waits for the next run."""
    earliest = block["rule"].get("earliest") if block.get("rule") else None
    if block["decision"] != "send" or not earliest or now_minute >= minutes(earliest):
        return block
    return {**block, "decision": "none", "reason_code": "too_early",
            "reason": f"Alerts go out from {clock(earliest)}. {block['reason']}",
            "message": None, "sent_at": None, "channel": None, "recipients": 0}


def no_routes(town, label, flag):
    """Forecast-only town: there is no route to advise on, so parents are never messaged. The school flag still applies."""
    rule = {"min_chance": town["alert_rule"]["min_chance"], "label": label}
    return {"decision": "none", "reason_code": "no_routes",
            "reason": "No routes are mapped for this town, so there is no route advice. Forecast and school flag only.",
            "rule": rule, "message": None, "sent_at": None, "channel": None, "recipients": 0,
            "school_flag": flag or dict(UNKNOWN_FLAG)}


def held(reason_code, reason, flag=None):
    return {"decision": "held", "reason_code": reason_code, "reason": reason, "rule": None, "message": None,
            "sent_at": None, "channel": None, "recipients": 0, "school_flag": flag or dict(UNKNOWN_FLAG)}
