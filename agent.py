import json
import os
import re
import socket
import sqlite3

import feedparser
from google import genai

SOURCES = [
    ("Hacker News", "https://hnrss.org/frontpage"),
    ("GitHub Blog", "https://github.blog/feed/"),
    ("LWN.net", "https://lwn.net/headlines/rss"),
    ("Lobsters", "https://lobste.rs/rss"),
    ("DEV Community", "https://dev.to/feed"),
]

KEYWORDS = ["python", "security", "linux", "release"]

DB_FILE = "agent.db"
KEY_FILE = "gemini_key.txt"
MODEL = "gemini-3.5-flash-lite"
AI_MIN_SCORE = 5
TITLE_POINTS = 2
SUMMARY_POINTS = 1
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) IT-News-Agent/1.0"

socket.setdefaulttimeout(15)


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url  TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES sources(id),
            title     TEXT NOT NULL,
            link      TEXT NOT NULL UNIQUE,
            published TEXT,
            score     INTEGER NOT NULL,
            reason    TEXT,
            added_at  TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    for name, url in SOURCES:
        conn.execute("INSERT OR IGNORE INTO sources (name, url) VALUES (?, ?)", (name, url))
    conn.commit()
    return conn


def fetch(url):
    try:
        feed = feedparser.parse(url, agent=USER_AGENT)
    except Exception:
        return []

    if feed.get("status", 200) >= 400 or (feed.get("bozo") and not feed.entries):
        return []

    articles = []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
        articles.append({
            "title": " ".join(entry.get("title", "").split()),
            "link": link,
            "summary": " ".join(summary.split()),
            "published": entry.get("published", entry.get("updated", "")),
        })
    return articles


def ai_score(articles, topics):
    key = os.environ.get("GEMINI_API_KEY")
    if not key and os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        print(f"[!] Немає API-ключа Gemini (файл {KEY_FILE})")
        return None

    lines = [f"{i}. {a['title']} — {a['summary'][:200]}" for i, a in enumerate(articles)]
    prompt = (
        "Ти фільтр IT-новин. Теми користувача: " + ", ".join(topics) + ".\n"
        "Для кожної статті нижче постав бал від 0 до 10: наскільки вона за змістом "
        "відповідає цим темам. Додай коротке пояснення українською (до 8 слів).\n"
        'Відповідай лише JSON-масивом: [{"id": 0, "score": 7, "reason": "..."}]\n\n'
        + "\n".join(lines)
    )

    try:
        client = genai.Client(api_key=key, http_options={"timeout": 120000})
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        data = json.loads(response.text)
    except Exception as error:
        print(f"[!] Gemini недоступний: {str(error)[:120]}")
        return None

    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])

    result = [(0, "") for _ in articles]
    for item in data:
        try:
            i = int(item["id"])
            if 0 <= i < len(articles):
                result[i] = (int(float(item["score"])), str(item.get("reason", "")))
        except (KeyError, TypeError, ValueError):
            continue
    return result


def keyword_score(article, keywords):
    title = article["title"].lower()
    summary = article["summary"].lower()
    points = 0
    matched = []
    for word in keywords:
        word = word.lower()
        found = False
        if word in title:
            points += TITLE_POINTS
            found = True
        if word in summary:
            points += SUMMARY_POINTS
            found = True
        if found:
            matched.append(word)
    return points, matched


def save(conn, article, points, reason):
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO articles (source_id, title, link, published, score, reason)
        VALUES ((SELECT id FROM sources WHERE url = ?), ?, ?, ?, ?, ?)
        """,
        (article["source_url"], article["title"], article["link"],
         article["published"], points, reason),
    )
    conn.commit()
    return cursor.rowcount == 1


def show(stats, results):
    print(f"Режим оцінки: {stats['mode']}")
    print(f"Джерел опрацьовано: {stats['sources']}")
    print(f"Отримано статей: {stats['fetched']}")
    print(f"Пройшли фільтр: {stats['passed']}")
    print(f"Нових збережено: {stats['new']}")
    print()

    if not results:
        print("Статей за вашими темами не знайдено.")
        return

    print("----- Відібрані статті -----")
    for item in sorted(results, key=lambda r: r["score"], reverse=True):
        print(f"[{item['score']}] {item['title']} | {item['source']} | {item['reason']}")
        print(f"    {item['link']}")


def main():
    print("===== IT News Agent =====")
    conn = init_db()
    stats = {"sources": len(SOURCES), "fetched": 0, "passed": 0, "new": 0}

    articles = []
    seen = set()
    for name, url in SOURCES:
        items = fetch(url)
        if not items:
            print(f"[!] Джерело недоступне: {name}")
            continue
        stats["fetched"] += len(items)
        for item in items:
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            item["source"], item["source_url"] = name, url
            articles.append(item)

    scores = ai_score(articles, KEYWORDS) if articles else None
    if scores is not None:
        stats["mode"] = f"Gemini ({MODEL})"
        threshold = AI_MIN_SCORE
    else:
        stats["mode"] = "ключові слова (Gemini недоступний)"
        threshold = 1
        scores = []
        for article in articles:
            points, matched = keyword_score(article, KEYWORDS)
            scores.append((points, ", ".join(matched)))

    results = []
    for article, (points, reason) in zip(articles, scores):
        if points >= threshold:
            stats["passed"] += 1
            if save(conn, article, points, reason):
                stats["new"] += 1
            results.append({**article, "score": points, "reason": reason})

    conn.close()
    show(stats, results)


if __name__ == "__main__":
    main()