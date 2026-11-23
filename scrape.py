import ipaddress
import json
import os
import re
import time
import unicodedata
from pathlib import Path

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


# --------------------------------------------------------------------------
# .env support (so HEADLESS, DB_PASSWORD, ... can live in the .env file)
# --------------------------------------------------------------------------
def load_env_file():
    for path in (Path(__file__).resolve().parent / ".env", Path.cwd() / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, value = line.split("=", 1)
            value = value.split(" #")[0].strip().strip("\"'")
            if not os.environ.get(key.strip()):      # a real environment value wins
                os.environ[key.strip()] = value
        return


def env_bool(name, default=False):
    """True/False from the environment / .env: true, false, yes, no, 1, 0, on, off."""
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    print(f"Note: {name}='{value}' is not true or false, using {str(default).lower()}.")
    return default


load_env_file()

SCRIPT_VERSION = "2.9 (HEADLESS=true/false in .env)"
DEBUG = True                          # print every chat message that contains an IP

# Put this in your .env file:
#     HEADLESS=true    -> Chrome runs without a window (needs an already logged-in profile)
#     HEADLESS=false   -> Chrome window is shown (needed once to scan the QR code)
# If the line is missing, the default below (False) is used.
HEADLESS = env_bool("HEADLESS", False)
HEADLESS_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)  # headless Chrome announces itself as "HeadlessChrome", which WhatsApp Web can refuse

CHAT_NAME = "CM-Support"
OUTPUT_FILE = "ip_port.json"
PROFILE_DIR = "./wa_chrome_profile"   # login is saved here (scan QR only once)
SCROLL_TIMES = 5                      # how many times to scroll up to load older messages

# "latest" = newest message first (id 1 = latest message)
# "oldest" = oldest message first
# "ip"     = ascending by IP address
SORT_ORDER = "latest"

# MySQL settings (override with environment variables if you prefer)
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "your_password"),
    "database": os.getenv("DB_NAME", "whatsapp_db"),
    "charset": "utf8mb4",
}
DB_TABLE = "server_details"

SEARCH_SELECTOR = (
    'input[aria-label="Search or start a new chat"], '
    '[role="textbox"][aria-label="Search or start a new chat"]'
)

# Shown instead of the chat list when WhatsApp Web is not logged in
QR_SELECTOR = 'canvas[aria-label*="QR" i], [data-testid="qrcode"], div[data-ref]'

DISMISS_TEXTS = [
    "continue", "got it", "ok", "close", "not now",
    "dismiss", "skip", "later", "cancel", "no thanks",
]

# IPv4 address (octets validated below)
IP_REGEX = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
# "port:8123", "ports 80, 443 & 8080", "port 8123 and 9000", "port:8123 ... port:9000"
PORTS_REGEX = re.compile(
    r"\bports?(?![a-z])\s*[:=\-]?\s*(\d{2,5}(?:\s*(?:,|;|/|&|\||\band\b)\s*\d{2,5})*)",
    re.IGNORECASE,
)
NUMBER_REGEX = re.compile(r"\d{2,5}")


# --------------------------------------------------------------------------
# Browser helpers
# --------------------------------------------------------------------------
def build_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    if HEADLESS:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")   # --start-maximized does nothing without a window
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument(f"--user-agent={HEADLESS_USER_AGENT}")
    else:
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
    """Wait for the chat list. In headless mode a QR code means 'not logged in': stop with a clear message."""
    limit = 60 if HEADLESS else 120
    end = time.time() + limit
    while time.time() < end:
        if driver.find_elements(By.CSS_SELECTOR, SEARCH_SELECTOR):
            time.sleep(3)
            return
        if HEADLESS and driver.find_elements(By.CSS_SELECTOR, QR_SELECTOR):
            driver.save_screenshot("debug_not_logged_in.png")
            raise RuntimeError(
                "WhatsApp Web is not logged in (QR code shown) and HEADLESS is true. "
                "Run once with HEADLESS=false, scan the QR code, then switch back to true."
            )
        time.sleep(1)
    driver.save_screenshot("debug_not_ready.png")
    raise RuntimeError(f"WhatsApp Web did not load within {limit} s "
                       "(see debug_not_ready.png; if it shows a QR code, log in with HEADLESS=false first).")


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

        # Wait (up to 15 s) for the filtered list to show the chat
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
# Parsing
# --------------------------------------------------------------------------
def valid_ip(ip):
    return all(0 <= int(p) <= 255 for p in ip.split("."))


def extract_ports(segment):
    """All valid ports mentioned in a piece of text, in order, without repeats."""
    ports = []
    for m in PORTS_REGEX.finditer(segment):
        for num in NUMBER_REGEX.findall(m.group(1)):
            p = int(num)
            if 1 <= p <= 65535 and p not in ports:
                ports.append(p)
    return ports


def parse_message(text):
    """Return a list of {"ip":..., "port":...} found in one message.
    One entry per IP + port, so "1.2.3.4 ports 80, 443" gives two entries.
    Port is None when the message has no port."""
    found = []
    matches = [m for m in IP_REGEX.finditer(text) if valid_ip(m.group(1))]
    for i, m in enumerate(matches):
        # Look for ports only between this IP and the next IP
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        ports = extract_ports(text[m.end():end])
        if ports:
            found.extend({"ip": m.group(1), "port": p} for p in ports)
        else:
            found.append({"ip": m.group(1), "port": None})
    return found


def extract_messages(driver):
    """Return a list (one item per chat message, OLDEST first as shown in
    WhatsApp) where each item is the list of IP/port entries in that message."""
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
    seen, out = set(), []
    for e in entries:
        key = (e["ip"], e["port"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def order_entries(messages):
    """Flatten messages into one list in the order selected by SORT_ORDER.
    Duplicates are removed, keeping the first one in that order
    (so with "latest" the newest occurrence of an IP is the one kept)."""
    if SORT_ORDER == "latest":
        flat = [e for msg in reversed(messages) for e in msg]
        return dedupe(flat)
    flat = [e for msg in messages for e in msg]
    if SORT_ORDER == "ip":
        return sorted(
            dedupe(flat),
            key=lambda e: (
                ipaddress.IPv4Address(e["ip"]),
                e["port"] is None,
                e["port"] or 0,
            ),
        )
    return dedupe(flat)  # "oldest"


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------
def ensure_status_column(cur):
    """Add the `status` column to the table if it doesn't exist yet."""
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = 'status'",
        (DB_CONFIG["database"], DB_TABLE),
    )
    if cur.fetchone()[0] == 0:
        cur.execute(f"ALTER TABLE {DB_TABLE} ADD COLUMN status VARCHAR(20) DEFAULT NULL")
        print(f"DB: added column `status` to {DB_TABLE}")


def save_to_db(entries):
    """Insert IP/Port/status rows into server_details in the given order,
    skipping IP+port pairs that are already stored.
    Returns (inserted, skipped). Raises on any error (and rolls back)."""
    if not entries:
        return 0, 0

    # `<=>` is MySQL's NULL-safe equals, so a NULL port also matches correctly
    insert_sql = (
        f"INSERT INTO {DB_TABLE} (IP, Port, status) "
        "SELECT %s, %s, %s FROM DUAL "
        f"WHERE NOT EXISTS (SELECT 1 FROM {DB_TABLE} WHERE IP = %s AND Port <=> %s)"
    )
    fill_status_sql = (
        f"UPDATE {DB_TABLE} SET status = 'Success' "
        "WHERE IP = %s AND Port <=> %s AND status IS NULL"
    )

    conn = pymysql.connect(**DB_CONFIG)
    inserted = 0
    try:
        with conn.cursor() as cur:
            ensure_status_column(cur)
            for e in entries:
                cur.execute(
                    insert_sql,
                    (e["ip"], e["port"], "Success", e["ip"], e["port"]),
                )
                if cur.rowcount:
                    inserted += cur.rowcount
                else:
                    # already stored earlier: just make sure its status is filled
                    cur.execute(fill_status_sql, (e["ip"], e["port"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return inserted, len(entries) - inserted


def main():
    print(f"Running scrape.py v{SCRIPT_VERSION}, sort order = {SORT_ORDER}, headless = {HEADLESS}")
    driver = build_driver()
    wait = WebDriverWait(driver, 120)  # long wait allows QR scan on first run
    try:
        open_chat(driver, wait)
        scroll_up(driver)
        data = order_entries(extract_messages(driver))
        print(f"Found {len(data)} unique IP entries (order: {SORT_ORDER}).")

        json_ok = db_ok = False

        # 1) Save to MySQL
        try:
            inserted, skipped = save_to_db(data)
            db_ok = True
            for e in data:
                e["status"] = "Success"
            print(
                f"DB: Success - {inserted} new row(s) inserted into "
                f"{DB_TABLE}, {skipped} already existed"
            )
        except Exception as e:
            for item in data:
                item["status"] = "Failure"
            print(f"DB: Failure - could not save to {DB_TABLE} ({e})")

        # 2) Save to JSON (each entry carries its Success/Failure status)
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
