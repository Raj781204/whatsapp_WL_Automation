import ipaddress
import json
import os
import re
import time
import unicodedata

import pymysql
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SCRIPT_VERSION = "4.0 (name + ip only, no port, single table)"
DEBUG = True                          # print every chat message that contains an IP

CHAT_NAME = "Grafana_Alerts"
OUTPUT_FILE = "ip_list.json"
PROFILE_DIR = "./wa_chrome_profile"   # login is saved here (scan QR only once)
SCROLL_TIMES = 5                      # how many times to scroll up to load older messages
TABLE_NAME = "ip_addresses"           # create it with create_ip_table.sql

# "latest" = newest message first | "oldest" = oldest first | "ip" = ascending by IP
SORT_ORDER = "latest"

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "raj"),
    "password": os.getenv("DB_PASSWORD", "Raj@2003"),
    "database": os.getenv("DB_NAME", "whatsapp_db"),
    "charset": "utf8mb4",
}

SEARCH_SELECTOR = (
    'input[aria-label="Search or start a new chat"], '
    '[role="textbox"][aria-label="Search or start a new chat"]'
)

DISMISS_TEXTS = [
    "continue", "got it", "ok", "close", "not now",
    "dismiss", "skip", "later", "cancel", "no thanks",
]

# IPv4 with optional /prefix, e.g. 116.72.118.73 or 152.57.87.32/32
IP_REGEX = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?")

# Request wording that is NOT a person's name (removed from the text before the IP)
NOISE_REGEX = re.compile(r"pls\s*wl\s*ip\s*for\s*cm\s*warehouse", re.IGNORECASE)


# --------------------------------------------------------------------------
# Browser helpers
# --------------------------------------------------------------------------
def build_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--start-maximized")
    return webdriver.Chrome(options=opts)


def dismiss_dialogs(driver, max_rounds=5):
    """Close any modal dialogs blocking the UI."""
    for _ in range(max_rounds):
        dialogs = driver.find_elements(
            By.CSS_SELECTOR, 'div[role="dialog"][aria-modal="true"]'
        )
        dialogs = [d for d in dialogs if d.is_displayed()]
        if not dialogs:
            return

        dialog = dialogs[0]
        try:
            print("Dialog detected:", dialog.text.replace("\n", " | ")[:200])
        except Exception:
            pass

        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(1)
        try:
            if not dialog.is_displayed():
                continue
        except Exception:
            continue

        clicked = False
        buttons = dialog.find_elements(By.CSS_SELECTOR, 'button, div[role="button"]')
        for btn in buttons:
            label = (btn.text or btn.get_attribute("aria-label") or "").strip().lower()
            if label and any(label == t or label.startswith(t) for t in DISMISS_TEXTS):
                try:
                    driver.execute_script("arguments[0].click();", btn)
                    clicked = True
                    break
                except Exception:
                    pass

        if not clicked and buttons:
            try:
                driver.execute_script("arguments[0].click();", buttons[0])
            except Exception:
                pass

        time.sleep(1)


def wait_for_app_ready(driver, wait):
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_SELECTOR)))
    time.sleep(3)


def safe_click(driver, element):
    """Normal click; if blocked or not interactable, scroll into view and click via JS."""
    try:
        element.click()
    except (ElementClickInterceptedException, ElementNotInteractableException):
        dismiss_dialogs(driver)
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", element
        )


def norm(text):
    """Normalise a chat title: drop invisible direction marks, collapse spaces, ignore case."""
    text = "".join(ch for ch in (text or "") if unicodedata.category(ch) != "Cf")
    return " ".join(text.split()).casefold()


def get_chat_rows(driver):
    """Return [(title, element)] for every chat row currently in the list."""
    rows = []
    for el in driver.find_elements(By.CSS_SELECTOR, '[data-testid="cell-frame-title"]'):
        try:
            spans = el.find_elements(By.CSS_SELECTOR, "span[title]")
            title = spans[0].get_attribute("title") if spans else el.text
            rows.append((title, el))
        except StaleElementReferenceException:
            continue
    return rows


def is_shown(el):
    try:
        return el.is_displayed()
    except WebDriverException:
        return False


def find_chat_row(driver):
    """Find the row whose title is CHAT_NAME (exact match; a single partial match is
    accepted only if there is no exact one). Visible rows are preferred."""
    target = norm(CHAT_NAME)
    rows = get_chat_rows(driver)
    exact = [el for t, el in rows if norm(t) == target]
    if exact:
        return next((el for el in exact if is_shown(el)), exact[0])
    partial = [el for t, el in rows if target in norm(t)]
    return partial[0] if len(partial) == 1 else None


def chat_is_open(driver, timeout=8):
    """True once the chat header on the right shows CHAT_NAME."""
    target = norm(CHAT_NAME)
    end = time.time() + timeout
    while time.time() < end:
        try:
            header = driver.find_element(By.CSS_SELECTOR, "#main header")
            first_line = header.text.split("\n")[0] if header.text else ""
            titles = [norm(first_line)] + [
                norm(e.get_attribute("title"))
                for e in header.find_elements(By.CSS_SELECTOR, "[title]")
            ]
            if target in titles:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def click_row(driver, el, attempt):
    """Click the whole chat row (not just the title text), a different way per attempt."""
    row = driver.execute_script(
        "return arguments[0].closest('[role=row]') || arguments[0];", el
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", row)
    time.sleep(0.5)
    if attempt == 1:
        safe_click(driver, row)
    elif attempt == 2:
        driver.execute_script("arguments[0].click();", row)
    else:
        ActionChains(driver).move_to_element(row).pause(0.3).click().perform()


def open_chat(driver, wait):
    driver.get("https://web.whatsapp.com")

    wait_for_app_ready(driver, wait)
    dismiss_dialogs(driver)

    for attempt in (1, 2, 3):
        search_box = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_SELECTOR))
        )
        try:
            search_box.click()
        except ElementClickInterceptedException:
            dismiss_dialogs(driver)
            driver.execute_script("arguments[0].focus(); arguments[0].click();", search_box)

        search_box.send_keys(Keys.CONTROL, "a")
        search_box.send_keys(Keys.BACKSPACE)
        search_box.send_keys(CHAT_NAME)

        row, end = None, time.time() + 15
        while time.time() < end:
            row = find_chat_row(driver)
            if row is not None:
                break
            time.sleep(0.5)

        if row is None:
            print(f"Attempt {attempt}: chat '{CHAT_NAME}' not found in the list. Titles seen:")
            for t, _ in get_chat_rows(driver):
                print("   -", repr(t))
            continue

        try:
            click_row(driver, row, attempt)
        except WebDriverException as e:
            print(f"Attempt {attempt}: click failed ({type(e).__name__}), trying another way...")
            continue

        if chat_is_open(driver):
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#main")))
            time.sleep(2)
            print(f"Opened chat: {CHAT_NAME}")
            return
        print(f"Attempt {attempt}: clicked, but the chat header does not show '{CHAT_NAME}'.")

    driver.save_screenshot("debug_open_chat.png")
    raise RuntimeError(
        f"Could not open chat '{CHAT_NAME}'. Screenshot saved to debug_open_chat.png"
    )


def scroll_up(driver):
    """Scroll the message pane up to load older messages."""
    for _ in range(SCROLL_TIMES):
        try:
            pane = driver.find_element(
                By.CSS_SELECTOR, '#main div[role="application"]'
            )
            driver.execute_script("arguments[0].scrollTop = 0;", pane)
        except Exception:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.PAGE_UP)
        time.sleep(1.5)


# --------------------------------------------------------------------------
# Parsing (name + ip, no port)
# --------------------------------------------------------------------------
def valid_ip(ip, prefix):
    if not all(0 <= int(p) <= 255 for p in ip.split(".")):
        return False
    return prefix is None or 0 <= int(prefix) <= 32


def clean_name(text):
    """Turn the text before an IP into a name: 'Hari-' -> 'Hari', 'Venky WH - ' -> 'Venky WH'.
    Returns None when nothing but request wording / separators is left."""
    text = NOISE_REGEX.sub(" ", text)
    text = text.strip(" \t\r\n-\u2013\u2014:,.")
    text = " ".join(text.split())
    return text or None


def parse_message(text):
    """Return a list of {"name":..., "ip":...} found in one message."""
    found = []
    matches = [m for m in IP_REGEX.finditer(text) if valid_ip(m.group(1), m.group(2))]
    prev_end = 0
    for m in matches:
        ip = m.group(1) + (f"/{m.group(2)}" if m.group(2) else "")
        found.append({"name": clean_name(text[prev_end:m.start()]), "ip": ip})
        # anything after the IP (old port text like '-330') is simply ignored
        prev_end = m.end()
    return found


def extract_messages(driver):
    """One item per chat message (OLDEST first as shown in WhatsApp),
    each item is the list of name/IP entries in that message."""
    messages = []
    spans = driver.find_elements(
        By.CSS_SELECTOR,
        '#main span.selectable-text, #main span[data-testid="selectable-text"]',
    )
    for sp in spans:
        text = sp.text.strip()
        if not text:
            continue
        entries = parse_message(text)
        if entries:
            messages.append(entries)
            if DEBUG:
                print(f"  chat message (oldest->newest): {text[:70]!r} -> {entries}")
    return messages


def dedupe(entries):
    """One row per IP. If the first one has no name, take the name from a later duplicate."""
    by_ip = {}
    for e in entries:
        if e["ip"] not in by_ip:
            by_ip[e["ip"]] = dict(e)
        elif not by_ip[e["ip"]]["name"] and e["name"]:
            by_ip[e["ip"]]["name"] = e["name"]
    return list(by_ip.values())


def ip_sort_key(e):
    return ipaddress.ip_network(e["ip"], strict=False)


def order_entries(messages):
    if SORT_ORDER == "latest":
        return dedupe([e for msg in reversed(messages) for e in msg])
    flat = [e for msg in messages for e in msg]
    if SORT_ORDER == "ip":
        return sorted(dedupe(flat), key=ip_sort_key)
    return dedupe(flat)  # "oldest"


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------
def save_to_db(entries):
    """Insert into ip_addresses (name, ip, status). IP is unique, so repeats are not duplicated.
    Sets entry["status"] on every entry. Returns (inserted, already_existed)."""
    if not entries:
        return 0, 0

    conn = pymysql.connect(**DB_CONFIG)
    inserted = existed = 0
    try:
        with conn.cursor() as cur:
            for e in entries:
                cur.execute(
                    f"INSERT INTO `{TABLE_NAME}` (name, ip, status) VALUES (%s, %s, 'Success') "
                    "ON DUPLICATE KEY UPDATE name = COALESCE(name, VALUES(name))",
                    (e["name"], e["ip"]),
                )
                # MySQL rowcount: 1 = new row, 2 = existing row changed, 0 = nothing changed
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    existed += 1
                e["status"] = "Success"
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return inserted, existed


def main():
    print(f"Running scrape.py v{SCRIPT_VERSION}, sort order = {SORT_ORDER}")
    driver = build_driver()
    wait = WebDriverWait(driver, 120)  # long wait allows QR scan on first run
    try:
        open_chat(driver, wait)
        scroll_up(driver)
        data = order_entries(extract_messages(driver))
        print(f"Found {len(data)} unique IP entries (order: {SORT_ORDER}).")

        json_ok = db_ok = False

        # 1) MySQL
        try:
            inserted, existed = save_to_db(data)
            db_ok = True
            print(f"DB: Success - {inserted} new row(s) inserted, {existed} already existed")
        except Exception as e:
            for item in data:
                item["status"] = "Failure"
            print(f"DB: Failure - could not save to the database ({e})")

        # 2) JSON
        try:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            json_ok = True
            print(f"JSON: Success - {len(data)} entries saved to {OUTPUT_FILE}")
        except Exception as e:
            print(f"JSON: Failure - could not save to {OUTPUT_FILE} ({e})")

        print(json.dumps(data, indent=2))

        if json_ok and db_ok:
            print("\nOverall: Success - data saved in both JSON and DB.")
        elif json_ok or db_ok:
            print("\nOverall: Failure - only one of JSON / DB was saved. See messages above.")
        else:
            print("\nOverall: Failure - nothing was saved. See messages above.")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()