import requests
import os
from datetime import datetime
from pytrends.request import TrendReq

NEWS_API_KEY = os.getenv("NEWS_API_KEY")
REDDIT_USER_AGENT = "trend-app/0.1"

def fetch_news():
    url = f"https://newsapi.org/v2/top-headlines?country=us&apiKey={NEWS_API_KEY}"
    res = requests.get(url)
    data = res.json()

    articles = [a["title"] for a in data.get("articles", [])]
    return articles


def fetch_reddit():
    url = "https://www.reddit.com/r/news/hot.json?limit=20"
    headers = {
        "User-Agent": "trend-app/1.0"
    }

    res = requests.get(url, headers=headers)

    print("status:", res.status_code)
    print("text preview:", res.text[:200])

    if res.status_code != 200:
        return []

    try:
        data = res.json()
    except Exception:
        return []

    return [
        p["data"]["title"]
        for p in data["data"]["children"]
    ]


def fetch_google_trends():
    # pytrends 사용

    pytrends = TrendReq()
    trending = pytrends.trending_searches(pn="united_states")

    return trending[0].tolist()


def collect_all():
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "news": fetch_news(),
        "reddit": fetch_reddit()#,
        #"trends": fetch_google_trends(),
    }
print(collect_all())