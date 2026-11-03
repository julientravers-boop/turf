#!/usr/bin/env python3
"""
Le Ticket : construit la page du jour à partir de collecte.json.

Entrées  : config.yaml, collecte.json (rempli par la tâche Cowork), ticket-template.html,
           journal-quinte.xlsx (créé au premier run)
Sorties  : ticket.html (la page de mon père), journal-quinte.xlsx mis à jour, message.txt (corps de l'email)

Le script ne lit aucune page web. Il calcule, il écrit, il ne devine rien :
une donnée absente de collecte.json reste absente sur la page.
"""

import datetime as dt
import json
import os
import re
import shutil
import sys

import yaml
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

BASE = os.path.dirname(os.path.abspath(__file__))
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
TYPES = ["quinte_presse", "quinte_valeur", "place"]
TYPES_TXT = {"quinte_presse": "Le Sage", "quinte_valeur": "L'Audacieux", "place": "Simple Placé"}


def chemin(nom):
    return nom if os.path.isabs(nom) else os.path.join(BASE, nom)


def date_fr(d):
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def euros(x):
    if x is None:
        return ""
    s = f"{x:.2f}".replace(".", ",")
    return s[:-3] + " €" if s.endswith(",00") else s + " €"


def cote_txt(x):
    if x is None:
        return ""
    x = float(x)
    return str(int(x)) if x == int(x) else f"{x:.1f}".replace(".", ",")


def iso(d):
    return str(d)[:10]


# ---------------------------------------------------------------------------
# Chargement
# ---------------------------------------------------------------------------

def charger():
    with open(chemin("config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    with open(chemin(cfg["fichiers"]["collecte"]), encoding="utf-8") as f:
        col = json.load(f)
    return cfg, col


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def vide(num, nom=""):
    return {"num": num, "nom": nom, "age": "", "driver": "", "entraineur": "", "musique": "",
            "cote": None, "pct": None, "stats": "", "points": 0, "citations": 0, "premiers": 0,
            "np": False, "top3_specialise": False}


def construire_chevaux(cfg, col):
    presse = col.get("presse") or {}
    bareme = cfg["presse"]["bareme"]
    np_ = set(col.get("non_partants") or [])
    chevaux = {}
    for p in col.get("partants") or []:
        c = vide(p["num"], p.get("nom", ""))
        c.update({"age": p.get("age") or "", "driver": p.get("driver") or "", "entraineur": p.get("entraineur") or "",
                  "musique": p.get("musique") or "", "cote": p.get("cote_probable"), "pct": p.get("pct_enjeux"),
                  "stats": p.get("stats") or "", "np": p["num"] in np_})
        chevaux[p["num"]] = c

    journaux = presse.get("journaux") or []
    specialises = set(cfg["presse"]["titres_specialises"])
    synthese = presse.get("synthese")
    if synthese:
        for s in synthese:
            c = chevaux.setdefault(s["num"], vide(s["num"], s.get("nom", "")))
            c["points"], c["citations"], c["premiers"] = s.get("points", 0), s.get("citations", 0), s.get("premiers_choix", 0)
            c["np"] = s["num"] in np_
    else:
        for j in journaux:
            for rang, num in enumerate(j.get("chevaux") or []):
                if rang >= len(bareme):
                    break
                c = chevaux.setdefault(num, vide(num))
                c["points"] += bareme[rang]
                c["citations"] += 1
                if rang == 0:
                    c["premiers"] += 1
                c["np"] = num in np_
    for j in journaux:
        if j.get("nom") in specialises:
            for num in (j.get("chevaux") or [])[:3]:
                if num in chevaux:
                    chevaux[num]["top3_specialise"] = True

    nb_journaux = presse.get("nb_journaux") or len(journaux) or 0
    partants = [c for c in chevaux.values() if not c["np"]]
    expo = float(cfg["valeur"].get("exposant_presse", 1.0))
    total = sum(c["points"] ** expo for c in partants) or 1
    trj = cfg["trj"]["simple"]
    for c in partants:
        c["p_est"] = (c["points"] ** expo) / total
        c["p_marche"] = (c["pct"] / 100.0) if c["pct"] is not None else ((trj / float(c["cote"])) if c["cote"] else None)
    communs = [c for c in partants if c["p_marche"]]
    masse_m, masse_p = sum(c["p_marche"] for c in communs), sum(c["p_est"] for c in communs)
    if communs and masse_p > 0:
        k = masse_m / masse_p
        for c in partants:
            c["p_est"] *= k
    for c in partants:
        c["ratio"] = (c["p_est"] / c["p_marche"]) if (c["p_marche"] and c["p_est"] > 0) else None
        if c["ratio"] is None:
            c["classe"] = "inconnue"
        elif c["ratio"] >= cfg["valeur"]["seuil_valeur"]:
            c["classe"] = "valeur"
        elif c["ratio"] <= cfg["valeur"]["seuil_surcote"]:
            c["classe"] = "surcote"
        else:
            c["classe"] = "juste"
        c["part_citations"] = c["citations"] / nb_journaux if nb_journaux else 0
    for c in chevaux.values():
        if c["np"]:
            c.update({"p_est": None, "p_marche": None, "ratio": None, "classe": "inconnue", "part_citations": 0, "rang": None})
    classement = sorted(partants, key=lambda c: (-c["points"], -c["citations"], c["num"]))
    for i, c in enumerate(classement, 1):
        c["rang"] = i
    return chevaux, classement, nb_journaux


def calculer_confiance(cfg, classement, sources):
    if len(classement) < 2:
        return 1
    c1, c2 = classement[0], classement[1]
    ecart = (c1["points"] - c2["points"]) / c1["points"] if c1["points"] else 0
    niveau = 1
    for n in cfg["confiance"]["niveaux"]:
        if c1["part_citations"] >= n["citations"] and ecart >= n["ecart"]:
            niveau = n["niveau"]
            break
    if sources.get("cotes") != "ok":
        niveau -= cfg["confiance"]["malus_sans_cotes"]
    return max(1, min(5, niveau))


def ticket_presse(classement):
    return [dict(c, role="presse") for c in classement[:5]]


def ticket_valeur(cfg, classement):
    tv = cfg["ticket_valeur"]
    pris, notes = [], []
    bases = [c for c in classement if c["part_citations"] >= tv["part_citations_base"]][:3]
    if len(bases) < 3:
        notes.append("La presse est partagée aujourd'hui : moins de trois chevaux font l'unanimité, complété avec la suite de la synthèse.")
        for c in classement:
            if len(bases) >= 3:
                break
            if c not in bases:
                bases.append(c)
    pris += [dict(c, role="base") for c in bases]
    pris_nums = {c["num"] for c in pris}

    candidats = [c for c in classement if tv["rang_valeur_min"] <= c["rang"] <= tv["rang_valeur_max"]
                 and c["num"] not in pris_nums and c["ratio"] is not None]
    if candidats:
        v = max(candidats, key=lambda c: c["ratio"])
    else:
        v = next(c for c in classement if c["num"] not in pris_nums)
        notes.append("Pas de cotes ce matin pour désigner le cheval valeur, pris le suivant de la synthèse.")
    pris.append(dict(v, role="valeur"))
    pris_nums.add(v["num"])

    outs = [c for c in classement if c["num"] not in pris_nums
            and c["part_citations"] <= tv["part_citations_outsider_max"]
            and c["cote"] and tv["cote_outsider_min"] <= float(c["cote"]) <= tv["cote_outsider_max"]]
    if outs:
        outs.sort(key=lambda c: (not c["top3_specialise"], -(c["ratio"] or 0)))
        pris.append(dict(outs[0], role="outsider"))
        if not outs[0]["top3_specialise"]:
            notes.append("Aucun outsider n'est défendu par un titre spécialisé aujourd'hui, pris le moins joué par le public dans la fourchette de cotes.")
    else:
        suivant = next(c for c in classement if c["num"] not in pris_nums)
        pris.append(dict(suivant, role="outsider"))
        notes.append("Aucun outsider dans la fourchette de cotes, ou pas de cotes ce matin : le ticket valeur rejoint la synthèse.")
    return pris, notes


def choisir_place(cfg, classement):
    cands = [c for c in classement[:cfg["place"]["rangs_consideres"]]
             if c["cote"] and float(c["cote"]) >= cfg["place"]["cote_min"] and c["ratio"] is not None]
    return max(cands, key=lambda c: c["ratio"]) if cands else None


def pourquoi(nb_journaux, valeur, place):
    lignes = []
    bases = [c for c in valeur if c["role"] == "base"]
    if bases:
        b = bases[0]
        lignes.append(f"Les bases sont retenues par {min(c['citations'] for c in bases)} journaux sur {nb_journaux} au moins, "
                      f"et le {b['num']} est le premier choix de {b['premiers']} d'entre eux.")
    v = next((c for c in valeur if c["role"] == "valeur"), None)
    o = next((c for c in valeur if c["role"] == "outsider"), None)
    m = []
    if v and v["classe"] == "valeur":
        m.append(f"le {v['num']} est moins joué par le public que ne le dit la presse")
    elif v:
        m.append(f"le {v['num']} est le mieux placé de la suite de la synthèse")
    if o and o["top3_specialise"]:
        m.append(f"le {o['num']} est dans les trois premiers d'un titre spécialisé, rarement cité ailleurs")
    elif o:
        m.append(f"le {o['num']} est l'outsider à la cote la plus cohérente")
    if m:
        lignes.append(m[0][0].upper() + m[0][1:] + (", " + m[1] if len(m) > 1 else "") + ".")
    if place:
        lignes.append(f"En placé, le {place['num']} offre le meilleur rapport entre ses chances et sa cote ({cote_txt(place['cote'])}).")
    return lignes


# ---------------------------------------------------------------------------
# Journal Excel
# ---------------------------------------------------------------------------

ENTETES = {
    "Courses": ["Date", "Hippodrome", "Prix", "RC", "Heure", "Partants", "Journaux", "Confiance", "Statut",
                "Sources", "Arrivée", "Bilan presse", "Rapports"],
    "Chevaux": ["Date", "Num", "Nom", "Rang synthèse", "Points", "Citations", "Premiers choix",
                "Cote probable", "% enjeux", "Chance estimée", "Ratio valeur", "Classe", "Rôle", "Place à l'arrivée",
                "Driver", "Entraîneur", "Musique"],
    "Tickets": ["Date", "Type", "Chevaux", "Formule", "Mise", "Joué", "Trouvés", "Gagné", "Gain", "Résultat connu"],
    "Journaux": ["Date", "Journal", "Ses 8 chevaux", "Trouvés à l'arrivée"],
}


def entetes(ws, cols):
    for i, h in enumerate(cols, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(name="Arial", bold=True)
        c.fill = PatternFill("solid", fgColor="DDE7F0")
        c.alignment = Alignment(wrap_text=True)
        ws.column_dimensions[c.column_letter].width = 16
    ws.freeze_panes = "A2"


def creer_journal(p):
    wb = Workbook()
    ws = wb.active
    ws.title = "Lisez-moi"
    lignes = [
        "Journal du Ticket. Une ligne par Quinté+ et par jour, que mon père joue ou non.",
        "",
        "Courses : la course du jour, la confiance, le statut de la page, l'arrivée et le bilan presse (remplis le lendemain).",
        "Chevaux : tous les partants, avec points, citations, cotes du matin, classe de valeur, et leur place à l'arrivée le lendemain.",
        "Tickets : les tickets proposés chaque jour. Colonne Joué : mets 'oui' quand mon père a réellement joué ce ticket. C'est la seule colonne à remplir à la main.",
        "Journaux : les 8 chevaux de chaque journal, et combien étaient à l'arrivée. Sert au classement des journaux les plus justes.",
        "KPI : calculé par formules. Papier = tous les tickets proposés. Réel = ceux marqués joués.",
        "Backtest : réservé aux vérifications sur l'historique.",
        "",
        "Les chances estimées et les ratios de valeur sont une hypothèse de méthode, pas une mesure. Ils se recalent quand le journal dépasse une centaine de Quintés.",
    ]
    for i, t in enumerate(lignes, 1):
        ws.cell(row=i, column=1, value=t).font = Font(name="Arial")
    ws.column_dimensions["A"].width = 120
    for nom in ["Courses", "Chevaux", "Tickets", "Journaux"]:
        entetes(wb.create_sheet(nom), ENTETES[nom])

    k = wb.create_sheet("KPI")
    entetes(k, ["Type", "Périmètre", "Tickets", "Mises", "Gains", "ROI", "Réussite", "Note"])
    r = 2
    tj = "Tickets!"
    for t in TYPES:
        for perim, crit in [("Papier", None), ("Réel", "oui")]:
            k.cell(row=r, column=1, value=t)
            k.cell(row=r, column=2, value=perim)
            extra = f',{tj}$F:$F,"oui"' if crit else ""
            k.cell(row=r, column=3, value=f'=COUNTIFS({tj}$B:$B,A{r},{tj}$J:$J,"oui"{extra})')
            k.cell(row=r, column=4, value=f'=SUMIFS({tj}$E:$E,{tj}$B:$B,A{r},{tj}$J:$J,"oui"{extra})')
            k.cell(row=r, column=5, value=f'=SUMIFS({tj}$I:$I,{tj}$B:$B,A{r},{tj}$J:$J,"oui"{extra})')
            k.cell(row=r, column=6, value=f'=IF(D{r}=0,"",E{r}/D{r}-1)')
            k.cell(row=r, column=7, value=f'=IF(C{r}=0,"",COUNTIFS({tj}$B:$B,A{r},{tj}$J:$J,"oui",{tj}$H:$H,"oui"{extra})/C{r})')
            k.cell(row=r, column=8, value=f'=IF(C{r}<20,"échantillon trop petit","")')
            k.cell(row=r, column=6).number_format = "0.0%"
            k.cell(row=r, column=7).number_format = "0.0%"
            for col in range(1, 9):
                k.cell(row=r, column=col).font = Font(name="Arial")
            r += 1
    k.cell(row=r + 1, column=1, value="ROI = gains / mises - 1. Les tickets dont le résultat n'est pas encore connu sont ignorés.").font = Font(name="Arial", italic=True)
    entetes(wb.create_sheet("Backtest"), ["Date", "Test", "Règle figée", "Résultat", "Commentaire"])
    wb.save(p)


def lignes_sheet(ws):
    return [[c.value for c in row] for row in ws.iter_rows(min_row=2) if any(c.value is not None for c in row)]


def reecrire_sheet(ws, lignes):
    ws.delete_rows(2, ws.max_row)
    for i, l in enumerate(lignes, 2):
        for j, v in enumerate(l, 1):
            ws.cell(row=i, column=j, value=v).font = Font(name="Arial")


def mettre_a_jour_journal(cfg, col, chevaux, classement, nb_journaux, confiance, statut, tickets):
    p = chemin(cfg["fichiers"]["journal"])
    if not os.path.exists(p):
        creer_journal(p)
    wb = load_workbook(p)
    for nom in ENTETES:
        if nom not in wb.sheetnames:
            entetes(wb.create_sheet(nom), ENTETES[nom])
    date = col["date"]
    course = col.get("course") or {}
    sources = col.get("sources") or {}
    hier = col.get("hier") or {}
    arr5 = [int(n) for n in (hier.get("arrivee") or [])[:5]] if hier.get("date") else []

    # Courses
    ws = wb["Courses"]
    lignes = [l for l in lignes_sheet(ws) if iso(l[0]) != date]
    lignes.append([date, course.get("hippodrome"), course.get("prix"), course.get("rc"), course.get("heure"),
                   course.get("partants"), nb_journaux, confiance, statut,
                   ", ".join(f"{k}: {v}" for k, v in sources.items()), None, None, None])
    if arr5:
        for l in lignes:
            while len(l) < 13:
                l.append(None)
            if iso(l[0]) == hier["date"]:
                l[10], l[11] = "-".join(str(n) for n in arr5), hier.get("presse_bilan")
                l[12] = json.dumps(hier.get("rapports") or {}, ensure_ascii=False)
    lignes.sort(key=lambda l: iso(l[0]))
    reecrire_sheet(ws, lignes)
    lignes_courses = lignes

    # Chevaux
    ws = wb["Chevaux"]
    lignes = [l for l in lignes_sheet(ws) if iso(l[0]) != date]
    roles = {}
    for c in tickets.get("valeur") or []:
        roles[c["num"]] = c["role"]
    if tickets.get("place"):
        roles.setdefault(tickets["place"]["num"], "placé")
    for c in sorted(chevaux.values(), key=lambda c: (c["rang"] or 99, c["num"])):
        lignes.append([date, c["num"], c["nom"], c["rang"], c["points"], c["citations"], c["premiers"],
                       c["cote"], c["pct"], round(c["p_est"], 4) if c["p_est"] is not None else None,
                       round(c["ratio"], 3) if c["ratio"] is not None else None, c["classe"],
                       roles.get(c["num"]), None, c["driver"], c["entraineur"], c["musique"]])
    if arr5:
        pos = {n: i + 1 for i, n in enumerate(arr5)}
        for l in lignes:
            if iso(l[0]) == hier["date"]:
                l[13] = pos.get(l[1])
    lignes.sort(key=lambda l: (iso(l[0]), l[3] or 99))
    reecrire_sheet(ws, lignes)

    # Journaux
    ws = wb["Journaux"]
    lignes = [l for l in lignes_sheet(ws) if iso(l[0]) != date]
    for j in (col.get("presse") or {}).get("journaux") or []:
        lignes.append([date, j.get("nom"), "-".join(str(n) for n in j.get("chevaux") or []), None])
    if arr5:
        for l in lignes:
            if iso(l[0]) == hier["date"] and l[2]:
                nums = {int(x) for x in str(l[2]).split("-") if x.strip()}
                l[3] = len(nums & set(arr5))
    lignes.sort(key=lambda l: (iso(l[0]), str(l[1])))
    reecrire_sheet(ws, lignes)
    lignes_journaux = lignes

    # Tickets : on garde les lignes du jour marquées jouées, on régénère le reste
    ws = wb["Tickets"]
    lignes = [l for l in lignes_sheet(ws) if not (iso(l[0]) == date and str(l[5] or "").lower() != "oui")]
    deja = {(iso(l[0]), l[1]) for l in lignes}
    mq, mp = cfg["budget"]["mise_quinte"], cfg["budget"]["mise_place"]
    if tickets.get("presse") and (date, "quinte_presse") not in deja:
        lignes.append([date, "quinte_presse", "-".join(str(c["num"]) for c in tickets["presse"]), "5 chevaux désordre", mq, "non", None, None, None, "non"])
    if tickets.get("valeur") and (date, "quinte_valeur") not in deja:
        lignes.append([date, "quinte_valeur", "-".join(str(c["num"]) for c in tickets["valeur"]), "5 chevaux désordre", mq, "non", None, None, None, "non"])
    if tickets.get("place") and (date, "place") not in deja:
        lignes.append([date, "place", str(tickets["place"]["num"]), "Simple Placé", mp, "non", None, None, None, "non"])
    if arr5:
        rap = hier.get("rapports") or {}
        bq, bp = cfg["rapports"]["base_quinte"], cfg["rapports"]["base_place"]
        for l in lignes:
            if iso(l[0]) != hier["date"]:
                continue
            nums = [int(x) for x in str(l[2]).split("-") if x.strip()]
            mise = float(l[4] or 0)
            if l[1] in ("quinte_presse", "quinte_valeur"):
                trouves = len(set(nums) & set(arr5))
                gagne = trouves == 5
                gain = None
                if gagne and rap.get("quinte_desordre") is not None:
                    gain = rap["quinte_desordre"] * mise / bq
                elif trouves == 4 and rap.get("bonus4") is not None:
                    gain = rap["bonus4"] * mise / bq
                elif trouves == 3 and rap.get("bonus3") is not None:
                    gain = rap["bonus3"] * mise / bq
                elif not gagne and (rap.get("quinte_desordre") is not None or rap.get("bonus3") is not None):
                    gain = 0
                l[6], l[7] = trouves, "oui" if gagne else "non"
            else:
                gagne = nums[0] in arr5[:3]
                rp = (rap.get("simple_place") or {}).get(str(nums[0]))
                gain = rp * mise / bp if (gagne and rp is not None) else (0 if (not gagne and rap.get("simple_place") is not None) else None)
                l[6], l[7] = (1 if gagne else 0), "oui" if gagne else "non"
            l[8] = round(gain, 2) if gain is not None else None
            l[9] = "oui" if gain is not None else "partiel"
    lignes.sort(key=lambda l: (iso(l[0]), l[1]))
    reecrire_sheet(ws, lignes)
    wb.save(p)
    return lignes, lignes_journaux, lignes_courses


# ---------------------------------------------------------------------------
# Budget, bilans, fiabilité
# ---------------------------------------------------------------------------

def budget_semaine(cfg, tickets_l, date):
    d = dt.date.fromisoformat(date)
    lundi = d - dt.timedelta(days=d.weekday())
    dimanche = lundi + dt.timedelta(days=6)
    utilise = 0.0
    for l in tickets_l:
        try:
            ld = dt.date.fromisoformat(iso(l[0]))
        except ValueError:
            continue
        if lundi <= ld <= dimanche and str(l[5] or "").lower() == "oui":
            utilise += float(l[4] or 0)
    semaine = cfg["budget"]["semaine_euros"]
    return {"semaine": semaine, "utilise": utilise, "restant": max(0.0, semaine - utilise)}


def joues_connus(tickets_l):
    return [l for l in tickets_l if str(l[5] or "").lower() == "oui" and str(l[9]) == "oui"]


def bilans(tickets_l, date):
    d = dt.date.fromisoformat(date)
    joues = joues_connus(tickets_l)
    mois = None
    jm = [l for l in joues if iso(l[0]).startswith(str(d)[:7])]
    if jm:
        mois = f"{MOIS[d.month - 1].capitalize()} : {len(jm)} tickets joués, {euros(sum(float(l[4]) for l in jm))} misés, {euros(sum(float(l[8] or 0) for l in jm))} récupérés."
    serie = None
    gagnants = [l for l in joues if float(l[8] or 0) > 0]
    if gagnants:
        g = max(gagnants, key=lambda l: iso(l[0]))
        serie = f"Dernier ticket gagnant : {date_fr(dt.date.fromisoformat(iso(g[0])))}, {TYPES_TXT[g[1]]}, {euros(float(g[8]))}."
    elif joues:
        serie = "Pas encore de ticket gagnant parmi ceux joués."
    papier = [l for l in tickets_l if str(l[9]) == "oui" and l[1] == "quinte_valeur"]
    methode = None
    if papier:
        n, mises, gains = len(papier), sum(float(l[4]) for l in papier), sum(float(l[8] or 0) for l in papier)
        methode = f"L'Audacieux, joué tous les jours sur le papier depuis le début : {n} Quinté{'s' if n > 1 else ''}, {euros(mises)} misés, {euros(gains)} récupérés."
        if n < 20:
            methode += " Trop peu de courses pour conclure."
    return mois, serie, methode


def semaines(tickets_l, date):
    d = dt.date.fromisoformat(date)
    lundi = d - dt.timedelta(days=d.weekday())
    out = []
    for i in range(5, -1, -1):
        deb = lundi - dt.timedelta(days=7 * i)
        fin = deb + dt.timedelta(days=6)
        solde, n = 0.0, 0
        for l in joues_connus(tickets_l):
            ld = dt.date.fromisoformat(iso(l[0]))
            if deb <= ld <= fin:
                solde += float(l[8] or 0) - float(l[4] or 0)
                n += 1
        out.append({"label": f"S{deb.isocalendar()[1]}", "solde": round(solde, 2), "tickets": n})
    return out


def fiabilite_journaux(journaux_l, date, jours=30, mini=10):
    d = dt.date.fromisoformat(date)
    deb = d - dt.timedelta(days=jours)
    acc = {}
    for l in journaux_l:
        if l[3] is None:
            continue
        ld = dt.date.fromisoformat(iso(l[0]))
        if deb <= ld < d:
            acc.setdefault(l[1], []).append(float(l[3]))
    out = [{"nom": j, "trouves_30j": round(sum(v) / len(v), 1), "n": len(v)} for j, v in acc.items() if len(v) >= mini]
    out.sort(key=lambda x: -x["trouves_30j"])
    return out[:8]


def resultats_recents(courses_l, date, jours=14):
    d = dt.date.fromisoformat(date)
    out = []
    for l in courses_l:
        try:
            ld = dt.date.fromisoformat(iso(l[0]))
        except ValueError:
            continue
        if not l[10] or not (d - dt.timedelta(days=jours) <= ld < d):
            continue
        try:
            rap = json.loads(l[12]) if len(l) > 12 and l[12] else {}
        except (ValueError, TypeError):
            rap = {}
        out.append({"date": iso(l[0]), "hippodrome": l[1], "arrivee": [int(x) for x in str(l[10]).split("-") if x.strip()], "rapports": rap})
    return out


def texte_hier(col, tickets_l):
    hier = col.get("hier") or {}
    if not hier.get("date") or not hier.get("arrivee"):
        return None
    d = dt.date.fromisoformat(hier["date"])
    arr = "-".join(str(n) for n in hier["arrivee"][:5])
    base = date_fr(d)
    txt = base[0].upper() + base[1:] + (f" à {hier['hippodrome']}" if hier.get("hippodrome") else "") + f", arrivée {arr}."
    par_type = {}
    for l in tickets_l:
        if iso(l[0]) != hier["date"] or l[6] is None:
            continue
        if l[1] == "quinte_presse":
            par_type[l[1]] = f"Le Sage avait {l[6]} chevaux sur 5" + (f" ({euros(float(l[8]))} récupérés)" if l[8] else "")
        elif l[1] == "quinte_valeur":
            par_type[l[1]] = f"L'Audacieux {l[6]}" + (f" ({euros(float(l[8]))})" if l[8] else "")
        elif l[1] == "place":
            par_type[l[1]] = ("le placé est rentré" + (f" ({euros(float(l[8]))})" if l[8] else "")) if l[7] == "oui" else "le placé n'est pas rentré"
    morceaux = [par_type[t] for t in TYPES if t in par_type]
    if morceaux:
        txt += " " + ", ".join(morceaux) + "."
    if hier.get("presse_bilan"):
        pb = hier["presse_bilan"].strip()
        txt += " " + pb[0].upper() + pb[1:] + ("" if pb.endswith(".") else ".")
    return txt


# ---------------------------------------------------------------------------
# Page et message
# ---------------------------------------------------------------------------

def donnees_page(cfg, col, chevaux, nb_journaux, confiance, statut, raison, tickets, lignes_pourquoi, notes,
                 tickets_l, journaux_l, courses_l):
    date = col["date"]
    d = dt.date.fromisoformat(date)
    course = col.get("course") or {}
    methode = bilans(tickets_l, date)[2]
    partants = []
    for c in sorted(chevaux.values(), key=lambda c: c["num"]):
        partants.append({
            "num": c["num"], "nom": c["nom"], "age": c["age"], "driver": c["driver"], "entraineur": c["entraineur"],
            "musique": c["musique"], "cote": c["cote"], "pct": c["pct"], "points": c["points"], "citations": c["citations"],
            "premiers": c["premiers"], "stats": c["stats"],
            "pest": round(c["p_est"], 4) if c.get("p_est") is not None else None,
            "pm": round(c["p_marche"], 4) if c.get("p_marche") is not None else None,
            "ratio": round(c["ratio"], 3) if c.get("ratio") is not None else None,
            "classe": c.get("classe", "inconnue"),
        })
    t = {"mise_quinte": cfg["budget"]["mise_quinte"], "mise_place": cfg["budget"]["mise_place"],
         "presse": [c["num"] for c in tickets.get("presse") or []],
         "valeur": [{"num": c["num"], "role": c["role"]} for c in tickets.get("valeur") or []],
         "place": ({"num": tickets["place"]["num"], "cote": tickets["place"]["cote"]} if tickets.get("place") else None)}
    sources = col.get("sources") or {}
    manquantes = [k for k, v in sources.items() if v != "ok" and k != "hier"]
    if manquantes and not raison:
        noms = {"presse": "les pronostics de la presse", "cotes": "les cotes du matin", "stats": "les statistiques"}
        raison = "Il manque ce matin " + ", ".join(noms.get(m, m) for m in manquantes) + ". La page fait avec ce qu'elle a."
    th = texte_hier(col, tickets_l)
    return {
        "exemple": False, "date": date, "date_texte": date_fr(d), "genere_le": col.get("genere_le", ""),
        "statut": statut, "raison_statut": raison or "", "confiance": confiance,
        "course": {"hippodrome": course.get("hippodrome", ""), "prix": course.get("prix", ""), "rc": course.get("rc", ""),
                   "heure": course.get("heure", ""), "detail": (course.get("detail") or "").rstrip("."),
                   "grande_course": bool(course.get("grande_course"))},
        "non_partants": col.get("non_partants") or [], "nb_journaux": nb_journaux,
        "partants": partants,
        "journaux": [{"nom": j.get("nom"), "chevaux": j.get("chevaux") or []} for j in (col.get("presse") or {}).get("journaux") or []],
        "fiabilite_journaux": fiabilite_journaux(journaux_l, date),
        "tickets": t, "pourquoi": lignes_pourquoi + notes,
        "budget": {"semaine": cfg["budget"]["semaine_euros"]},
        "hier": ({"texte": th} if th else None),
        "resultats": resultats_recents(courses_l, date),
        "suivi": {"methode": methode},
        "params": {"expo": cfg["valeur"].get("exposant_presse", 1.0), "trj": cfg["trj"]["simple"],
                   "seuil_valeur": cfg["valeur"]["seuil_valeur"], "seuil_surcote": cfg["valeur"]["seuil_surcote"]},
    }


def rendre_page(cfg, data):
    with open(chemin(cfg["fichiers"]["gabarit"]), encoding="utf-8") as f:
        gab = f.read()
    bloc = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
    new, n = re.subn(r'(<script type="application/json" id="data">)[\s\S]*?(</script>)',
                     lambda m: m.group(1) + "\n" + bloc + "\n" + m.group(2), gab, count=1)
    if n != 1:
        raise RuntimeError("Bloc de données introuvable dans le gabarit")
    return new


def rendre_message(col, statut, confiance, tickets, lignes_pourquoi, cfg):
    d = dt.date.fromisoformat(col["date"])
    course = col.get("course") or {}
    sujet = {"jouer": "Bon jour pour jouer", "pas_de_jeu": "Pas de jeu conseillé", "budget": "Budget atteint",
             "source_manquante": "Source presse indisponible"}[statut]
    lignes = [f"{sujet} : {date_fr(d)}", "",
              f"Quinté+ à {course.get('hippodrome', '?')}, {course.get('prix', '')} ({course.get('rc', '')}), départ {course.get('heure', '')}.",
              f"Confiance {confiance}/5."]
    if tickets.get("presse"):
        mq, mp = cfg["budget"]["mise_quinte"], cfg["budget"]["mise_place"]
        lignes += ["", f"Le Sage : {'-'.join(str(c['num']) for c in tickets['presse'])}, 5 chevaux désordre, {euros(mq)}",
                   f"L'Audacieux : {'-'.join(str(c['num']) for c in tickets['valeur'])}, 5 chevaux désordre, {euros(mq)}"]
        if tickets.get("place"):
            lignes.append(f"Simple Placé : {tickets['place']['num']} {tickets['place']['nom']}, {euros(mp)}")
        lignes += [""] + lignes_pourquoi
    lignes += ["", "La page complète est dans ticket.html."]
    return "\n".join(lignes)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg, col = charger()
    date = col.get("date")
    if not date:
        print("collecte.json : champ date absent", file=sys.stderr)
        sys.exit(1)
    sources = col.get("sources") or {}
    chevaux, classement, nb_journaux = construire_chevaux(cfg, col)

    tickets, notes, lignes_pourquoi, confiance, raison = {}, [], [], 1, ""
    if not classement or sources.get("presse") != "ok":
        statut = "source_manquante"
        raison = "Les pronostics de la presse n'ont pas pu être relevés ce matin. Pas de ticket plutôt qu'un ticket inventé."
    else:
        confiance = calculer_confiance(cfg, classement, sources)
        tickets["presse"] = ticket_presse(classement)
        tickets["valeur"], notes = ticket_valeur(cfg, classement)
        tickets["place"] = choisir_place(cfg, classement)
        lignes_pourquoi = pourquoi(nb_journaux, tickets["valeur"], tickets["place"])
        statut = "jouer" if confiance >= cfg["confiance"]["seuil_jouer"] else "pas_de_jeu"
        if statut == "pas_de_jeu":
            raison = "La presse est trop partagée sur la course du jour, ou il manque trop de données pour trancher."

    tickets_l, journaux_l, courses_l = mettre_a_jour_journal(cfg, col, chevaux, classement, nb_journaux, confiance, statut, tickets)
    # Le budget réel est tenu par la page de mon père, dans son navigateur. L'Excel ne sert qu'à la méthode.

    data = donnees_page(cfg, col, chevaux, nb_journaux, confiance, statut, raison, tickets, lignes_pourquoi, notes,
                        tickets_l, journaux_l, courses_l)
    p_page = chemin(cfg["fichiers"]["page"])
    with open(p_page, "w", encoding="utf-8") as f:
        f.write(rendre_page(cfg, data))
    with open(chemin(cfg["fichiers"]["message"]), "w", encoding="utf-8") as f:
        f.write(rendre_message(col, statut, confiance, tickets, lignes_pourquoi, cfg))

    od = cfg["fichiers"].get("onedrive")
    if od:
        if os.path.isdir(od):
            shutil.copy2(p_page, os.path.join(od, os.path.basename(p_page)))
            print(f"Copié vers {od}")
        else:
            print(f"Dossier OneDrive introuvable : {od}", file=sys.stderr)

    print(f"Statut {statut}, confiance {confiance}/5")
    if tickets.get("presse"):
        print("Le Sage      :", "-".join(str(c["num"]) for c in tickets["presse"]))
        print("L'Audacieux  :", "-".join(str(c["num"]) for c in tickets["valeur"]))
        print("Simple Placé :", tickets["place"]["num"] if tickets.get("place") else "aucun")


if __name__ == "__main__":
    main()
