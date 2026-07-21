"""
Monitor de rebajas de Walmart Costa Rica (walmart.co.cr)
----------------------------------------------------------
Walmart CR usa la plataforma VTEX, que expone una API pública de búsqueda
de productos en formato JSON (mucho más liviana y confiable que raspar HTML).

Revisa las secciones "Rebajas" y "Rebajas exclusivas", detecta productos con
descuento > MIN_DISCOUNT % y envía un correo cuando aparece una oferta NUEVA
(no repite alertas de productos ya notificados mientras sigan en oferta).
"""

import requests
import json
import os
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ----------------------------- Configuración ------------------------------

BASE_URL = "https://www.walmart.co.cr/api/catalog_system/pub/products/search"

# Secciones a revisar (usan la misma ruta que la página web, con map=c para
# indicarle a VTEX que es una navegación por categoría)
CATEGORIES = ["rebajas", "exclusivas"]

PAGE_SIZE = 50            # máximo permitido por la API de VTEX en cada request
MIN_DISCOUNT = 76         # % mínimo de descuento para alertar ("mayor a 75%")
STATE_FILE = "state.json"
REQUEST_DELAY = 1.5       # segundos entre requests
MAX_PAGES_PER_CATEGORY = 30   # tope de seguridad (30 * 50 = 1500 productos por categoría)
TIMEOUT = 30
MAX_RETRIES = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "es-CR,es;q=0.9,en;q=0.8",
    "Referer": "https://www.walmart.co.cr/",
}

session = requests.Session()
session.headers.update(HEADERS)

# ------------------------------- Utilidades --------------------------------


def fetch_page(category, offset):
    url = f"{BASE_URL}/{category}"
    params = {"map": "c", "_from": offset, "_to": offset + PAGE_SIZE - 1}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            wait = 5 * attempt
            print(f"[warn] Fallo de red en {category} offset={offset} (intento {attempt}): {e}. Esperando {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 8 * attempt
            print(f"[warn] 429 en {category} offset={offset} (intento {attempt}). Esperando {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            wait = 5 * attempt
            print(f"[warn] Error {resp.status_code} en {category} offset={offset} (intento {attempt}). Esperando {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code == 206 or resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                print(f"[warn] Respuesta no-JSON en {category} offset={offset}")
                return []
            return data

        print(f"[warn] Status inesperado {resp.status_code} en {category} offset={offset}")
        return []

    print(f"[warn] Se omite {category} offset={offset} tras varios intentos")
    return []


def extract_deals_from_products(products):
    deals = []
    for prod in products:
        name = prod.get("productName", "").strip()
        link = prod.get("link") or prod.get("linkText")
        items = prod.get("items") or []
        if not items:
            continue

        best = None
        for item in items:
            sellers = item.get("sellers") or []
            for seller in sellers:
                offer = seller.get("commertialOffer") or {}
                price = offer.get("Price")
                list_price = offer.get("ListPrice")
                available = offer.get("AvailableQuantity", 0)
                if not price or not list_price or list_price <= price:
                    continue
                if available is not None and available <= 0:
                    continue
                discount = round((1 - price / list_price) * 100)
                if best is None or discount > best["discount"]:
                    best = {"price": price, "list_price": list_price, "discount": discount}

        if best is None:
            continue

        deals.append({
            "id": prod.get("productId") or link,
            "name": name,
            "url": link,
            "special_price": round(best["price"]),
            "regular_price": round(best["list_price"]),
            "discount": best["discount"],
        })

    return deals


def scrape_category(category):
    all_deals = {}
    offset = 0
    for page in range(MAX_PAGES_PER_CATEGORY):
        products = fetch_page(category, offset)
        if not products:
            break

        deals = extract_deals_from_products(products)
        for d in deals:
            all_deals[d["id"]] = d

        print(f"[info] {category}: pagina {page + 1} ({offset}-{offset + PAGE_SIZE - 1}) -> {len(products)} productos, {len(deals)} con oferta")

        if len(products) < PAGE_SIZE:
            break  # ultima pagina

        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY)

    return list(all_deals.values())


def scrape_all_deals():
    all_deals = {}
    for category in CATEGORIES:
        print(f"[info] Revisando categoria: {category}")
        for d in scrape_category(category):
            all_deals[d["id"]] = d
    return list(all_deals.values())


# --------------------------------- Estado -----------------------------------


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"alerted_ids": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------- Correo -----------------------------------


def send_email(new_deals):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("ALERT_EMAIL", "martinez96jason@gmail.com")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔥 {len(new_deals)} nueva(s) rebaja(s) +{MIN_DISCOUNT - 1}% en Walmart CR"
    msg["From"] = gmail_user
    msg["To"] = recipient

    text_lines = []
    html_items = []
    for p in sorted(new_deals, key=lambda x: -x["discount"]):
        text_lines.append(
            f"- {p['name']}\n"
            f"  {p['discount']}% OFF -> ₡{p['special_price']:,} (antes ₡{p['regular_price']:,})\n"
            f"  {p['url']}\n"
        )
        html_items.append(
            f"<li style='margin-bottom:14px'>"
            f"<b>{p['name']}</b><br>"
            f"<span style='color:#0071ce;font-weight:bold'>{p['discount']}% OFF</span> "
            f"&mdash; ₡{p['special_price']:,} <span style='text-decoration:line-through;color:#888'>"
            f"₡{p['regular_price']:,}</span><br>"
            f"<a href='{p['url']}'>Ver producto</a></li>"
        )

    text_body = f"Nuevas rebajas de mas de {MIN_DISCOUNT - 1}% en Walmart CR:\n\n" + "\n".join(text_lines)
    html_body = (
        f"<h2>🔥 Nuevas rebajas de más de {MIN_DISCOUNT - 1}% en Walmart CR</h2>"
        f"<ul style='list-style:none;padding:0'>{''.join(html_items)}</ul>"
    )

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, recipient, msg.as_string())


# ---------------------------------- Main -------------------------------------


def main():
    state = load_state()
    alerted = set(state.get("alerted_ids", []))

    products = scrape_all_deals()
    print(f"[info] Productos con oferta escaneados: {len(products)}")

    deals = [p for p in products if p["discount"] >= MIN_DISCOUNT]
    print(f"[info] Ofertas >= {MIN_DISCOUNT}%: {len(deals)}")

    new_deals = [p for p in deals if p["id"] not in alerted]

    if new_deals:
        print(f"[info] Enviando correo por {len(new_deals)} oferta(s) nueva(s)")
        send_email(new_deals)
    else:
        print("[info] No hay ofertas nuevas que alertar")

    current_deal_ids = {p["id"] for p in deals}
    alerted = (alerted & current_deal_ids) | {p["id"] for p in new_deals}

    state["alerted_ids"] = sorted(str(i) for i in alerted)
    save_state(state)


if __name__ == "__main__":
    main()
