#!/usr/bin/env python3
"""
Automatisation Appium - UpJunoo Driver

Flow:
1) Ouvre l'app chauffeur (com.upjunoo.driver)
2) Login partenaire (email + mot de passe)
3) Va dans l'onglet "Comptes"
4) Ouvre "Portefeuille"
5) Ouvre la modal "Transférer"
6) Saisit montant = 500
7) Saisit téléphone = 0102030405
"""

import os
import time
from typing import Iterable, Tuple

from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


APPIUM_URL = os.getenv("APPIUM_URL", "http://127.0.0.1:4723")
DEVICE_UDID = os.getenv("ANDROID_UDID", "").strip()
APP_PACKAGE = os.getenv("UPJUNOO_PACKAGE", "com.upjunoo.driver")
APP_ACTIVITY = os.getenv("UPJUNOO_ACTIVITY", ".MainActivity")

TRANSFER_AMOUNT = os.getenv("TRANSFER_AMOUNT", "500")
TRANSFER_PHONE = os.getenv("TRANSFER_PHONE", "0102030405")
PARTNER_EMAIL = os.getenv("PARTNER_EMAIL", "partenaire5@upjunoo.com")
PARTNER_PASSWORD = os.getenv("PARTNER_PASSWORD", "123456789@")
PARTNER_EMAIL_2 = os.getenv("PARTNER_EMAIL_2", "partenaire8@upjunoo.com")


Locator = Tuple[str, str]


def wait_click(driver, wait: WebDriverWait, locators: Iterable[Locator], label: str):
    last_err = None
    for by, value in locators:
        try:
            el = wait.until(EC.element_to_be_clickable((by, value)))
            el.click()
            print(f"✅ Click: {label} [{by}={value}]")
            return el
        except Exception as e:
            last_err = e
    raise TimeoutException(f"Element non cliquable: {label} | err={last_err}")


def click_portefeuille(driver, wait: WebDriverWait):
    # Essai direct (texte/accessibility)
    try:
        return wait_click(
            driver,
            wait,
            [
                (AppiumBy.ACCESSIBILITY_ID, "Portefeuille"),
                (By.XPATH, "//*[@text='Portefeuille']"),
                (By.XPATH, "//*[contains(@text,'Portefeuille')]"),
            ],
            "Menu Portefeuille",
        )
    except Exception:
        pass

    # Essai avec UiScrollable (utile si la liste est scrollable)
    try:
        el = driver.find_element(
            AppiumBy.ANDROID_UIAUTOMATOR,
            'new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView(new UiSelector().textContains("Portefeuille"))',
        )
        el.click()
        print("✅ Click: Menu Portefeuille [UiScrollable]")
        return el
    except Exception:
        pass

    # Dernier fallback : chercher tout élément contenant le texte, puis cliquer
    candidates = driver.find_elements(By.XPATH, "//*[contains(@text,'Portefeuille')]")
    for el in candidates:
        if el.is_displayed():
            el.click()
            print("✅ Click: Menu Portefeuille [fallback contains text]")
            return el

    raise TimeoutException("Element non cliquable: Menu Portefeuille")


def click_transfer_button(driver, wait: WebDriverWait):
    # Attendre que l'écran portefeuille soit bien visible
    try:
        wait_visible(
            driver,
            wait,
            [
                (By.XPATH, "//*[@text='Portefeuille']"),
                (By.XPATH, "//*[contains(@text,'Portefeuille')]"),
                (AppiumBy.ACCESSIBILITY_ID, "Portefeuille"),
            ],
            "Ecran Portefeuille",
        )
    except Exception:
        pass

    # Essais directs
    try:
        return wait_click(
            driver,
            wait,
            [
                (AppiumBy.ACCESSIBILITY_ID, "Transférer"),
                (By.XPATH, "//*[@text='Transférer']"),
                (By.XPATH, "//*[contains(@text,'Transférer')]"),
                (By.XPATH, "//*[contains(@content-desc,'Transférer')]"),
            ],
            "Bouton Transférer",
        )
    except Exception:
        pass

    # UiAutomator fallback (tolérant sur accents/variantes)
    for token in ["Transf", "Transfer", "Retirer"]:
        try:
            el = driver.find_element(
                AppiumBy.ANDROID_UIAUTOMATOR,
                f'new UiSelector().textContains("{token}")',
            )
            if el.is_displayed():
                # Si on a "Retirer" par erreur de token, on ne clique pas
                txt = (el.text or "").strip().lower()
                if "retir" in txt:
                    continue
                el.click()
                print(f"✅ Click: Bouton Transférer [UiSelector textContains={token}]")
                return el
        except Exception:
            continue

    # Fallback final: parmi les boutons visibles Ajouter/Retirer/Transférer,
    # le bouton "Transférer" est souvent le plus à droite
    candidates = driver.find_elements(By.CLASS_NAME, "android.widget.Button")
    visible = [e for e in candidates if e.is_displayed()]
    if visible:
        # Tri gauche->droite par position X, puis clic dernier
        visible.sort(key=lambda e: e.location.get("x", 0))
        last_btn = visible[-1]
        last_text = (last_btn.text or "").strip()
        if "annuler" not in last_text.lower():
            last_btn.click()
            print(f"✅ Click: Bouton Transférer [fallback dernier bouton visible: {last_text}]")
            return last_btn

    raise TimeoutException("Element non cliquable: Bouton Transférer")


def wait_transfer_modal_ready(driver, wait: WebDriverWait):
    # L'ouverture de la bottom-sheet peut être animée. On accepte plusieurs signaux.
    time.sleep(0.8)

    modal_locators = [
        (By.XPATH, "//*[@text='Transférer']"),
        (By.XPATH, "//*[contains(@text,'Sélectionnez le type de destinataire')]"),
        (By.XPATH, "//*[@text='Annuler']"),
        (By.XPATH, "//*[contains(@text,'Entrez le montant')]"),
        (By.XPATH, "//*[contains(@text,'numéro de téléphone')]"),
        (By.XPATH, "//*[@text='Conducteur']"),
        (By.XPATH, "//*[@text='Utilisateur']"),
    ]

    for by, value in modal_locators:
        try:
            el = WebDriverWait(driver, 3, poll_frequency=0.25).until(
                EC.visibility_of_element_located((by, value))
            )
            print(f"✅ Modal prête [{by}={value}]")
            return el
        except Exception:
            continue

    # Fallback: au moins 2 champs input visibles + bouton Annuler ou Transférer
    def _modal_signature(d):
        edits = [e for e in d.find_elements(By.CLASS_NAME, "android.widget.EditText") if e.is_displayed()]
        has_action = bool(d.find_elements(By.XPATH, "//*[@text='Annuler']") or d.find_elements(By.XPATH, "//*[@text='Transférer']"))
        return len(edits) >= 2 and has_action

    try:
        wait.until(_modal_signature)
        print("✅ Modal prête [fallback signature inputs+actions]")
        return True
    except Exception:
        # Ne pas échouer dur si le titre/champs sont déjà rendus mais différemment
        page_text = (driver.page_source or "")
        if "Transférer" in page_text or "Utilisateur" in page_text or "Conducteur" in page_text:
            print("⚠️ Modal probablement visible (détection fallback via page_source).")
            return True
        raise TimeoutException("Modal Transférer non détectée après clic sur le bouton.")


def get_modal_anchor(driver):
    """
    Retourne un point d'ancrage de la modal (x_center, y_top du titre 'Transférer').
    """
    titles = [e for e in driver.find_elements(By.XPATH, "//*[@text='Transférer']") if e.is_displayed()]
    if not titles:
        return None
    title = titles[0]
    rect = title.rect
    x_center = int(rect["x"] + rect["width"] * 0.5)
    y_top = int(rect["y"])
    return x_center, y_top


def ensure_conducteur_selected(driver, wait: WebDriverWait):
    # 1) Localiser le champ dropdown (valeur Utilisateur/Conducteur affichée)
    field = None
    for xp in ["//*[@text='Utilisateur']", "//*[@text='Conducteur']"]:
        elems = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
        if elems:
            field = elems[0]
            break
    if field is None:
        # Fallback géométrique dans la modal: tap sur le champ destinataire puis 2e option
        anchor = get_modal_anchor(driver)
        if anchor:
            x_center, y_top = anchor
            # Champ destinataire approximativement sous le texte d'aide
            driver.execute_script("mobile: clickGesture", {"x": x_center, "y": y_top + 145})
            time.sleep(0.25)
            # Option "Conducteur" = ligne inférieure dans la liste déroulante
            driver.execute_script("mobile: clickGesture", {"x": x_center, "y": y_top + 250})
            time.sleep(0.25)
            print("✅ Destinataire réglé sur Conducteur (fallback coordonnées modal)")
            return
        print("⚠️ Champ destinataire non trouvé.")
        return

    # Si déjà conducteur, on garde
    if (field.text or "").strip().lower() == "conducteur":
        print("✅ Destinataire déjà sur Conducteur")
        return

    # 2) Ouvrir le dropdown
    try:
        field.click()
    except Exception:
        rect = field.rect
        driver.execute_script(
            "mobile: clickGesture",
            {"x": int(rect["x"] + rect["width"] * 0.5), "y": int(rect["y"] + rect["height"] * 0.5)},
        )
    time.sleep(0.35)

    # 3) Cliquer l'option Conducteur dans la liste (celle sous le champ)
    conducteur_items = [e for e in driver.find_elements(By.XPATH, "//*[@text='Conducteur']") if e.is_displayed()]
    if conducteur_items:
        fy = field.location.get("y", 0)
        below = [e for e in conducteur_items if e.location.get("y", 0) > fy + 8]
        target = below[0] if below else conducteur_items[-1]
        try:
            target.click()
        except Exception:
            rect = target.rect
            driver.execute_script(
                "mobile: clickGesture",
                {"x": int(rect["x"] + rect["width"] * 0.5), "y": int(rect["y"] + rect["height"] * 0.5)},
            )
        time.sleep(0.25)
    else:
        # 4) Fallback géométrique: 2e ligne du menu (Conducteur sur ta capture)
        rect = field.rect
        x = int(rect["x"] + rect["width"] * 0.5)
        y = int(rect["y"] + rect["height"] * 1.9)
        driver.execute_script("mobile: clickGesture", {"x": x, "y": y})
        time.sleep(0.25)

    # 5) Vérification finale
    selected_vals = [e for e in driver.find_elements(By.XPATH, "//*[@text='Conducteur']") if e.is_displayed()]
    if selected_vals:
        print("✅ Destinataire réglé sur Conducteur")
    else:
        print("⚠️ Impossible de confirmer Conducteur, on continue.")


def fill_transfer_fields(driver, amount: str, phone: str):
    edits = [e for e in driver.find_elements(By.CLASS_NAME, "android.widget.EditText") if e.is_displayed()]
    if len(edits) < 2:
        raise RuntimeError(f"Champs de transfert introuvables (EditText visibles={len(edits)})")

    amount_input = edits[0]
    phone_input = edits[1]

    amount_input.click()
    amount_input.clear()
    amount_input.send_keys(amount)
    print(f"✅ Montant saisi: {amount}")

    phone_input.click()
    phone_input.clear()
    phone_input.send_keys(phone)
    print(f"✅ Téléphone saisi: {phone}")


def submit_transfer(driver, wait: WebDriverWait):
    try:
        driver.hide_keyboard()
    except Exception:
        pass

    # cibler prioritairement le bouton de la modal (android.widget.Button)
    btn = wait_click(
        driver,
        wait,
        [
            (By.XPATH, "//android.widget.Button[@text='Transférer']"),
            (AppiumBy.ACCESSIBILITY_ID, "Transférer"),
            (By.XPATH, "//*[@text='Transférer']"),
        ],
        "Bouton final Transférer",
    )
    time.sleep(0.25)

    # si toujours dans la modal, retenter par tapGesture
    still_modal = bool(driver.find_elements(By.XPATH, "//*[@text='Annuler']")) and bool(
        [e for e in driver.find_elements(By.CLASS_NAME, "android.widget.EditText") if e.is_displayed()]
    )
    if still_modal:
        rect = btn.rect
        x = int(rect["x"] + rect["width"] * 0.5)
        y = int(rect["y"] + rect["height"] * 0.5)
        driver.execute_script("mobile: clickGesture", {"x": x, "y": y})
        print("✅ Retry tap sur bouton final Transférer")
        time.sleep(0.35)

    print("✅ Clic sur le bouton final Transférer")


def confirm_and_close_after_transfer(driver, wait: WebDriverWait):
    # 1) Attendre un retour post-transfert (popup succès/échec/confirmation)
    popup_seen = False
    for label in ["Oui", "Confirmer", "Valider", "OK", "Continuer", "Succès", "Echec", "Échec", "Erreur"]:
        try:
            el = wait_click(
                driver,
                WebDriverWait(driver, 3, poll_frequency=0.2),
                [
                    (By.XPATH, f"//*[@text='{label}']"),
                    (By.XPATH, f"//*[contains(@text,'{label}')]"),
                ],
                f"Popup post-transfert: {label}",
            )
            popup_seen = True
            if el:
                time.sleep(0.35)
            time.sleep(0.5)
            break
        except Exception:
            continue
    if not popup_seen:
        # Pas de popup visible: on loggue explicitement ce cas
        # Essai toast Android (souvent non cliquable)
        try:
            toast = driver.find_element(By.XPATH, "//android.widget.Toast[1]")
            print(f"ℹ️ Toast transfert détecté: {(toast.text or '').strip()}")
        except Exception:
            print("⚠️ Aucune popup succès/échec visible après transfert.")

    # 2) Fermer la modal si encore visible
    try:
        wait_click(
            driver,
            WebDriverWait(driver, 2, poll_frequency=0.2),
            [
                (By.XPATH, "//*[@text='Fermer']"),
                (By.XPATH, "//*[contains(@content-desc,'close') or contains(@content-desc,'Close')]"),
                (By.XPATH, "//*[contains(@content-desc,'fermer') or contains(@content-desc,'Fermer')]"),
            ],
            "Fermeture modal",
        )
    except Exception:
        # Si pas de bouton explicite, back Android pour fermer le bas-sheet
        try:
            driver.back()
            time.sleep(0.3)
            print("✅ Modal fermée via back")
        except Exception:
            pass


def logout_from_account(driver, wait: WebDriverWait):
    # Revenir sur l'écran compte
    try:
        wait_click(
            driver,
            WebDriverWait(driver, 4, poll_frequency=0.2),
            [
                (AppiumBy.ACCESSIBILITY_ID, "Comptes"),
                (By.XPATH, "//*[@text='Comptes']"),
            ],
            "Onglet Comptes (retour)",
        )
    except Exception:
        try:
            driver.back()
            time.sleep(0.3)
        except Exception:
            pass

    # Trouver Déconnexion (texte variable selon accent/casse) avec plusieurs scrolls
    logout_locators = [
        (By.XPATH, "//*[@text='Déconnexion']"),
        (By.XPATH, "//*[@text='Deconnexion']"),
        (By.XPATH, "//*[contains(@text,'Déconnexion')]"),
        (By.XPATH, "//*[contains(@text,'Deconnexion')]"),
        (By.XPATH, "//*[contains(@content-desc,'Déconnexion')]"),
        (By.XPATH, "//*[contains(@content-desc,'Deconnexion')]"),
        (AppiumBy.ACCESSIBILITY_ID, "Déconnexion"),
        (AppiumBy.ACCESSIBILITY_ID, "Deconnexion"),
    ]

    clicked = False
    for attempt in range(1, 6):
        # 1) essai direct
        for by, value in logout_locators:
            try:
                elems = driver.find_elements(by, value)
                visible = [e for e in elems if e.is_displayed()]
                if visible:
                    visible[0].click()
                    print(f"✅ Click: Déconnexion [{by}={value}]")
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            break

        # 2) essai UiScrollable
        try:
            deconnect = driver.find_element(
                AppiumBy.ANDROID_UIAUTOMATOR,
                'new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView(new UiSelector().textContains("deconnexion"))',
            )
            if deconnect and deconnect.is_displayed():
                deconnect.click()
                print("✅ Click: Déconnexion [UiScrollable]")
                clicked = True
                break
        except Exception:
            pass

        # 3) fallback swipe vers le bas (sur page comptes)
        size = driver.get_window_size()
        x = int(size["width"] * 0.5)
        y_start = int(size["height"] * 0.82)
        y_end = int(size["height"] * 0.38)
        try:
            driver.swipe(x, y_start, x, y_end, 350)
            time.sleep(0.35)
            print(f"ℹ️ Scroll déconnexion tentative {attempt}/5")
        except Exception:
            pass

    if not clicked:
        print("⚠️ Déconnexion non trouvée.")
        return

    # Si popup Oui/Non de déconnexion, forcer Oui (capture: "À bientôt ... Non | Oui")
    for label in ["Oui", "Confirmer", "Se déconnecter", "Déconnexion", "Deconnexion", "OK"]:
        try:
            wait_click(
                driver,
                WebDriverWait(driver, 5, poll_frequency=0.2),
                [
                    (By.XPATH, f"//*[@text='{label}']"),
                    (By.XPATH, f"//*[contains(@text,'{label}')]"),
                ],
                f"Confirmation déconnexion: {label}",
            )
            break
        except Exception:
            continue

    # Fallback anti "Non": si popup encore visible, cliquer explicitement la zone du bouton Oui (à droite)
    try:
        yes_btn = [e for e in driver.find_elements(By.XPATH, "//*[@text='Oui' or @text='Confirmer' or @text='OK']") if e.is_displayed()]
        if yes_btn:
            yes_btn[0].click()
            print("✅ Confirmation déconnexion (fallback Oui)")
        else:
            no_btn = [e for e in driver.find_elements(By.XPATH, "//*[@text='Non']") if e.is_displayed()]
            if no_btn:
                # On clique à droite du bouton Non (position typique du Oui)
                rect = no_btn[0].rect
                x = int(rect["x"] + rect["width"] * 1.6)
                y = int(rect["y"] + rect["height"] * 0.5)
                driver.execute_script("mobile: clickGesture", {"x": x, "y": y})
                print("✅ Confirmation déconnexion (fallback tap zone Oui)")
    except Exception:
        pass

    # Vérifier retour à l'écran login partenaire
    try:
        WebDriverWait(driver, 6, poll_frequency=0.2).until(
            lambda d: bool(
                d.find_elements(By.XPATH, "//*[contains(@text,'Connexion partenaire')]")
                or d.find_elements(By.XPATH, "//*[contains(@text,'Se connecter')]")
            )
        )
        print("✅ Déconnexion confirmée, retour écran login.")
    except Exception:
        print("⚠️ Déconnexion cliquée mais retour login non confirmé.")


def run_partner_flow(driver, wait: WebDriverWait, email: str, password: str):
    print(f"\n▶️ Exécution flow pour {email}")

    # Login partenaire
    do_partner_login(driver, wait, email, password)

    # Onglet comptes puis portefeuille
    wait_click(
        driver,
        wait,
        [
            (By.XPATH, "//*[@text='Comptes']"),
            (By.XPATH, "//*[contains(@text,'Compte')]"),
            (AppiumBy.ACCESSIBILITY_ID, "Comptes"),
        ],
        "Onglet Comptes",
    )
    click_portefeuille(driver, wait)
    click_transfer_button(driver, wait)
    wait_transfer_modal_ready(driver, wait)

    ensure_conducteur_selected(driver, wait)
    fill_transfer_fields(driver, TRANSFER_AMOUNT, TRANSFER_PHONE)
    submit_transfer(driver, wait)
    confirm_and_close_after_transfer(driver, wait)
    logout_from_account(driver, wait)

    print(f"✅ Flow terminé pour {email}")


def wait_visible(driver, wait: WebDriverWait, locators: Iterable[Locator], label: str):
    last_err = None
    for by, value in locators:
        try:
            el = wait.until(EC.visibility_of_element_located((by, value)))
            print(f"✅ Visible: {label} [{by}={value}]")
            return el
        except Exception as e:
            last_err = e
    raise TimeoutException(f"Element non visible: {label} | err={last_err}")


def fill_modal_inputs(driver, amount: str, phone: str):
    # Dans la modal "Transférer", on prend les EditText visibles:
    # - 1er champ texte -> montant
    # - 2e champ texte -> téléphone
    edits = driver.find_elements(By.CLASS_NAME, "android.widget.EditText")
    visible_edits = [e for e in edits if e.is_displayed()]
    if len(visible_edits) < 2:
        raise RuntimeError(f"Champs EditText insuffisants dans la modal: {len(visible_edits)}")

    amount_input = visible_edits[0]
    phone_input = visible_edits[1]

    amount_input.click()
    amount_input.clear()
    amount_input.send_keys(amount)
    print(f"✅ Montant saisi: {amount}")

    phone_input.click()
    phone_input.clear()
    phone_input.send_keys(phone)
    print(f"✅ Téléphone saisi: {phone}")


def tap_bottom_center(driver):
    size = driver.get_window_size()
    x = int(size["width"] * 0.5)
    y = int(size["height"] * 0.92)
    actions = ActionChains(driver)
    actions.w3c_actions.pointer_action.move_to_location(x, y)
    actions.w3c_actions.pointer_action.pointer_down()
    actions.w3c_actions.pointer_action.pause(0.08)
    actions.w3c_actions.pointer_action.release()
    actions.perform()
    print(f"✅ Tap fallback bas-centre ({x}, {y})")


def find_login_fields(driver):
    edits = driver.find_elements(By.CLASS_NAME, "android.widget.EditText")
    visible_edits = [e for e in edits if e.is_displayed()]
    if len(visible_edits) >= 2:
        return visible_edits[0], visible_edits[1]
    raise RuntimeError("Impossible de trouver les champs login (email/mobile + mot de passe).")


def do_partner_login(driver, wait: WebDriverWait, email: str, password: str):
    # Ecran connexion: bouton "Connexion partenaire" (bas) -> onglet E-mail -> champs
    try:
        wait_click(
            driver,
            wait,
            [
                (By.XPATH, "//*[@text='Connexion partenaire']"),
                (By.XPATH, "//*[contains(@text,'Connexion partenaire')]"),
                (AppiumBy.ACCESSIBILITY_ID, "Connexion partenaire"),
                (By.XPATH, "//*[@content-desc='Connexion partenaire']"),
                (By.XPATH, "//*[contains(@content-desc,'Connexion partenaire')]"),
            ],
            "Bouton Connexion partenaire",
        )
        time.sleep(1)
    except Exception:
        print("ℹ️ Bouton 'Connexion partenaire' non trouvé, fallback tap bas-centre.")
        tap_bottom_center(driver)
        time.sleep(1)

    try:
        wait_click(
            driver,
            wait,
            [
                (By.XPATH, "//*[@text='E-mail']"),
                (By.XPATH, "//*[contains(@text,'E-mail')]"),
                (AppiumBy.ACCESSIBILITY_ID, "E-mail"),
                (By.XPATH, "//*[@content-desc='E-mail']"),
            ],
            "Onglet E-mail",
        )
    except Exception:
        print("ℹ️ Onglet E-mail non trouvé, on continue avec les champs visibles.")

    # Champs login: premier EditText visible = email/mobile, second = mot de passe
    wait.until(lambda d: len([e for e in d.find_elements(By.CLASS_NAME, "android.widget.EditText") if e.is_displayed()]) >= 2)
    email_input, password_input = find_login_fields(driver)

    email_input.click()
    email_input.clear()
    email_input.send_keys(email)
    print(f"✅ Email saisi: {email}")

    password_input.click()
    password_input.clear()
    password_input.send_keys(password)
    print("✅ Mot de passe saisi")

    # Consentement CGU si non coché
    try:
        consent_candidates = driver.find_elements(
            By.XPATH,
            "//*[contains(@text,'J’accepte les Conditions générales') or contains(@text,\"J'accepte les Conditions générales\")]",
        )
        if consent_candidates:
            consent_candidates[0].click()
            print("✅ Consentement CGU coché")
    except Exception:
        pass

    wait_click(
        driver,
        wait,
        [
            (By.XPATH, "//*[@text='Se connecter']"),
            (By.XPATH, "//*[contains(@text,'Se connecter')]"),
            (AppiumBy.ACCESSIBILITY_ID, "Se connecter"),
            (By.XPATH, "//*[@content-desc='Se connecter']"),
        ],
        "Bouton Se connecter",
    )
    time.sleep(3)
    print("✅ Login tenté")


def main():
    opts = UiAutomator2Options()
    opts.platform_name = "Android"
    opts.automation_name = "UiAutomator2"
    if DEVICE_UDID:
        opts.udid = DEVICE_UDID
    opts.app_package = APP_PACKAGE
    opts.app_activity = APP_ACTIVITY
    opts.no_reset = True
    opts.auto_launch = False
    opts.new_command_timeout = 180
    opts.ignore_hidden_api_policy_error = True
    opts.skip_device_initialization = True

    print("🚀 Connexion Appium...")
    driver = webdriver.Remote(APPIUM_URL, options=opts)
    wait = WebDriverWait(driver, 8, poll_frequency=0.2)

    try:
        print("📱 Démarrage forcé de l'application...")
        try:
            driver.terminate_app(APP_PACKAGE)
            time.sleep(0.5)
        except Exception:
            pass

        driver.activate_app(APP_PACKAGE)
        time.sleep(1)
        try:
            driver.start_activity(APP_PACKAGE, APP_ACTIVITY)
        except Exception:
            # activate_app suffit souvent; start_activity peut échouer selon version
            pass

        print("📱 App ouverte, attente écran principal...")
        time.sleep(1)

        # Partenaire 5 puis partenaire 8 (même mot de passe)
        run_partner_flow(driver, wait, PARTNER_EMAIL, PARTNER_PASSWORD)
        time.sleep(1)
        run_partner_flow(driver, wait, PARTNER_EMAIL_2, PARTNER_PASSWORD)

        print("🎉 Tous les flows sont terminés (partenaire5 puis partenaire8).")

    finally:
        driver.quit()
        print("🧹 Session Appium fermée.")


if __name__ == "__main__":
    main()

