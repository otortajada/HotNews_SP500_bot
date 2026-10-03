import os
import requests
import feedparser

# Configuración mediante Secrets de GitHub
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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

def enviar_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Error: No se han configurado las credenciales de Telegram.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")

def revisar_feeds():
    headers = {'User-Agent': 'MiSistemaAlertasTrading miemail@ejemplo.com'}
    alertas_enviadas = 0

    for feed_url in RSS_FEEDS:
        try:
            response = requests.get(feed_url, headers=headers, timeout=10)
            feed = feedparser.parse(response.content)

            for entry in feed.entries:
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
                        alertas_enviadas += 1
                        break

        except Exception as e:
            print(f"Error procesando feed {feed_url}: {e}")

    if alertas_enviadas == 0:
        print("Escaneo completado sin nuevas alertas coincidentes.")

if __name__ == "__main__":
    revisar_feeds()
