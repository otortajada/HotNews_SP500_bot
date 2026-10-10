import html
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from datetime import datetime, timezone
from urllib.parse import quote, urljoin
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
EXTRA_FILE = Path("extra.txt")  # tus empresas adicionales (opcional)
SEEN_FILE = Path("seen.json")
MAX_SEEN = 5000
INCLUIR_EXTRACTO = True   # añade un extracto en inglés del filing al aviso
EXTRACTO_MAX = 500        # caracteres por extracto
EXTRACTO_RESULTADOS_MAX = 1100  # más espacio para los resultados (2.02)
BUSCAR_NOTICIAS = True    # busca la nota de prensa en GlobeNewswire y PR Newswire
VENTANA_HORAS = 72        # cuánto antes del 8-K puede haberse publicado la noticia
ENLACE_WEBULL = True      # añade el enlace a la cotización en Webull
USAR_STOCKTITAN = True    # si no hay nota en las dos webs anteriores, prueba en StockTitan
NOTICIAS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BOLSAS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
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


def construir_extractos(entrada, items, headers):
    """{punto: extracto en inglés}. Si algo falla, devuelve lo que haya (o nada)."""
    extractos = {}
    try:
        principal, exhibit = documentos_filing(entrada.get("link", ""), headers)
        texto = descargar_texto(principal, headers) if principal else ""
        for i in items:
            seccion = extraer_seccion(texto, i)
            if seccion:
                extractos[i] = recortar(seccion, EXTRACTO_MAX)

        # Los resultados (2.02) solo remiten a un comunicado: lo bueno está en el Exhibit 99
        if exhibit and ("2.02" in items or not extractos):
            time.sleep(0.2)
            texto_ex = descargar_texto(exhibit, headers)
            comunicado = puntos_clave_resultados(texto_ex) if "2.02" in items else ""
            if not comunicado:
                comunicado = recortar(extracto_comunicado(texto_ex), EXTRACTO_MAX)
            if comunicado:
                extractos["2.02" if "2.02" in items else items[0]] = comunicado
    except Exception as e:  # el extracto nunca debe impedir el aviso
        print(f"No se pudo extraer el texto del filing: {e}")
    return extractos


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
        resumen = puntos_clave_resultados(texto) if resultados else ""
        return {
            "fuente": c["fuente"],
            "link": c["link"],
            "resumen": resumen or resumen_desde_dateline(texto, EXTRACTO_MAX),
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


def armar_mensaje(ticker, nombre, motivos, extractos, link, noticia=None, webull=None):
    con_resumen_noticia = bool(noticia and noticia.get("resumen"))
    bloques = []
    for i in motivos:
        bloque = f"• {i}: {html.escape(ITEMS_RELEVANTES.get(i, 'Otro'))}"
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

    return (
        f"🚨 <b>8-K · {html.escape(ticker)}</b>\n"
        f"{html.escape(nombre)}\n\n"
        f"{cuerpo}\n\n" + "\n".join(enlaces)
    )


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

    vistos = cargar_vistos()
    primera_vez = vistos is None
    vistos = vistos or []
    vistos_set = set(vistos)
    enviadas = nuevos = sin_formato = del_sp500 = a_avisar = 0
    bolsas = None
    otros = []

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

        ticker = candidatos[0]
        link = e.get("link", "")

        noticia = None
        if BUSCAR_NOTICIAS:
            try:
                noticia = buscar_noticia(ticker, m["name"], fecha_de(e), resultados=("2.02" in motivos))
            except Exception as ex:  # la búsqueda nunca debe impedir el aviso
                print(f"  búsqueda de noticia fallida: {ex}")
            print(f"  noticia relacionada: {noticia['link'] if noticia else 'ninguna'}")

        # Sin resumen de la noticia, se usa el extracto del propio filing
        extractos = {}
        if INCLUIR_EXTRACTO and not (noticia and noticia["resumen"]):
            extractos = construir_extractos(e, motivos[:3], headers)

        webull = None
        if ENLACE_WEBULL:
            if bolsas is None:
                bolsas = cargar_bolsas(headers)
            webull = enlace_webull(ticker, bolsas)

        mensaje = armar_mensaje(ticker, m["name"], motivos, extractos, link, noticia, webull)
        if len(mensaje) > 4000:  # límite de Telegram: 4096
            mensaje = armar_mensaje(ticker, m["name"], motivos, {}, link, noticia, webull)
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
