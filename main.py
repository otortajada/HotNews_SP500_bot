import html
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

# --- Configuración (Secrets de GitHub) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# El SEC exige identificarse: "Nombre Apellido tu@email.com"
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT")
ENVIAR_PRUEBA = os.environ.get("ENVIAR_PRUEBA", "").lower() == "true"

FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
    "&company=&dateb=&owner=include&count={count}&start={start}&output=atom"
)
POR_PAGINA = 100  # máximo que admite el SEC
PAGINAS = 5       # hasta 500 filings hacia atrás (cubre varias horas)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SP500_FILE = Path("sp500.txt")
SEEN_FILE = Path("seen.json")
MAX_SEEN = 5000

# Puntos del 8-K que te interesan. Si lo dejas vacío (set()), recibes todos.
ITEMS_RELEVANTES = {
    "1.01": "Acuerdo material",
    "1.02": "Fin de acuerdo material",
    "1.03": "Quiebra / concurso",
    "2.01": "Adquisición o venta completada",
    "2.02": "Resultados financieros",
    "2.05": "Costes de reestructuración",
    "2.06": "Deterioro de activos",
    "4.01": "Cambio de auditor",
    "4.02": "Cuentas no fiables (reformulación)",
    "5.01": "Cambio de control",
}

TITLE_RE = re.compile(r"^(?P<form>\S+)\s+-\s+(?P<name>.+?)\s+\((?P<cik>\d{10})\)")
ITEM_RE = re.compile(r"Item\s+(\d\.\d{2})")
ACC_RE = re.compile(r"accession-number=([\d-]+)")


def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print(f"Telegram devolvió {r.status_code}: {r.text}")
            return False
        return True
    except requests.RequestException as e:
        print(f"Error enviando a Telegram: {e}")
        return False


class FeedNoDisponible(Exception):
    """El SEC no responde tras varios intentos (fallo temporal)."""


def get_con_reintentos(url, headers, intentos=3, timeout=(10, 45)):
    ultimo = None
    for n in range(1, intentos + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 403:
                # Esto no es temporal: el SEC rechaza el User-Agent
                sys.exit("El SEC devolvió 403: revisa SEC_USER_AGENT (nombre y email reales).")
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            ultimo = e
            print(f"Intento {n}/{intentos} fallido: {e}")
            if n < intentos:
                time.sleep(5 * n)
    raise FeedNoDisponible(str(ultimo))


def cargar_sp500():
    tickers = set()
    for linea in SP500_FILE.read_text(encoding="utf-8").splitlines():
        linea = linea.strip().upper()
        if linea and not linea.startswith("#"):
            tickers.add(linea.replace(".", "-"))  # BRK.B -> BRK-B (formato SEC)
    return tickers


def cargar_mapa_cik(headers):
    r = get_con_reintentos(TICKERS_URL, headers)
    mapa = {}
    for v in r.json().values():
        cik = str(v["cik_str"]).zfill(10)
        mapa.setdefault(cik, []).append(v["ticker"].upper())
    return mapa


def cargar_vistos():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return None  # None = primera ejecución


def guardar_vistos(vistos):
    SEEN_FILE.write_text(json.dumps(vistos[-MAX_SEEN:]), encoding="utf-8")


def id_filing(entrada):
    m = ACC_RE.search(entrada.get("id", ""))
    return m.group(1) if m else entrada.get("link", "")


def descargar_entradas(headers, vistos_set, primera_vez):
    """Lee páginas del feed del SEC hasta llegar a filings ya vistos."""
    entradas = []
    for pagina in range(PAGINAS):
        url = FEED_URL.format(count=POR_PAGINA, start=pagina * POR_PAGINA)
        try:
            r = get_con_reintentos(url, headers)
        except FeedNoDisponible as e:
            if pagina == 0:
                raise
            print(f"Aviso: falló la página {pagina + 1} del feed ({e}); sigo con lo leído.")
            break

        de_pagina = feedparser.parse(r.content).entries
        if not de_pagina:
            if pagina == 0:
                sys.exit("El feed del SEC no devolvió entradas.")
            break

        entradas.extend(de_pagina)
        # Si toda la página ya se conocía, no hace falta ir más atrás
        if not primera_vez and all(id_filing(e) in vistos_set for e in de_pagina):
            break
        time.sleep(0.3)  # el SEC limita las peticiones por segundo
    return entradas


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        sys.exit("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID.")
    if not SEC_USER_AGENT:
        sys.exit("Falta SEC_USER_AGENT (nombre y email reales, lo exige el SEC).")

    if ENVIAR_PRUEBA:
        ok = enviar_telegram("✅ Prueba del bot de alertas S&P 500: Telegram funciona.")
        print("Mensaje de prueba enviado." if ok else "Falló el mensaje de prueba.")

    headers = {"User-Agent": SEC_USER_AGENT}
    sp500 = cargar_sp500()
    mapa_cik = cargar_mapa_cik(headers)

    vistos = cargar_vistos()
    primera_vez = vistos is None
    vistos = vistos or []
    vistos_set = set(vistos)
    enviadas = 0

    entradas = descargar_entradas(headers, vistos_set, primera_vez)
    print(f"Filings leídos del feed: {len(entradas)}.")

    # De la más antigua a la más reciente, para que lleguen en orden
    for e in reversed(entradas):
        acc = id_filing(e)
        if not acc or acc in vistos_set:
            continue

        vistos.append(acc)
        vistos_set.add(acc)
        if primera_vez:
            continue  # primera ejecución: solo se memoriza, no se envía

        m = TITLE_RE.match(e.get("title", ""))
        if not m:
            continue
        candidatos = [t for t in mapa_cik.get(m["cik"], []) if t in sp500]
        if not candidatos:
            continue

        items = set(ITEM_RE.findall(e.get("summary", "")))
        relevantes = sorted(items & set(ITEMS_RELEVANTES))
        if ITEMS_RELEVANTES and not relevantes:
            continue
        motivos = relevantes if ITEMS_RELEVANTES else sorted(items)

        lineas = "\n".join(
            f"• {i}: {html.escape(ITEMS_RELEVANTES.get(i, 'Otro'))}" for i in motivos
        )
        mensaje = (
            f"🚨 <b>8-K · {html.escape(candidatos[0])}</b>\n"
            f"{html.escape(m['name'])}\n\n"
            f"{lineas}\n\n"
            f'🔗 <a href="{html.escape(e.get("link", ""), quote=True)}">Ver en el SEC</a>'
        )
        if enviar_telegram(mensaje):
            enviadas += 1
        else:
            # Fallo de envío: se desmarca para reintentarlo en la próxima ejecución
            vistos.remove(acc)
            vistos_set.discard(acc)

    guardar_vistos(vistos)

    if primera_vez:
        print(f"Primera ejecución: {len(vistos)} filings memorizados, sin enviar nada.")
    else:
        print(f"Escaneo completado. Alertas enviadas: {enviadas}.")


if __name__ == "__main__":
    try:
        main()
    except FeedNoDisponible as e:
        print(f"Aviso: el SEC no responde ({e}). Se reintentará en la próxima ejecución.")
