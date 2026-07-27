import html
import re
import requests
from datetime import datetime
import time
import xml.etree.ElementTree as ET


def is_rate_limited(response):
    """Check if the response indicates rate limiting (status code 429)"""
    return response.status_code == 429


def make_request(url, headers, params=None, max_attempts=3):
    """Make a request with retry logic for rate limiting"""
    last_error = None

    for attempt in range(max_attempts):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
            if is_rate_limited(response) and attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as error:
            last_error = error
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
                continue

    raise last_error


def _date_for_query(date_str):
    if "-" in date_str:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    return datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y-%m-%d")


def _clean_html_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def getNewsData(query, start_date, end_date):
    """
    Scrape Google News search results for a given query and date range.
    query: str - search query
    start_date: str - start date in the format yyyy-mm-dd or mm/dd/yyyy
    end_date: str - end date in the format yyyy-mm-dd or mm/dd/yyyy
    """
    start_date = _date_for_query(start_date)
    end_date = _date_for_query(end_date)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/101.0.4951.54 Safari/537.36"
        )
    }

    url = "https://news.google.com/rss/search"
    params = {
        "q": f"{query} after:{start_date} before:{end_date}",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    }

    try:
        response = make_request(url, headers, params=params)
        root = ET.fromstring(response.content)
    except Exception as e:
        print(f"Failed after multiple retries: {e}")
        return []

    news_results = []
    for item in root.findall("./channel/item"):
        source = item.find("source")
        news_results.append(
            {
                "link": item.findtext("link", default=""),
                "title": item.findtext("title", default=""),
                "snippet": _clean_html_text(item.findtext("description", default="")),
                "date": item.findtext("pubDate", default=""),
                "source": source.text if source is not None and source.text else "",
            }
        )

    return news_results
