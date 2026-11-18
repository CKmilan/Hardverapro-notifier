

from datetime import datetime
import html
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin
import requests

BASE_URL = "https://hardverapro.hu"
CATEGORY_URL = f"{BASE_URL}/aprok/notebook/pc/index.html"
OFFSETS = [0, 100]  # Oldalanként 100 hirdetés -> a legfrissebb 200 találatot figyeli
SEEN_FILE = Path("seen_notebooks.txt")

# --- EMAIL BEÁLLÍTÁSOK ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
SENDER_EMAIL = "XXXXXXXXXXXXXXXXXXXXXXX"
APP_PASSWORD = "XXXXXXXXXXXXXXXXXX"  # 16 karakteres Google Alkalmazásjelszó
RECIPIENT_EMAIL = "XXXXXXXXXXXXXXXXXXXXXXX"

# --- SZŰRŐK ---
SKIP_FEATURED = True  # Kihagyja a fizetett "Kiemelt" hirdetéseket

# Csak a 4060, 5050 vagy 5060 kártyát fogadja el (akkumulátor mAh értékeket kizárva)
INCLUDE_PATTERN = re.compile(
    r"\b(?:(?:rtx|geforce)[\s-]*)?(4060|5050|5060)\b(?!\s*mah\b)",
    re.IGNORECASE,
)

# Ha a címben már egy másik videokártya vagy MacBook szerepel, nem nyitja meg a hirdetést
OTHER_GPU_PATTERN = re.compile(
    r"\b(?:rtx|geforce|gtx)[\s-]*(?:1050|1060|1070|1080|1650|1660|2050|2060|2070|2080|3050|3060|3070|3080|4050|4070|4080|4090|5070|5080|5090)\b"
    r"|\b(?:radeon|rx\s*\d{3,4}|macbook|applecare|chromebook)\b",
    re.IGNORECASE,
)

TITLE_EXCLUDE_PATTERN = re.compile(
    r"\b(keresek|vásárolok|veszek|alkatrésznek|egpu)\b|(?<!esztétikai\s)(?<!esztétikailag\s)\bhibás\b",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
}


def strip_tags(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_url(raw_url: str) -> str:
    full_url = urljoin(BASE_URL, raw_url)
    return re.sub(r"/(?:index|friss)\.html$", "", full_url, flags=re.I)


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    return set(SEEN_FILE.read_text(encoding="utf-8").splitlines())


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text("\n".join(sorted(seen)), encoding="utf-8")


def send_email(gpu_label: str, title: str, price: str, posted_time: str, link: str) -> None:
    price_tag = f" - {price}" if price else ""
    msg = EmailMessage()
    msg["Subject"] = f"[HardverApró {gpu_label}{price_tag}] {title}"
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    msg.set_content(
        f"{title}\n"
        f"Ár: {price or 'Nincs megadva'}\n"
        f"Időpont: {posted_time or 'Ismeretlen'}\n\n"
        f"Link: {link}"
    )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.send_message(msg)
    print(f"  -> [EMAIL ELKÜLDVE - {gpu_label}] {title} ({price})")


def description_has_target_gpu(ad_url: str) -> str | None:
    resp = requests.get(f"{ad_url}/index.html", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    page = resp.text

    # Csak a hirdetés főcíme (<h1>) és a "Hirdető:" blokk közötti saját szöveget nézi
    h1_pos = page.find("<h1")
    if h1_pos != -1:
        page = page[h1_pos:]
    seller_pos = page.find("Hirdető:")
    if seller_pos != -1:
        page = page[:seller_pos]

    match = INCLUDE_PATTERN.search(strip_tags(page))
    return match.group(1) if match else None


def parse_listings(page_html: str) -> list[tuple[str, str, str, str, bool]]:
    results: list[tuple[str, str, str, str, bool]] = []
    seen_in_page: set[str] = set()

    parts = re.split(
        r'<li\s+[^>]*class=["\'][^"\']*\bmedia\b[^"\']*["\'][^>]*>',
        page_html,
        flags=re.I,
    )

    for raw_block in parts[1:]:
        block = raw_block.split("</ul>")[0]

        url_match = re.search(
            r'href=["\']((?:https://hardverapro\.hu)?/apro/[^"\'?#\s]+/(?:index|friss)\.html)["\']',
            block,
            re.I,
        )
        if not url_match:
            continue

        norm_url = normalize_url(url_match.group(1))
        if norm_url in seen_in_page:
            continue

        anchor_texts = re.findall(
            r'<a\s+[^>]*href=["\'][^"\']*?/apro/[^"\']*?["\'][^>]*>(.*?)</a>',
            block,
            re.S | re.I,
        )
        cleaned_titles = [strip_tags(t) for t in anchor_texts if strip_tags(t)]
        if not cleaned_titles:
            continue
        title = max(cleaned_titles, key=len)

        price_match = re.search(
            r'class=["\'][^"\']*uad-price[^"\']*["\'][^>]*>(.*?)</div>',
            block,
            re.S | re.I,
        )
        price = strip_tags(price_match.group(1)) if price_match else ""

        time_match = re.search(
            r'class=["\'][^"\']*uad-time[^"\']*["\'][^>]*>(.*?)</div>',
            block,
            re.S | re.I,
        )
        posted_time = strip_tags(time_match.group(1)) if time_match else ""
        is_featured = bool(re.search(r"\bKiemelt\b", posted_time, re.I))

        seen_in_page.add(norm_url)
        results.append((norm_url, title, price, posted_time, is_featured))

    return results


def check_category(silent_baseline: bool = False) -> None:
    seen = load_seen()
    updated = False
    total_checked = 0
    new_emails = 0
    baseline_saved = 0

    for offset in OFFSETS:
        url = f"{CATEGORY_URL}?offset={offset}" if offset else CATEGORY_URL
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        listings = parse_listings(resp.text)
        total_checked += len(listings)

        for link, title, price, posted_time, is_featured in listings:
            if link in seen:
                continue

            seen.add(link)
            updated = True

            if silent_baseline:
                baseline_saved += 1
                continue

            if SKIP_FEATURED and is_featured:
                continue

            if TITLE_EXCLUDE_PATTERN.search(title):
                continue

            # 1. Ellenőrzés a hirdetés címében
            title_match = INCLUDE_PATTERN.search(title)
            gpu_num = title_match.group(1) if title_match else None

            # 2. Ha a címben nincs GPU típus (és más kártya sem), megnézi a hirdetés leírását
            if not gpu_num and not OTHER_GPU_PATTERN.search(title):
                try:
                    time.sleep(0.8)
                    gpu_num = description_has_target_gpu(link)
                except Exception:
                    gpu_num = None

            if not gpu_num:
                continue

            send_email(f"RTX {gpu_num}", title, price, posted_time, link)
            new_emails += 1
            time.sleep(1.5)

        time.sleep(1.5)

    now = datetime.now().strftime("%H:%M:%S")
    if silent_baseline:
        print(
            f"[{now}] Kezdeti szinkronizálás kész: {baseline_saved} meglévő hirdetés rögzítve (email nélkül). "
            f"Innentől csak az ÚJ RTX 4060 / 5050 / 5060 találatokról küldök értesítést!"
        )
    else:
        print(f"[{now}] Ellenőrizve: {total_checked} hirdetés, {new_emails} új találat.")

    if updated:
        save_seen(seen)


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_email(
            "RTX 4060",
            "Teszt értesítés - HardverApró figyelő",
            "265 000 Ft",
            "ma 12:15",
            "https://hardverapro.hu/aprok/notebook/pc/index.html",
        )
        sys.exit(0)

    print("HardverApró figyelés elindítva (RTX 4060 / 5050 / 5060)... Leállítás: Ctrl+C")
    is_first_run = not SEEN_FILE.exists()

    while True:
        try:
            check_category(silent_baseline=is_first_run)
            is_first_run = False
        except Exception as e:
            print(f"[HIBA] {e}")
        time.sleep(180)