import html
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urljoin
from zoneinfo import ZoneInfo
from pathlib import Path

import feedparser
import requests

# --- Configuración (Secrets de GitHub) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# El SEC exige identificarse: "Nombre Apellido tu@email.com"
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT")
ALPACA_KEY = os.environ.get("ALPACA_KEY")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET")
ENVIAR_PRUEBA = os.environ.get("ENVIAR_PRUEBA", "").lower() == "true"

FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
    "&company=&dateb=&owner=include&count={count}&start={start}&output=atom"
)
POR_PAGINA = 100  # máximo que admite el SEC
PAGINAS = 5       # hasta 500 filings hacia atrás (cubre varias horas)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SP500_FILE = Path("sp500.txt")
EXTRA_FILE = Path("extra.txt")  # tus empresas adicionales (opcional)
FEEDS_EXTRA_FILE = Path("feeds_extra.txt")  # listados RSS de notas de prensa adicionales (opcional)
SEEN_FILE = Path("seen.json")
MAX_SEEN = 5000
INCLUIR_EXTRACTO = True   # añade un extracto en inglés del filing al aviso
EXTRACTO_MAX = 500        # caracteres por extracto
EXTRACTO_RESULTADOS_MAX = 1100  # más espacio para los resultados (2.02)
BUSCAR_NOTICIAS = True    # busca la nota de prensa en GlobeNewswire y PR Newswire
VENTANA_HORAS = 72        # cuánto antes del 8-K puede haberse publicado la noticia
ENLACE_WEBULL = True      # añade el enlace a la cotización en Webull
USAR_STOCKTITAN = True    # si no hay nota en las dos webs anteriores, prueba en StockTitan
USAR_ALPACA = True        # añade precio y variación reciente (datos IEX gratuitos de Alpaca)
PRECIOS_MINUTOS = (5, 15, 60)   # variaciones a mostrar
PRECIOS_VENTANA = 120     # minutos de velas de 1 minuto que se descargan
UMBRAL_SALTO = 2.0        # % mínimo de un movimiento de un minuto para avisar de un "salto"
ALPACA_URL = "https://data.alpaca.markets"
MADRID = ZoneInfo("Europe/Madrid")
NUEVA_YORK = ZoneInfo("America/New_York")
NOTICIAS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BOLSAS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
# --- Avisos con la nota de prensa (antes de que salga el 8-K) ---
AVISAR_CON_NOTA = True    # vigila las webs de notas de prensa además del SEC
FEEDS_NOTAS = [
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
    ("GlobeNewswire",
     "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
]
# Qué notas merecen aviso (se mira solo el titular). Se puede editar libremente.
CATEGORIAS_NOTA = {
    "Resultados financieros": re.compile(
        r"\b(results|earnings|quarter(?:ly)?|fiscal (?:year|20\d\d)|full[- ]year|Q[1-4])\b", re.I),
    "Adquisición / fusión": re.compile(
        r"\b(acquire[sd]?|acquiring|acquisition|merger|merge[sd]?|takeover|buyout|"
        r"to be acquired|divest\w*|spin-?off)\b", re.I),
    "Acuerdo material": re.compile(
        r"\b(definitive agreement|licen[cs](?:e|ing) agreement|collaboration agreement|"
        r"supply agreement|strategic agreement|enters? into (?:an? )?agreement)\b", re.I),
    "Previsiones": re.compile(
        r"\b(raises?|lowers?|cuts?|updates?|reaffirms?|withdraws?|initiates?)\b.{0,40}"
        r"\b(guidance|outlook|forecast)\b", re.I),
    "Quiebra": re.compile(r"\b(chapter 11|bankruptcy)\b", re.I),
    "Regulatorio / ensayos": re.compile(r"\b(FDA|topline|phase [123])\b", re.I),
}
# Anuncios de fechas, llamadas, conferencias… no son la noticia
EXCLUIR_NOTA_RE = re.compile(
    r"\b(to (?:report|announce|release|host|hold|present|participate|attend)|"
    r"will (?:report|announce|release|host|hold|present|participate)|schedules?|"
    r"date of|conference call|webcast|investor (?:day|conference)|fireside|upcoming)\b", re.I)
NOMBRES_SEC = {}  # ticker -> nombre de la empresa (se rellena al cargar el mapa del SEC)
DATELINE_RE = re.compile(
    r"/PRNewswire[^/]*/|\(GLOBE ?NEWSWIRE\)|\(BUSINESS WIRE\)|/BUSINESS WIRE/", re.I
)

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


def enviar_telegram(mensaje, responder_a=None):
    """Devuelve el id del mensaje enviado (o True si no se pudo leer) y False si falla."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if responder_a:
        payload["reply_to_message_id"] = responder_a
        payload["allow_sending_without_reply"] = True
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print(f"Telegram devolvió {r.status_code}: {r.text}")
            return False
        try:
            return r.json()["result"]["message_id"]
        except Exception:
            return True
    except requests.RequestException as e:
        print(f"Error enviando a Telegram: {e}")
        return False


class FeedNoDisponible(Exception):
    """El SEC no responde tras varios intentos (fallo temporal)."""


def get_con_reintentos(url, headers, intentos=3, timeout=(10, 45), exit_en_403=True):
    ultimo = None
    for n in range(1, intentos + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 403:
                if not exit_en_403:
                    raise FeedNoDisponible("403 Forbidden")
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


def cargar_lista(ruta):
    """Tickers de un archivo (uno por línea, # para comentarios). Si no existe, vacío."""
    tickers = set()
    if not ruta.exists():
        return tickers
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.split("#")[0].strip().upper()
        if linea:
            tickers.add(linea.replace(".", "-"))  # BRK.B -> BRK-B (formato SEC)
    return tickers


def cargar_feeds_extra():
    """Listados RSS adicionales de feeds_extra.txt, una línea 'Nombre | URL' por cada uno."""
    feeds = []
    if not FEEDS_EXTRA_FILE.exists():
        return feeds
    for linea in FEEDS_EXTRA_FILE.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "|" not in linea:
            continue
        nombre, url = (x.strip() for x in linea.split("|", 1))
        if url.startswith("http"):
            feeds.append((nombre, url))
    return feeds


def cargar_mapa_cik(headers):
    r = get_con_reintentos(TICKERS_URL, headers)
    mapa = {}
    for v in r.json().values():
        cik = str(v["cik_str"]).zfill(10)
        mapa.setdefault(cik, []).append(v["ticker"].upper())
        NOMBRES_SEC[v["ticker"].upper()] = v.get("title", "")
    return mapa


def cargar_estado():
    """Estado guardado entre ejecuciones. None si es la primera vez."""
    if not SEEN_FILE.exists():
        return None
    try:
        datos = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if isinstance(datos, list):  # formato antiguo: solo la lista de filings vistos
        datos = {"filings": datos}
    return datos


def _reciente(iso, horas, ahora=None):
    ahora = ahora or datetime.now(timezone.utc)
    return ahora - datetime.fromisoformat(iso) <= timedelta(hours=horas)


def guardar_estado(estado):
    estado["filings"] = estado["filings"][-MAX_SEEN:]
    estado["notas"] = estado["notas"][-MAX_SEEN:]
    for clave in ("avisos", "recientes"):  # solo interesan las últimas 72 horas
        estado[clave] = {t: v for t, v in estado[clave].items() if _reciente(v["hora"], 72)}
    SEEN_FILE.write_text(json.dumps(estado), encoding="utf-8")


BLOQUES_HTML = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}


class _ExtractorTexto(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes = []

    def handle_starttag(self, tag, attrs):
        if tag in BLOQUES_HTML:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in BLOQUES_HTML:
            self.partes.append("\n")
        elif tag in ("td", "th"):
            self.partes.append(" ")

    def handle_data(self, data):
        self.partes.append(data)


def html_a_texto(crudo):
    crudo = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", crudo)  # metadatos XBRL ocultos
    crudo = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", crudo)
    p = _ExtractorTexto()
    p.feed(crudo)
    p.close()
    texto = "".join(p.partes).replace("\xa0", " ")
    lineas = [re.sub(r"[ \t\r\f\v]+", " ", l).strip() for l in texto.split("\n")]
    return "\n".join(l for l in lineas if l)


def recortar(texto, maximo):
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) <= maximo:
        return texto
    corte = texto[:maximo]
    punto = max(corte.rfind(". "), corte.rfind("; "))
    if punto > maximo * 0.5:
        return corte[: punto + 1]
    return corte.rsplit(" ", 1)[0] + "…"


def extraer_seccion(texto, item):
    """Texto del punto del 8-K (p. ej. 1.01), sin el título."""
    m = re.search(rf"(?im)^\s*Item\s+{re.escape(item)}(?!\d)[\s.:\-–—]*", texto)
    if not m:
        return ""
    resto = texto[m.end():]
    fin = re.search(r"(?im)^\s*(Item\s+\d\.\d{2}(?!\d)|SIGNATURES?\b)", resto)
    if fin:
        resto = resto[: fin.start()]
    lineas = [l for l in resto.split("\n") if l.strip()]
    if len(lineas) > 1 and len(lineas[0]) < 100:
        lineas = lineas[1:]  # era el título del punto
    return " ".join(lineas)


MESES = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
FECHA_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})", re.I)


def fecha_dateline(texto):
    """Fecha que aparece en 'CIUDAD, Oct. 5, 2026 /PRNewswire/ --' (o None)."""
    for linea in texto.split("\n"):
        m = DATELINE_RE.search(linea[:250])
        if m:
            f = FECHA_RE.search(linea[: m.end()])
            if f:
                try:
                    return datetime(int(f.group(3)), MESES[f.group(1).lower()], int(f.group(2)),
                                    tzinfo=timezone.utc)
                except ValueError:
                    return None
            return None
    return None


MENCION_RE = re.compile(
    r"\b(NYSE|NASDAQ|Nasdaq|Cboe|CBOE)[A-Za-z ]{0,20}:\s*([A-Z]{1,5}(?:[.\-][A-Z])?)\b")


def tickers_mencionados(texto, excluir):
    """{ticker: bolsa} de empresas citadas como '(Nasdaq: PCRX)' en el cuerpo de una nota."""
    lineas = texto.split("\n")
    inicio = next((i for i, l in enumerate(lineas) if DATELINE_RE.search(l[:250])), 0)
    cuerpo = "\n".join(lineas[inicio:])[:6000]  # evita menús y listados laterales de la web
    excluir = {t.upper().replace(".", "-") for t in excluir}
    res = {}
    for bolsa, t in MENCION_RE.findall(cuerpo):
        t = t.replace(".", "-")
        if t.upper() not in excluir and t not in res:
            res[t] = bolsa.lower()
    return res


def resumen_desde_dateline(texto, maximo, con_titular=False):
    """Primer párrafo de una nota de prensa: arranca tras 'CIUDAD, fecha /PRNewswire/ --'."""
    lineas = texto.split("\n")
    for i, linea in enumerate(lineas):
        m = DATELINE_RE.search(linea[:250])
        if m:
            partes = []
            if con_titular and i > 0 and 40 <= len(lineas[i - 1]) <= 250:
                partes.append(lineas[i - 1])  # el titular suele ir justo encima
            partes.append(linea[m.end():].lstrip(" -–—"))
            partes.extend(lineas[i + 1:i + 4])
            return recortar(" ".join(partes), maximo)
    return ""


FRASE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“])")
METRICAS_RE = re.compile(
    r"\b(revenues?|net sales|sales|net (?:income|loss|earnings)|earnings per share|EPS|diluted|"
    r"EBITDA|operating (?:income|loss|cash flow)|cash flow|guidance|outlook|production|"
    r"dividends?|repurchases?|buybacks?|backlog|margins?|bookings|orders)\b", re.I)
CIFRAS_RE = re.compile(
    r"\$\s?\d|\d\s?%|\d\s?(?:million|billion)|\b\d[\d.,]*\s?(?:MMboe|Mboe|boe)", re.I)
RUIDO_RE = re.compile(
    r"forward-looking|safe harbor|webcast|dial[- ]in|access code|replay|"
    r"risks? and uncertaint|undue reliance|conference call", re.I)


def puntos_clave_resultados(texto, maximo=None):
    """Titular + frases con cifras (ingresos, beneficio, BPA, previsiones…) de un comunicado."""
    maximo = maximo or EXTRACTO_RESULTADOS_MAX
    lineas = texto.split("\n")

    titular = ""
    for i, linea in enumerate(lineas):
        if DATELINE_RE.search(linea[:250]):
            if i > 0 and 40 <= len(lineas[i - 1]) <= 250:
                titular = lineas[i - 1]
            break
    if not titular:
        titular = next((l for l in lineas if 40 <= len(l) <= 250 and not RUIDO_RE.search(l)), "")
    titular = recortar(titular, 220) if titular else ""

    puntos, vistos, total = [], set(), len(titular)
    lleno = False
    for linea in lineas:
        trozos = FRASE_RE.split(linea) if len(linea) > 300 else [linea]
        for t in trozos:
            t = t.strip()
            if len(t) < 20 or t in vistos or t == titular:
                continue
            if RUIDO_RE.search(t) or not METRICAS_RE.search(t) or not CIFRAS_RE.search(t):
                continue
            vistos.add(t)
            t = recortar(t, 260)
            if total + len(t) > maximo:
                lleno = True
                break
            puntos.append(t)
            total += len(t)
            if len(puntos) >= 6:
                lleno = True
                break
        if lleno:
            break

    if not puntos:
        return ""  # sin cifras reconocibles: que use el resumen normal
    return "\n".join(([titular] if titular else []) + ["• " + p for p in puntos])


def extracto_comunicado(texto):
    """Titular y primeros párrafos de un comunicado (Exhibit 99.1)."""
    desde = resumen_desde_dateline(texto, 2000, con_titular=True)
    if desde:
        return desde
    return " ".join(l for l in texto.split("\n") if len(l) >= 40)


def descargar_texto(url, headers):
    r = get_con_reintentos(url, headers, intentos=2, timeout=(10, 30), exit_en_403=False)
    try:
        crudo = r.content.decode("utf-8")
    except UnicodeDecodeError:
        crudo = r.content.decode("cp1252", errors="replace")
    return html_a_texto(crudo[:3_000_000])


def documentos_filing(url_indice, headers):
    """Devuelve (url del 8-K, url del comunicado EX-99) a partir de la página índice."""
    r = get_con_reintentos(url_indice, headers, intentos=2, timeout=(10, 30), exit_en_403=False)
    principal = exhibit = None
    for fila in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", r.text):
        celdas = re.findall(r"(?is)<td[^>]*>(.*?)</td>", fila)
        if len(celdas) < 4:
            continue
        href = re.search(r'(?i)href="([^"]+)"', celdas[2])
        if not href:
            continue
        tipo = re.sub(r"<[^>]+>", "", celdas[3]).strip().upper()
        url = href.group(1)
        if "doc=" in url:  # visor XBRL: /ix?doc=/Archives/...
            url = url.split("doc=", 1)[1].split("&")[0]
        url = urljoin("https://www.sec.gov", url)
        if tipo.startswith("8-K") and not principal:
            principal = url
        elif tipo.startswith("EX-99") and not exhibit:
            exhibit = url
    return principal, exhibit


def construir_extractos(entrada, items, headers, excluir=()):
    """({punto: extracto en inglés}, {ticker: bolsa}). Si algo falla, devuelve lo que haya."""
    extractos, mencionados = {}, {}
    try:
        principal, exhibit = documentos_filing(entrada.get("link", ""), headers)
        texto = descargar_texto(principal, headers) if principal else ""
        mencionados.update(tickers_mencionados(texto, excluir))
        for i in items:
            seccion = extraer_seccion(texto, i)
            if seccion:
                extractos[i] = recortar(seccion, EXTRACTO_MAX)

        if exhibit:  # el comunicado (Exhibit 99) trae los resultados y cita a otras empresas
            time.sleep(0.2)
            texto_ex = descargar_texto(exhibit, headers)
            for t, b in tickers_mencionados(texto_ex, excluir).items():
                mencionados.setdefault(t, b)
            if "2.02" in items or not extractos:
                comunicado = puntos_clave_resultados(texto_ex) if "2.02" in items else ""
                if not comunicado:
                    comunicado = recortar(extracto_comunicado(texto_ex), EXTRACTO_MAX)
                if comunicado:
                    extractos["2.02" if "2.02" in items else items[0]] = comunicado
    except Exception as e:  # el extracto nunca debe impedir el aviso
        print(f"No se pudo extraer el texto del filing: {e}")
    return extractos, mencionados


def nombre_para_buscar(nombre_sec):
    """'ALECTOR, INC.' -> 'Alector'."""
    n = re.sub(r"[^\w\s]", " ", nombre_sec)
    n = re.sub(
        r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|PLC|LLC|LP|HOLDINGS?|GROUP|THE|NV|SA|AG)\b",
        " ", n, flags=re.I,
    )
    return " ".join(n.split()).title()


def fecha_de(entrada):
    t = entrada.get("updated_parsed") or entrada.get("published_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def buscar_globenewswire(termino):
    url = f"https://www.globenewswire.com/RssFeed/keyword/{quote(termino)}"
    r = get_con_reintentos(url, {"User-Agent": NOTICIAS_UA}, intentos=2,
                           timeout=(10, 20), exit_en_403=False)
    resultados = []
    for en in feedparser.parse(r.content).entries[:15]:
        t = en.get("published_parsed")
        resultados.append({
            "fuente": "GlobeNewswire",
            "titulo": en.get("title", ""),
            "link": en.get("link", ""),
            "fecha": datetime(*t[:6], tzinfo=timezone.utc) if t else None,
        })
    return resultados


def buscar_prnewswire(termino):
    url = f"https://www.prnewswire.com/search/news/?keyword={quote(termino)}&page=1&pagesize=25"
    r = get_con_reintentos(url, {"User-Agent": NOTICIAS_UA}, intentos=2,
                           timeout=(10, 20), exit_en_403=False)
    resultados, vistos = [], set()
    for href, cuerpo in re.findall(
        r'(?is)href="(/news-releases/[^"#?]+\.html)"[^>]*>(.*?)</a>', r.text
    ):
        titulo = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cuerpo)).split())
        if href in vistos or len(titulo) < 20:
            continue
        vistos.add(href)
        resultados.append({
            "fuente": "PR Newswire",
            "titulo": titulo,
            "link": urljoin("https://www.prnewswire.com", href),
            "fecha": None,  # el listado no trae fecha fiable
        })
    return resultados[:15]


def buscar_stocktitan(ticker):
    """Noticias de un ticker en StockTitan (la más reciente primero)."""
    url = f"https://www.stocktitan.net/news/{quote(ticker.upper())}/"
    r = get_con_reintentos(url, {"User-Agent": NOTICIAS_UA}, intentos=2,
                           timeout=(10, 20), exit_en_403=False)
    resultados, vistos = [], set()
    patron = rf'(?is)href="(/news/{re.escape(ticker)}/[^"#?]+\.html)"[^>]*>(.*?)</a>'
    for href, cuerpo in re.findall(patron, r.text):
        titulo = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cuerpo)).split())
        if href in vistos or len(titulo) < 20:
            continue
        vistos.add(href)
        resultados.append({
            "fuente": "StockTitan",
            "titulo": titulo,
            "link": urljoin("https://www.stocktitan.net", href),
            "fecha": None,
            "por_ticker": True,  # ya es del ticker: no hace falta buscar el nombre en el título
        })
    return resultados[:8]


ABOGADOS_RE = re.compile(
    r"shareholder rights|investors? (?:alert|rights)|law (?:firm|offices?)|class action|"
    r"securities (?:fraud|class)|(?:investigat\w+|claims) on behalf of|"
    r"on behalf of (?:investors|shareholders|stockholders)|encourages? (?:investors|shareholders)|"
    r"\bLLP\b|Pomerantz|Rosen Law|Levi & Korsinsky|Bragar Eagel|Faruqi|Kessler Topaz|"
    r"Halper Sadeh|Johnson Fistel|Hagens Berman|Bronstein, Gewirtz|Glancy Prongay|Schall", re.I)


def elegir_noticia(candidatos, ticker, fecha_filing, resultados):
    """Abre los mejores candidatos y se queda con el que es de esa empresa y de esas fechas."""
    patron = rf"(?:NYSE|NASDAQ|CBOE)[^):]{{0,20}}:\s*{re.escape(ticker)}\b"
    for c in candidatos[:3]:
        try:
            texto = descargar_texto(c["link"], {"User-Agent": NOTICIAS_UA})
        except Exception as ex:
            print(f"  no se pudo abrir {c['link']}: {ex}")
            continue
        if not re.search(patron, texto, re.I):  # confirma que es de esa empresa
            continue
        if fecha_filing and not c["fecha"]:  # sin fecha en el listado: se mira la de la nota
            f = fecha_dateline(texto)
            if f:
                dias = (fecha_filing.date() - f.date()).days
                if dias > VENTANA_HORAS / 24 or dias < -1:
                    print(f"  descartada por fecha ({f.date()}): {c['link']}")
                    continue
        arranque = resumen_desde_dateline(texto, 700) or texto[:700]
        if ABOGADOS_RE.search(c["titulo"]) or ABOGADOS_RE.search(arranque):
            print(f"  descartada (aviso de un despacho de abogados): {c['link']}")
            continue
        resumen = puntos_clave_resultados(texto) if resultados else ""
        return {
            "fuente": c["fuente"],
            "link": c["link"],
            "resumen": resumen or resumen_desde_dateline(texto, EXTRACTO_MAX),
            "tickers": tickers_mencionados(texto, {ticker}),
            "fecha": c["fecha"],
        }
    return None


def buscar_noticia(ticker, nombre_sec, fecha_filing, resultados=False):
    """Busca la nota de prensa del 8-K. Devuelve dict o None."""
    termino = nombre_para_buscar(nombre_sec)
    clave = termino.split()[0].lower() if termino else ""

    etapas = []  # primero las webs de origen; StockTitan solo si no hay nada
    if termino:
        etapas.append([
            ("buscar_globenewswire", termino, lambda: buscar_globenewswire(termino)),
            ("buscar_prnewswire", termino, lambda: buscar_prnewswire(termino)),
        ])
    if USAR_STOCKTITAN:
        etapas.append([("buscar_stocktitan", ticker, lambda: buscar_stocktitan(ticker))])

    for etapa in etapas:
        encontrados = []
        for nombre, arg, buscar in etapa:
            try:
                res = buscar()
                print(f"  {nombre}('{arg}'): {len(res)} resultados")
                encontrados += res
            except Exception as ex:
                print(f"  {nombre}: no disponible ({ex})")

        candidatos = []
        for c in encontrados:
            if not c.get("por_ticker") and clave not in c["titulo"].lower():
                continue
            c["dist"] = 999.0
            if c["fecha"] and fecha_filing:
                horas = (fecha_filing - c["fecha"]).total_seconds() / 3600
                if horas > VENTANA_HORAS or horas < -2:
                    continue
                c["dist"] = abs(horas)
            candidatos.append(c)
        candidatos.sort(key=lambda c: c["dist"])

        elegido = elegir_noticia(candidatos, ticker, fecha_filing, resultados)
        if elegido:
            return elegido
    return None


def _hora(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _pct(actual, base):
    return (actual / base - 1) * 100 if actual and base else None


def obtener_precios(tickers, hora_nota=None, ahora=None):
    """{ticker: datos} con precio y variación reciente (Alpaca, feed IEX). Vacío si falla."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        print("  precios: faltan ALPACA_KEY / ALPACA_SECRET")
        return {}
    ahora = ahora or datetime.now(timezone.utc)
    simbolos = {t: t.replace("-", ".") for t in tickers}  # BRK-B -> BRK.B
    cab = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    inicio = (ahora - timedelta(minutes=PRECIOS_VENTANA)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lista = ",".join(simbolos.values())
    try:
        snaps = get_con_reintentos(
            f"{ALPACA_URL}/v2/stocks/snapshots?" + urlencode({"symbols": lista, "feed": "iex"}),
            cab, intentos=2, timeout=(10, 20), exit_en_403=False).json()
        barras = get_con_reintentos(
            f"{ALPACA_URL}/v2/stocks/bars?" + urlencode({
                "symbols": lista, "timeframe": "1Min", "start": inicio, "feed": "iex",
                "limit": 10000, "adjustment": "raw", "sort": "asc"}),
            cab, intentos=2, timeout=(10, 20), exit_en_403=False).json()
    except Exception as ex:
        print(f"  precios: no se pudieron obtener ({ex})")
        return {}
    snaps = snaps.get("snapshots", snaps)
    barras = barras.get("bars") or {}
    hoy_ny = ahora.astimezone(NUEVA_YORK).date()

    resultado = {}
    for t, simbolo in simbolos.items():
        sn = snaps.get(simbolo) or {}
        velas = [(_hora(b["t"]), b["c"]) for b in (barras.get(simbolo) or [])]

        ultimo = sn.get("latestTrade") or {}
        precio = None
        if ultimo.get("p") and ahora - _hora(ultimo["t"]) <= timedelta(minutes=30):
            precio = ultimo["p"]
        elif velas and ahora - velas[-1][0] <= timedelta(minutes=30):
            precio = velas[-1][1]
        if precio is None:
            print(f"  precios: {t} sin operaciones recientes en IEX")
            continue

        # Cierre anterior: si aún no hay vela diaria de hoy, la última vela diaria es la de ayer
        hoy_vela, ayer_vela = sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}
        if hoy_vela and _hora(hoy_vela["t"]).astimezone(NUEVA_YORK).date() == hoy_ny:
            cierre_prev = ayer_vela.get("c")
        else:
            cierre_prev = hoy_vela.get("c")

        def precio_hace(minutos=None, desde=None):
            limite = desde or (ahora - timedelta(minutes=minutos))
            previos = [c for h, c in velas if h <= limite]
            return previos[-1] if previos else None

        datos = {
            "precio": precio,
            "v_cierre": _pct(precio, cierre_prev),
            "v": {n: _pct(precio, precio_hace(n)) for n in PRECIOS_MINUTOS},
            "desde_nota": None, "hora_nota": None, "salto": None, "hora_salto": None,
        }
        if hora_nota and ahora - hora_nota <= timedelta(minutes=PRECIOS_VENTANA):
            datos["desde_nota"] = _pct(precio, precio_hace(desde=hora_nota))
            datos["hora_nota"] = hora_nota.astimezone(MADRID).strftime("%H:%M")

        mayor, hora_mayor = 0.0, None  # mayor movimiento entre dos velas seguidas
        for (_, c0), (h1, c1) in zip(velas, velas[1:]):
            cambio = _pct(c1, c0)
            if cambio is not None and abs(cambio) > abs(mayor):
                mayor, hora_mayor = cambio, h1
        if hora_mayor and abs(mayor) >= UMBRAL_SALTO:
            datos["salto"] = mayor
            datos["hora_salto"] = hora_mayor.astimezone(MADRID).strftime("%H:%M")
        resultado[t] = datos
    return resultado


def linea_precio(ticker, d):
    partes = [f"${d['precio']:.2f}"]
    if d["v_cierre"] is not None:
        partes.append(f"vs cierre {d['v_cierre']:+.1f}%")
    for n in PRECIOS_MINUTOS:
        if d["v"].get(n) is not None:
            partes.append(f"{n} min {d['v'][n]:+.1f}%")
    if d["desde_nota"] is not None:
        partes.append(f"desde la nota ({d['hora_nota']}) {d['desde_nota']:+.1f}%")
    if d["salto"] is not None:
        partes.append(f"salto {d['salto']:+.1f}% a las {d['hora_salto']}")
    return f"{ticker}  " + " · ".join(partes)


def cargar_bolsas(headers):
    """{ticker: bolsa} desde el SEC. Si falla, no habrá enlace a Webull."""
    try:
        r = get_con_reintentos(BOLSAS_URL, headers, intentos=2, timeout=(10, 30),
                               exit_en_403=False)
        d = r.json()
        i_t, i_b = d["fields"].index("ticker"), d["fields"].index("exchange")
        return {f[i_t].upper(): (f[i_b] or "") for f in d["data"]}
    except Exception as ex:
        print(f"No se pudo cargar la bolsa de cada ticker: {ex}")
        return {}


def enlace_webull(ticker, bolsas):
    bolsa = (bolsas.get(ticker.upper()) or "").lower()
    if bolsa not in ("nasdaq", "nyse"):
        return None
    return f"https://www.webull.com/quote/{bolsa}-{ticker.lower()}"


def _titulo(tipo, ticker, nombre):
    return f"<b>{html.escape(tipo)} - {html.escape(ticker)} - {html.escape(nombre)}</b>"


def _bloque_precios(precios):
    if not precios:
        return ""
    return "📊 <b>Cotización (IEX, hora de Madrid)</b>\n" + "\n".join(html.escape(l) for l in precios)


def _otras_empresas(mencionados):
    if not mencionados:
        return ""
    partes = [
        f'<a href="{html.escape(u, quote=True)}">{html.escape(t)}</a>' if u else html.escape(t)
        for t, u in mencionados
    ]
    return "👀 También menciona: " + " · ".join(partes)


def _tipo(item):
    """'1.01: ACUERDO MATERIAL' en negrita."""
    return f"<b>{html.escape(item)}: {html.escape(ITEMS_RELEVANTES.get(item, 'Otro').upper())}</b>"


def _ensamblar(cabecera, precios, noticia, mencionados, enlaces):
    """Orden del aviso: título, cotización, noticia, otras empresas y enlaces."""
    bloques = [cabecera, _bloque_precios(precios), noticia, _otras_empresas(mencionados),
               "\n".join(enlaces)]
    return "\n\n".join(b for b in bloques if b)


def armar_mensaje(ticker, nombre, motivos, extractos, link, noticia=None, webull=None,
                  mencionados=(), precios=(), etiqueta=""):
    """Aviso disparado por un 8-K."""
    con_resumen_noticia = bool(noticia and noticia.get("resumen"))
    bloques = []
    for i in motivos:
        bloque = _tipo(i)
        if i in extractos and not con_resumen_noticia:
            bloque += f"\n<i>{html.escape(extractos[i])}</i>"
        bloques.append(bloque)
    cuerpo = "\n\n".join(bloques)
    if con_resumen_noticia:
        cuerpo = "\n".join(bloques) + f"\n\n<i>{html.escape(noticia['resumen'])}</i>"

    enlaces = []
    if noticia:
        enlaces.append(
            f'📰 <a href="{html.escape(noticia["link"], quote=True)}">'
            f'Noticia ({html.escape(noticia["fuente"])})</a>'
        )
    enlaces.append(f'🔗 <a href="{html.escape(link, quote=True)}">Comunicado SEC</a>')
    if webull:
        enlaces.append(f'📈 <a href="{html.escape(webull, quote=True)}">Cotización en Webull</a>')

    cabecera = "🚨 " + _titulo("8-K", ticker, nombre)
    if etiqueta:
        cabecera += f"\n{html.escape(etiqueta)}"
    return _ensamblar(cabecera, precios, cuerpo, mencionados, enlaces)


def armar_mensaje_nota(ticker, nombre, categoria, etiqueta, resumen, nota_link, fuente,
                       link_8k=None, webull=None, mencionados=(), precios=()):
    """Aviso disparado por una nota de prensa."""
    cuerpo = f"<b>{html.escape(categoria.upper())}</b>"
    if resumen:
        cuerpo += f"\n<i>{html.escape(resumen)}</i>"
    enlaces = [f'📰 <a href="{html.escape(nota_link, quote=True)}">Noticia ({html.escape(fuente)})</a>']
    if link_8k:
        enlaces.append(f'🔗 <a href="{html.escape(link_8k, quote=True)}">Comunicado SEC</a>')
    if webull:
        enlaces.append(f'📈 <a href="{html.escape(webull, quote=True)}">Cotización en Webull</a>')
    cabecera = "🚨 " + _titulo("Nota de prensa", ticker, nombre) + f"\n{html.escape(etiqueta)}"
    return _ensamblar(cabecera, precios, cuerpo, mencionados, enlaces)


def armar_seguimiento_8k(ticker, nombre, motivos, extractos, link):
    """Respuesta corta cuando llega el 8-K de una noticia que ya se avisó con la nota."""
    bloques = []
    for i in motivos:
        bloque = _tipo(i)
        if i in extractos:
            bloque += f"\n<i>{html.escape(recortar(extractos[i], 300))}</i>"
        bloques.append(bloque)
    cabecera = "📄 " + _titulo("8-K publicado", ticker, nombre) + "\n🏷 Nota de prensa + 8-K"
    enlaces = [f'🔗 <a href="{html.escape(link, quote=True)}">Comunicado SEC</a>']
    return _ensamblar(cabecera, (), "\n\n".join(bloques), (), enlaces)


def quitar_html(texto):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", texto or "")).split())


def categoria_nota(titular):
    """Categoría de la noticia según su titular, o None si no merece aviso."""
    if EXCLUIR_NOTA_RE.search(titular):
        return None
    for nombre, patron in CATEGORIAS_NOTA.items():
        if patron.search(titular):
            return nombre
    return None


def vigilados_en_nota(entrada, vigilados, claves_nombre):
    """({ticker: bolsa} citados como '(Nasdaq: X)', [tickers que solo coinciden por el nombre])."""
    titular = entrada.get("title", "")
    etiquetas = " | ".join(
        t.get("term", "") for t in (entrada.get("tags") or []) if isinstance(t, dict))
    texto = f"{titular} | {quitar_html(entrada.get('summary', ''))} | {etiquetas}"
    con_patron = {}
    for bolsa, t in MENCION_RE.findall(texto):
        t = t.replace(".", "-")
        if t in vigilados:
            con_patron.setdefault(t, bolsa.lower())
    minus = titular.lower()
    por_nombre = [
        t for t, clave in claves_nombre.items()
        if t not in con_patron and (minus.startswith(clave + " ") or minus.startswith(clave + ","))
    ]
    return con_patron, por_nombre


def extras_aviso(ctx, ticker, mencionados, hora_nota):
    """(enlace Webull, [(otra empresa, enlace)], [líneas de cotización]) de un aviso."""
    if (ENLACE_WEBULL or mencionados) and ctx["bolsas"] is None:
        ctx["bolsas"] = cargar_bolsas(ctx["headers"])
    bolsas = ctx["bolsas"] or {}
    webull = enlace_webull(ticker, bolsas) if ENLACE_WEBULL else None

    otras = []
    for t, bolsa_texto in list(mencionados.items())[:4]:
        url = enlace_webull(t, bolsas) if ENLACE_WEBULL else None
        if not url and ENLACE_WEBULL and bolsa_texto in ("nasdaq", "nyse"):
            url = f"https://www.webull.com/quote/{bolsa_texto}-{t.lower()}"
        otras.append((t, url))
    if otras:
        print("  también menciona: " + ", ".join(t for t, _ in otras))

    precios = []
    if USAR_ALPACA:
        lista = [ticker] + [t for t, _ in otras]
        datos = obtener_precios(lista, hora_nota=hora_nota)
        precios = [linea_precio(t, datos[t]) for t in lista if t in datos]
    return webull, otras, precios


def _id_mensaje(resultado):
    return resultado if isinstance(resultado, int) and not isinstance(resultado, bool) else None


def tramitar_8k(ctx, e, nombre, ticker, motivos, acc):
    """Aviso (o seguimiento) de un 8-K relevante. True si se envió."""
    headers, estado = ctx["headers"], ctx["estado"]
    link = e.get("link", "")
    ahora_iso = datetime.now(timezone.utc).isoformat()
    nombre_buscar = nombre
    nombre = NOMBRES_SEC.get(ticker) or nombre  # el de la lista del SEC trae bien las minúsculas
    estado["recientes"][ticker] = {"hora": ahora_iso, "items": motivos, "link": link}

    noticia = None
    if BUSCAR_NOTICIAS:
        try:
            noticia = buscar_noticia(ticker, nombre_buscar, fecha_de(e), resultados=("2.02" in motivos))
        except Exception as ex:  # la búsqueda nunca debe impedir el aviso
            print(f"  búsqueda de noticia fallida: {ex}")
        print(f"  noticia relacionada: {noticia['link'] if noticia else 'ninguna'}")

    # ¿Ya se avisó de esta noticia con la nota de prensa? Entonces solo se responde a ese aviso.
    previo = estado["avisos"].get(ticker)
    mismo_suceso = bool(
        previo and previo.get("tipo") == "nota" and not previo.get("acc")
        and _reciente(previo["hora"], 24)
        and not (noticia and previo.get("link") and noticia["link"] != previo["link"])
    )
    if mismo_suceso:
        extractos = {}
        if INCLUIR_EXTRACTO:
            extractos, _ = construir_extractos(e, motivos[:3], headers, {ticker})
        mensaje = armar_seguimiento_8k(ticker, nombre, motivos, extractos, link)
        if enviar_telegram(mensaje, responder_a=previo.get("msg")):
            previo["acc"] = acc
            print(f"  seguimiento del 8-K enviado como respuesta al aviso de la nota ({ticker})")
            return True
        return False

    # Sin resumen de la noticia, se usa el extracto del propio filing
    extractos, mencionados = {}, {}
    if noticia:
        mencionados.update(noticia.get("tickers", {}))
    if INCLUIR_EXTRACTO and not (noticia and noticia["resumen"]):
        extractos, del_filing = construir_extractos(e, motivos[:3], headers, {ticker})
        for t, b in del_filing.items():
            mencionados.setdefault(t, b)

    webull, otras, precios = extras_aviso(ctx, ticker, mencionados, (noticia or {}).get("fecha"))
    etiqueta = ("🏷 Nota de prensa + 8-K" if noticia
                else "🏷 Solo 8-K (no encuentro nota de prensa)")
    mensaje = armar_mensaje(ticker, nombre, motivos, extractos, link, noticia, webull,
                            otras, precios, etiqueta)
    if len(mensaje) > 4000:  # límite de Telegram: 4096
        mensaje = armar_mensaje(ticker, nombre, motivos, {}, link, noticia, webull,
                                otras, precios, etiqueta)
    resultado = enviar_telegram(mensaje)
    if not resultado:
        return False
    estado["avisos"][ticker] = {
        "msg": _id_mensaje(resultado), "hora": ahora_iso, "tipo": "8k", "acc": acc,
        "cat": "", "link": noticia["link"] if noticia else None,
    }
    if noticia:  # esta nota ya está avisada: que no salte de nuevo por la vía de las notas
        estado["notas"].append(noticia["link"])
    return True


def procesar_nota(ctx, fuente, en, claves_nombre):
    """Decide si una nota de prensa merece aviso. Devuelve su resultado como texto."""
    estado = ctx["estado"]
    titular = " ".join(en.get("title", "").split())
    link = en.get("link", "")
    resumen_rss = quitar_html(en.get("summary", ""))

    con_patron, por_nombre = vigilados_en_nota(en, ctx["vigilados"], claves_nombre)
    if not con_patron and not por_nombre:
        return "ajena"
    categoria = categoria_nota(titular)
    if not categoria:
        print(f"  nota sin categoría relevante ({', '.join(list(con_patron) + por_nombre)}): {titular[:90]}")
        return "no_relevante"
    if ABOGADOS_RE.search(titular + " " + resumen_rss):
        return "ruido"

    texto = ""
    try:
        texto = descargar_texto(link, {"User-Agent": NOTICIAS_UA})
    except Exception as ex:
        print(f"  no se pudo abrir la nota {link}: {ex}")
    elegidos = dict(con_patron)
    for t in por_nombre:  # coincidencia solo por nombre: se confirma con "(Nasdaq: X)" en la nota
        if texto and re.search(rf"(?:NYSE|NASDAQ|CBOE)[^):]{{0,20}}:\s*{re.escape(t)}\b", texto, re.I):
            elegidos.setdefault(t, "")
    if not elegidos:
        return "ajena"
    ticker = next(iter(elegidos))

    arranque = (resumen_desde_dateline(texto, 700) or texto[:700]) if texto else ""
    if ABOGADOS_RE.search(arranque):
        return "ruido"

    previo = estado["avisos"].get(ticker)
    if previo and (previo.get("link") == link or (
            previo.get("tipo") == "nota" and previo.get("cat") == categoria
            and _reciente(previo["hora"], 3))):
        return "duplicada"

    resumen = ""
    if texto:
        if categoria == "Resultados financieros":
            resumen = puntos_clave_resultados(texto)
        resumen = resumen or resumen_desde_dateline(texto, EXTRACTO_MAX)
    resumen = resumen or recortar(resumen_rss, EXTRACTO_MAX)
    mencionados = tickers_mencionados(texto, {ticker}) if texto else {}

    hora_nota = None
    if en.get("published_parsed"):
        hora_nota = datetime(*en["published_parsed"][:6], tzinfo=timezone.utc)
    webull, otras, precios = extras_aviso(ctx, ticker, mencionados, hora_nota)

    reciente_8k = estado["recientes"].get(ticker)
    tiene_8k = bool(reciente_8k and _reciente(reciente_8k["hora"], 24))
    etiqueta = ("🏷 Nota de prensa + 8-K" if tiene_8k
                else "🏷 Solo nota de prensa (8-K aún no publicado)")
    nombre = NOMBRES_SEC.get(ticker, ticker)
    link_8k = reciente_8k["link"] if tiene_8k else None
    mensaje = armar_mensaje_nota(ticker, nombre, categoria, etiqueta, resumen, link, fuente,
                                 link_8k, webull, otras, precios)
    if len(mensaje) > 4000:
        mensaje = armar_mensaje_nota(ticker, nombre, categoria, etiqueta, recortar(resumen, 300),
                                     link, fuente, link_8k, webull, otras, precios)
    resultado = enviar_telegram(mensaje)
    if not resultado:
        return "fallo"
    estado["avisos"][ticker] = {
        "msg": _id_mensaje(resultado), "hora": datetime.now(timezone.utc).isoformat(),
        "tipo": "nota", "acc": "previo" if tiene_8k else None, "cat": categoria, "link": link,
    }
    return "enviada"


def revisar_notas(ctx, primera_vez):
    """Lee los feeds de notas de prensa y avisa de las que afectan a tu lista."""
    estado = ctx["estado"]
    claves_nombre = {}
    for t in ctx["vigilados"]:
        clave = nombre_para_buscar(NOMBRES_SEC.get(t, "")).lower()
        if len(clave) >= 4:
            claves_nombre[t] = clave

    vistas = set(estado["notas"])
    cuentas = {"leidas": 0, "nuevas": 0}
    por_procesar = []
    for fuente, url in FEEDS_NOTAS + cargar_feeds_extra():
        try:
            r = get_con_reintentos(url, {"User-Agent": NOTICIAS_UA}, intentos=2,
                                   timeout=(10, 20), exit_en_403=False)
            entradas = feedparser.parse(r.content).entries
        except Exception as ex:
            print(f"  notas {fuente}: no disponible ({ex})")
            continue
        print(f"  notas {fuente}: {len(entradas)} entradas")
        cuentas["leidas"] += len(entradas)
        for en in entradas:
            link = en.get("link", "")
            if not link or link in vistas:
                continue
            vistas.add(link)
            estado["notas"].append(link)
            cuentas["nuevas"] += 1
            if not primera_vez:
                por_procesar.append((fuente, en))

    if primera_vez:
        print(f"Notas: primera lectura, {cuentas['nuevas']} memorizadas sin avisar.")
        return
    for fuente, en in reversed(por_procesar):  # de la más antigua a la más reciente
        try:
            resultado = procesar_nota(ctx, fuente, en, claves_nombre)
        except Exception as ex:  # una nota rara no debe romper el resto
            print(f"  error procesando una nota: {ex}")
            resultado = "error"
        cuentas[resultado] = cuentas.get(resultado, 0) + 1
        if resultado == "fallo":  # no se pudo enviar: se reintenta en la próxima ejecución
            estado["notas"].remove(en.get("link", ""))
    detalle = " · ".join(f"{k} {v}" for k, v in cuentas.items() if k not in ("leidas", "nuevas"))
    print(f"Notas: {cuentas['leidas']} leídas · {cuentas['nuevas']} nuevas"
          + (f" · {detalle}" if detalle else ""))


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
    sp500 = cargar_lista(SP500_FILE)
    extra = cargar_lista(EXTRA_FILE)
    vigilados = sp500 | extra
    mapa_cik = cargar_mapa_cik(headers)
    con_cik = {t for ts in mapa_cik.values() for t in ts}
    sin_cik = sorted(vigilados - con_cik)
    print(f"Lista: {len(sp500)} del S&P 500 + {len(extra)} extra; "
          f"{len(vigilados) - len(sin_cik)} encontrados en el SEC."
          + (f" Sin datos: {', '.join(sin_cik[:15])}" if sin_cik else ""))

    estado = cargar_estado()
    primera_vez = estado is None
    estado = estado or {}
    primera_vez_notas = "notas" not in estado  # también al actualizar desde una versión antigua
    estado.setdefault("filings", [])
    estado.setdefault("notas", [])
    estado.setdefault("avisos", {})
    estado.setdefault("recientes", {})
    vistos = estado["filings"]
    vistos_set = set(vistos)
    ctx = {"headers": headers, "bolsas": None, "estado": estado, "vigilados": vigilados}
    enviadas = nuevos = sin_formato = del_sp500 = a_avisar = 0
    otros = []

    try:
        entradas = descargar_entradas(headers, vistos_set, primera_vez)
    except FeedNoDisponible as e:
        print(f"Aviso: el SEC no responde ({e}). Se reintentará en la próxima ejecución.")
        entradas = []
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
        nuevos += 1

        m = TITLE_RE.match(e.get("title", ""))
        if not m:
            sin_formato += 1
            continue
        candidatos = [t for t in mapa_cik.get(m["cik"], []) if t in vigilados]
        if not candidatos:
            if len(otros) < 12:
                otros.append(m["name"][:28])
            continue
        del_sp500 += 1

        items = set(ITEM_RE.findall(e.get("summary", "")))
        relevantes = sorted(items & set(ITEMS_RELEVANTES))
        avisar = bool(relevantes) or not ITEMS_RELEVANTES
        print(f"  {candidatos[0]}: puntos {sorted(items) or 'sin detectar'} -> "
              f"{'AVISO' if avisar else 'descartado'}")
        if not avisar:
            continue
        a_avisar += 1
        motivos = relevantes if ITEMS_RELEVANTES else sorted(items)

        if tramitar_8k(ctx, e, m["name"], candidatos[0], motivos, acc):
            enviadas += 1
        else:
            # Fallo de envío: se desmarca para reintentarlo en la próxima ejecución
            vistos.remove(acc)
            vistos_set.discard(acc)

    if AVISAR_CON_NOTA:
        revisar_notas(ctx, primera_vez_notas)

    guardar_estado(estado)

    if primera_vez:
        print(f"Primera ejecución: {len(vistos)} filings memorizados, sin enviar nada.")
    else:
        print(
            f"Resumen: {nuevos} filings nuevos · {sin_formato} con título no reconocido · "
            f"{del_sp500} de tu lista · {a_avisar} con punto relevante · "
            f"{enviadas} avisos enviados."
        )
        if otros:
            print("  Fuera de tu lista: " + " | ".join(otros))


if __name__ == "__main__":
    try:
        main()
    except FeedNoDisponible as e:
        print(f"Aviso: el SEC no responde ({e}). Se reintentará en la próxima ejecución.")
