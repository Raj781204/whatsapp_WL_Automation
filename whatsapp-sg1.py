import ipaddress
import json
import os
from pathlib import Path
import re
import time
import unicodedata

from dotenv import load_dotenv
import pymysql
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    InvalidSessionIdException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Load .env BEFORE building DB_CONFIG
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

SCRIPT_VERSION = "5.9 (keep all multi-resource entries on latest message)"
DEBUG = True                          # print every chat message that contains an IP
LATEST_ONLY = True                    # True = latest message only (all its resources kept)

CHAT_NAME = "Grafana_Alerts"
OUTPUT_FILE = "ip_list.json"
PROFILE_DIR = "./wa_chrome_profile"   # login is saved here (scan QR only once)
SCROLL_TIMES = 0                      # 0 = do not scroll back into older history
TABLE_NAME = "ip_addresses"           # create it with create_ip_table.sql

# "latest" = newest message first | "oldest" = oldest first | "ip" = ascending by IP
SORT_ORDER = "latest"

# Resource-to-port mapping (case-insensitive); update CRM port once confirmed
RESOURCE_PORT_MAP = {
    "WH": 5431,
    "CMOL": 3306,
    "CRM": 443,
}
RESOURCE_KEYWORDS = set(RESOURCE_PORT_MAP.keys())
DEFAULT_PORT = int(os.getenv("AWS_DEFAULT_PORT", "443"))
FILLER_WORDS = {"PLZ", "PLEASE", "PLS", "WL", "WHITELIST", "FOR", "IP", "IPS"}

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "raj"),
    "password": os.getenv("DB_PASSWORD", ""),
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
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")
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
    """True once #main is visible and shows CHAT_NAME in its header."""
    target = norm(CHAT_NAME)
    end = time.time() + timeout
    while time.time() < end:
        try:
            main_el = driver.find_element(By.CSS_SELECTOR, "#main")
            if not main_el.is_displayed():
                time.sleep(0.5)
                continue
            headers = driver.find_elements(By.CSS_SELECTOR, "#main header")
            for header in headers:
                raw_text = header.text or ""
                if target in norm(raw_text):
                    return True
                for e in header.find_elements(By.CSS_SELECTOR, "[title]"):
                    if target in norm(e.get_attribute("title")):
                        return True
        except InvalidSessionIdException:
            raise
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
        except InvalidSessionIdException:
            raise
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
    if SCROLL_TIMES <= 0:
        return
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
# Parsing (name + resource + port + ip)
# --------------------------------------------------------------------------
def valid_ip(ip, prefix):
    if not all(0 <= int(p) <= 255 for p in ip.split(".")):
        return False
    return prefix is None or 0 <= int(prefix) <= 32


def split_label_parts(text):
    """Split text before an IP into (name, resources_list)."""
    text = NOISE_REGEX.sub(" ", text or "")
    text = re.sub(r"[-\u2013\u2014:,/]+", " ", text)
    text = " ".join(text.split()).strip()
    if not text:
        return None, []

    tokens = text.split(" ")
    name_parts = []
    resources = []

    for tok in tokens:
        clean_tok = tok.strip("()[]{}")
        upper_tok = clean_tok.upper()
        if not upper_tok:
            continue
        if upper_tok in RESOURCE_KEYWORDS:
            if upper_tok not in resources:
                resources.append(upper_tok)
        elif upper_tok not in FILLER_WORDS and not name_parts:
            name_parts.append(clean_tok)

    name = " ".join(name_parts).strip() or None
    return name, resources


def parse_message(text):
    """Return one entry per (ip, resource, port) found in one message."""
    found = []
    matches = [m for m in IP_REGEX.finditer(text) if valid_ip(m.group(1), m.group(2))]
    prev_end = 0
    for m in matches:
        ip = m.group(1) + (f"/{m.group(2)}" if m.group(2) else "")
        name, resources = split_label_parts(text[prev_end:m.start()])
        if resources:
            for res in resources:
                found.append({
                    "name": name,
                    "resource": res,
                    "port": RESOURCE_PORT_MAP.get(res, DEFAULT_PORT),
                    "ip": ip,
                })
        else:
            found.append({
                "name": name,
                "resource": "DEFAULT",
                "port": DEFAULT_PORT,
                "ip": ip,
            })
        prev_end = m.end()
    return found


def extract_messages(driver):
    """One item per chat message (OLDEST first as shown in WhatsApp),
    each item is the list of name/resource/port/IP entries in that message."""
    messages = []
    elements = driver.find_elements(
        By.CSS_SELECTOR,
        '#main div[data-testid="msg-container"], '
        '#main div.message-in, #main div.message-out, '
        '#main span.selectable-text, #main span[data-testid="selectable-text"], '
        '#main div.copyable-text',
    )

    seen_texts = set()
    for el in elements:
        try:
            text = (el.text or "").strip()
        except StaleElementReferenceException:
            continue
        if not text or text in seen_texts:
            continue
        if not IP_REGEX.search(text):
            continue
        seen_texts.add(text)
        entries = parse_message(text)
        if entries:
            messages.append(entries)
            if DEBUG:
                print(f"  chat message (oldest->newest): {text[:90]!r} -> {entries}")

    if not messages and DEBUG:
        try:
            main_text = (driver.find_element(By.CSS_SELECTOR, "#main").text or "").splitlines()
            ip_lines = [line.strip() for line in main_text if IP_REGEX.search(line)]
            print(f"  [DEBUG] Found {len(ip_lines)} IP lines in #main.text: {ip_lines[:10]}")
        except Exception:
            pass

    return messages


def dedupe(entries):
    """Deduplicate by (ip, resource, port) so multiple resources/ports for one IP stay separate."""
    seen = {}
    for e in entries:
        key = (e["ip"], e.get("resource"), e.get("port"))
        if key not in seen:
            seen[key] = dict(e)
        elif not seen[key].get("name") and e.get("name"):
            seen[key]["name"] = e["name"]
    return list(seen.values())


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
# Saving (MySQL)
# --------------------------------------------------------------------------
def save_to_db(entries):
    """Insert into ip_addresses (name, resource, port, ip, status) keyed on (ip, resource, port)
    without wiping existing rows."""
    if not entries:
        return 0, 0

    conn = pymysql.connect(**DB_CONFIG)
    inserted = existed = 0
    try:
        with conn.cursor() as cur:
            for e in entries:
                res_val = e.get("resource") or "DEFAULT"
                port_val = e.get("port") if e.get("port") is not None else DEFAULT_PORT
                cur.execute(
                    f"INSERT INTO `{TABLE_NAME}` (name, resource, port, ip, status) "
                    "VALUES (%s, %s, %s, %s, 'Success') "
                    "ON DUPLICATE KEY UPDATE "
                    "name = COALESCE(name, VALUES(name))",
                    (e.get("name"), res_val, port_val, e["ip"]),
                )
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
    print(f"Running whatsapp-sg1.py v{SCRIPT_VERSION}, sort order = {SORT_ORDER}")
    driver = build_driver()
    wait = WebDriverWait(driver, 120)  # long wait allows QR scan on first run
    try:
        open_chat(driver, wait)
        scroll_up(driver)
        messages = extract_messages(driver)
        if LATEST_ONLY and messages:
            data = dedupe(messages[-1])
        else:
            data = order_entries(messages)
        print(f"Found {len(data)} unique (ip, resource, port) entries (order: {SORT_ORDER}).")

        if not data:
            print(
                "No entries found in chat. Existing MySQL rows and "
                f"{OUTPUT_FILE} were left untouched."
            )
            return

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

        # 2) JSON (only written when entries are found)
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
    except InvalidSessionIdException:
        print("Browser session ended unexpectedly. Close any leftover Chrome window and retry.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
