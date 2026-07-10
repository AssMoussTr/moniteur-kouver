#!/usr/bin/env python3
"""
moniteur_reservation.py
-----------------------
Monitoring synthétique du parcours de réservation FoodCollect (Kouver).

À chaque exécution, le bot :
  1. ouvre la page de réservation,
  2. choisit un nombre de couverts AU HASARD,
  3. sélectionne la date J+2 et un créneau horaire AU HASARD,
  4. remplit le formulaire avec une identité de test,
  5. valide la réservation,
  6. surveille les réponses réseau (erreur 501/5xx) et l'écran de confirmation.

S'il détecte une erreur au moment de réserver, il envoie un e-mail
d'alerte (avec capture d'écran) à l'équipe.

Conçu pour être lancé UNE FOIS par heure (via le planificateur launchd).
Les réglages e-mail (SMTP) sont fournis par l'environnement (via demarrer.sh).
"""

import os
import re
import sys
import time
import random
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime, date, timedelta
from pathlib import Path

# ===========================================================================
# CONFIGURATION
# ===========================================================================
URL = "https://foodcollect.fr/store/restaurant-kouver/reserver"

# Décalage de la date de réservation (J+2 comme demandé)
JOURS_A_LAVANCE = 2

# Nombre de tentatives complètes avant d'envoyer une alerte.
# Évite les fausses alertes dues à un hoquet ponctuel (page lente, calendrier
# qui tarde à s'ouvrir...). Un vrai bug du service échouera les 3 fois.
MAX_ESSAIS = 3

# Tablée maximale tirée au sort (les grandes tablées ont souvent 0 dispo,
# ce qui ferait croire à tort qu'il n'y a aucun créneau)
MAX_COUVERTS = 4

# Capture d'écran de diagnostic quand aucun créneau n'apparaît
DEBUG_CAPTURE = str(Path(__file__).with_name("derniere_page.png"))

# Alerter aussi quand aucun créneau n'est proposé à J+2 ?
# (souvent = jour de fermeture, donc désactivé par défaut pour éviter les fausses alertes)
ALERTE_SI_AUCUN_CRENEAU = False

# Identité de test injectée dans le formulaire
TEST = {
    "firstname": "TEST",
    "lastname": "BOT",
    "email": "test-bot@kouver.fr",
    "phone": "0664850542",
    "message": "RESERVATION TEST AUTOMATIQUE - NE PAS TRAITER",
}

# Destinataires de l'alerte
EMAIL_TO = "assane@kouver.fr"
EMAIL_CC = ["guillaume@kouver.fr", "jeanalexis@kouver.fr"]
SUJET = "Détection d'erreur - réservation Joie Kouver"

SMTP = {
    "host": os.getenv("SMTP_HOST", ""),
    "port": int(os.getenv("SMTP_PORT", "465")),
    "user": os.getenv("SMTP_USER", ""),
    "pass": os.getenv("SMTP_PASS", ""),
    "from": os.getenv("EMAIL_FROM", os.getenv("SMTP_USER", "")),
}

CAPTURE = str(Path(__file__).with_name("derniere_erreur.png"))


# ===========================================================================
# E-MAIL D'ALERTE
# ===========================================================================
def envoyer_alerte(raison: str, details: str, capture: str | None):
    horodatage = datetime.now().strftime("%d/%m/%Y à %H:%M:%S")
    corps = (
        "Une erreur a été détectée lors de l'essai du bot en faisant une réservation.\n\n"
        f"— Détails —\n"
        f"Quand   : {horodatage}\n"
        f"Nature  : {raison}\n"
        f"{details}\n\n"
        f"Page    : {URL}\n"
        "(capture d'écran de l'erreur en pièce jointe, si disponible)\n"
    )

    if not (SMTP["host"] and SMTP["user"] and SMTP["pass"]):
        print("[!] SMTP non configuré — alerte non envoyée.", file=sys.stderr)
        print(corps)
        return

    outer = MIMEMultipart("mixed")
    outer["Subject"] = SUJET
    outer["From"] = SMTP["from"]
    outer["To"] = EMAIL_TO
    if EMAIL_CC:
        outer["Cc"] = ", ".join(EMAIL_CC)
    outer.attach(MIMEText(corps, "plain", "utf-8"))

    if capture and Path(capture).exists():
        with open(capture, "rb") as f:
            img = MIMEImage(f.read())
            img.add_header("Content-Disposition", "attachment",
                           filename="erreur_reservation.png")
            outer.attach(img)

    rcpts = [EMAIL_TO] + EMAIL_CC
    if int(SMTP["port"]) == 465:
        with smtplib.SMTP_SSL(SMTP["host"], SMTP["port"], timeout=30) as s:
            s.login(SMTP["user"], SMTP["pass"])
            s.send_message(outer, from_addr=SMTP["from"], to_addrs=rcpts)
    else:
        with smtplib.SMTP(SMTP["host"], SMTP["port"], timeout=30) as s:
            s.starttls()
            s.login(SMTP["user"], SMTP["pass"])
            s.send_message(outer, from_addr=SMTP["from"], to_addrs=rcpts)
    cc_txt = f" (cc {', '.join(EMAIL_CC)})" if EMAIL_CC else ""
    print(f"    ✉️  Alerte envoyée à {EMAIL_TO}{cc_txt}")


# ===========================================================================
# PARCOURS DE RÉSERVATION
# ===========================================================================
class AucunCreneau(Exception):
    pass


def tenter_reservation(page) -> dict:
    """Effectue une réservation de test. Renvoie un dict décrivant l'issue."""
    infos = {"couverts": None, "date": None, "creneau": None}

    # 1) Couverts au hasard (petites tablées, là où il y a de la dispo)
    page.wait_for_selector("#add_resa_couverts", timeout=20000)
    valeurs = page.locator("#add_resa_couverts option").evaluate_all(
        "els => els.map(e => e.value).filter(v => v && /^[0-9]+$/.test(v))")
    valeurs = [v for v in valeurs if v.isdigit() and int(v) <= MAX_COUVERTS]
    if not valeurs:
        valeurs = [str(n) for n in range(1, MAX_COUVERTS + 1)]
    couverts = random.choice(valeurs)
    page.locator("#add_resa_couverts").select_option(couverts)
    infos["couverts"] = couverts
    print(f"    Couverts choisis : {couverts}")

    # 2) Date = J+2 (calendrier parfois capricieux en mode invisible :
    #    on réessaie d'ouvrir le calendrier jusqu'à voir le jour cible)
    cible = date.today() + timedelta(days=JOURS_A_LAVANCE)
    infos["date"] = cible.strftime("%d/%m/%Y")
    champ_date = page.get_by_role("textbox", name="Sélectionnez une date")
    lien_jour = page.get_by_role("link", name=str(cible.day), exact=True).first
    ouvert = False
    for _ in range(4):
        champ_date.scroll_into_view_if_needed()
        champ_date.click()
        try:
            lien_jour.wait_for(state="visible", timeout=5000)
            ouvert = True
            break
        except Exception:
            page.wait_for_timeout(600)
    if not ouvert:
        raise RuntimeError("le calendrier de sélection de date ne s'est pas ouvert")
    lien_jour.scroll_into_view_if_needed()
    lien_jour.click()
    page.wait_for_timeout(1500)  # laisser les créneaux se charger

    # 3) Créneau au hasard PARMI CEUX VISIBLES.
    #    La page garde en mémoire tous les horaires de la journée (souvent
    #    cachés) ; seuls les créneaux réellement disponibles sont visibles.
    #    On filtre donc sur ":visible" pour ne cliquer qu'un créneau affiché.
    slots = None
    fin = time.time() + 20
    while time.time() < fin:
        loc = page.locator("[id^='slot-']:visible")
        if loc.count() == 0:
            loc = page.locator(".time-slot-button:visible")
        if loc.count() == 0:
            loc = page.get_by_text(re.compile(r'^\s*\d{1,2}\s*:\s*\d{2}\s*$'))
        if loc.count() > 0:
            slots = loc
            break
        page.wait_for_timeout(500)
    if slots is None:
        raise AucunCreneau(f"aucun créneau proposé à J+2 ({infos['date']})")

    n = slots.count()
    choisi = slots.nth(random.randrange(n))
    infos["creneau"] = (choisi.get_attribute("id") or choisi.inner_text() or "").strip()
    print(f"    Créneau choisi   : {infos['creneau']} (parmi {n} visibles)")
    choisi.click()

    # 4) Passage au formulaire
    page.get_by_role("button", name="Réserver", exact=True).click()

    # 5) Remplissage du formulaire
    page.locator("#add_resa_firstname").fill(TEST["firstname"])
    page.locator("#add_resa_lastname").fill(TEST["lastname"])
    page.locator("#add_resa_email").fill(TEST["email"])
    page.locator("#add_resa_phone").fill(TEST["phone"])
    try:
        page.get_by_role("textbox", name="Allergies, commentaires...").fill(TEST["message"])
    except Exception:
        pass  # champ commentaire facultatif

    # Case OBLIGATOIRE "Ma réservation sera confirmée par email."
    # (c'est elle qui active le bouton Réserver final)
    try:
        page.get_by_role("checkbox", name=re.compile("confirmée", re.I)).check()
    except Exception:
        page.get_by_text("Ma réservation sera confirmée").click()

    # Conditions générales
    page.get_by_role("checkbox", name="J’accepte les conditions géné").check()

    # 6) Validation finale
    page.get_by_role("button", name="Réserver", exact=True).click()

    # 7) Attente de l'écran de confirmation
    page.get_by_role("heading", name="Votre réservation a été envoy").wait_for(
        state="visible", timeout=20000)
    infos["confirme"] = True
    return infos


def run():
    from playwright.sync_api import sync_playwright

    horodatage = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{horodatage}] Test de réservation…")

    headless = os.getenv("HEADFUL") is None
    raison = None
    details = ""
    capture = None
    succes = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        for essai in range(1, MAX_ESSAIS + 1):
            erreurs_5xx = []      # réponses serveur >= 500 (ex. le fameux 501)
            context = browser.new_context(locale="fr-FR",
                                          viewport={"width": 1366, "height": 900})
            page = context.new_page()
            page.set_default_timeout(20000)
            page.on("response", lambda r: erreurs_5xx.append(f"{r.status} {r.url}")
                    if r.status >= 500 else None)

            try:
                page.goto(URL, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)     # laisser le JS de la page s'initialiser
                infos = tenter_reservation(page)
                details = (f"Couverts : {infos.get('couverts')} | Date : {infos.get('date')} "
                           f"| Créneau : {infos.get('creneau')}")

                if erreurs_5xx:
                    # Vraie erreur serveur (le bug recherché) : on alerte sans retenter.
                    raison = "Erreur serveur (5xx/501) pendant la réservation"
                    details += "\nRéponses serveur ≥500 : " + " ; ".join(erreurs_5xx[:5])
                    try:
                        page.screenshot(path=CAPTURE, full_page=True)
                        capture = CAPTURE
                    except Exception:
                        pass
                    context.close()
                    break

                succes = True
                print(f"    ✅ Réservation confirmée ({details}) — aucun problème.")
                context.close()
                break

            except AucunCreneau as e:
                # Pas un vrai bug (souvent jour de fermeture) : on n'alerte pas.
                print(f"    ⚠️  {e}")
                try:
                    page.screenshot(path=DEBUG_CAPTURE, full_page=True)
                except Exception:
                    pass
                if ALERTE_SI_AUCUN_CRENEAU:
                    raison = "Aucun créneau disponible à J+2"
                    details = str(e)
                context.close()
                break

            except Exception as e:
                # Le parcours n'a pas pu aller au bout. Cela peut être un hoquet
                # ponctuel -> on RETENTE. On n'alerte que si toutes les tentatives échouent.
                msg = f"{type(e).__name__} — {e}"
                print(f"    ⚠️  Tentative {essai}/{MAX_ESSAIS} échouée : {msg}")
                if erreurs_5xx:
                    raison = "Erreur serveur (5xx/501) pendant la réservation"
                else:
                    raison = "Le bot n'a pas pu terminer la réservation (à vérifier)"
                details = f"(après {essai} tentative(s)) {msg}"
                try:
                    page.screenshot(path=CAPTURE, full_page=True)
                    capture = CAPTURE
                except Exception:
                    pass
                context.close()
                if essai < MAX_ESSAIS:
                    raison = None      # on efface : on va retenter
                    p_wait = 3
                    print(f"    ⏳ Nouvelle tentative dans {p_wait}s…")
                    time.sleep(p_wait)
                    continue
                break

        browser.close()

    if succes:
        raison = None

    if raison:
        print(f"    ❌ {raison}")
        envoyer_alerte(raison, details, capture)


if __name__ == "__main__":
    run()
