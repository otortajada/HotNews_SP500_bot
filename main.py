import os
import time
import threading
import requests
import feedparser
from flask import Flask

# Servidor Web mínimo para mantener Render despierto
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Bot de Alertas de Trading activo y escaneando 24/7."

# ==========================================
# CONFIGURACIÓN VÍA VARIABLES DE ENTORNO
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")

CHECK_INTERVAL = 300  # 5 minutos

RSS_FEEDS = [
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=40&output=atom",
    "https://seekingalpha.com/market_currents.xml",
    "https://www.prnewswire.com/rss/financial-services-news.rss"
]

SP500_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK.B", "UNH", 
    "JNJ", "JPM", "V", "XOM", "PG", "MA", "HD", "CVX", "MRK", "ABBV",
    "COST", "PEP", "KO", "ADBE", "WMT", "MCD", "CSCO", "CRM", "ACN", "BAC"
]

noticias_procesadas = set()

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje: {e}")

def revisar_feeds():
    headers = {'User-Agent': 'MiSistemaAlertasTrading miemail@ejemplo.com'}

    for feed_url in RSS_FEEDS:
        try:
            response = requests.get(feed_url, headers=headers, timeout=10)
            feed = feedparser.parse(response.content)

            for entry in feed.entries:
                noticia_id = entry.get('id', entry.get('link', entry.get('title')))
                
                if noticia_id in noticias_procesadas:
                    continue

                titulo = entry.get('title', '')
                link = entry.get('link', '')

                for ticker in SP500_TICKERS:
                    if f" {ticker} " in f" {titulo} " or f"({ticker})" in titulo:
                        mensaje = (
                            f"🚨 *ALERTA S&P 500: {ticker}*\n\n"
                            f"📰 *Titular:* {titulo}\n\n"
                            f"🔗 [Ver Noticia]({link})"
                        )
                        enviar_telegram(mensaje)
                        print(f"Notificación enviada para {ticker}")
                        break

                noticias_procesadas.add(noticia_id)

        except Exception as e:
            print(f"Error procesando feed {feed_url}: {e}")

def bucle_bot():
    print("🤖 Bot iniciado correctamente...")
    enviar_telegram("🤖 *Bot de Alertas Activado en Render (24/7)*")
    while True:
        revisar_feeds()
        time.sleep(CHECK_INTERVAL)

# Arrancar el bucle del bot en segundo plano
threading.Thread(target=bucle_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
