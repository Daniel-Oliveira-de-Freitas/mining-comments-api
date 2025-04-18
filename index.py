from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin, urlparse
from collections import deque
import json

app = Flask(__name__)

@app.route('/')
def hello():
    return 'Hello Comments World!'

# Raspagem simples da URL fornecida
def scrape_comments_from_page(url):
    try:
        response = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
    except Exception as e:
        return {"error": f"Erro ao acessar a página: {str(e)}"}

    soup = BeautifulSoup(response.text, 'html.parser')

    comments = []

    for el in soup.find_all('p'):
        text = el.get_text(strip=True)
        if text and len(text) > 20:
            comment_obj = {
                "body": text,
                "author": "Desconhecido",
                "createDate": datetime.utcnow().isoformat(),
                "sentiment": None,
                "search": None
            }
            comments.append(comment_obj)

    return comments

# Crawler que segue links internos
def crawl_comments_from_url(start_url, max_pages=5):
    visited = set()
    queue = deque([start_url])
    comments = []

    headers = {'User-Agent': 'Mozilla/5.0'}

    while queue and len(visited) < max_pages:
        current_url = queue.popleft()
        if current_url in visited:
            continue

        try:
            response = requests.get(current_url, headers=headers, timeout=10)
            response.raise_for_status()
        except Exception as e:
            print(f"Erro ao acessar {current_url}: {e}")
            continue

        visited.add(current_url)
        soup = BeautifulSoup(response.text, 'html.parser')

        for p in soup.find_all('p'):
            text = p.get_text(strip=True)
            if text and len(text) > 20:
                comments.append({
                    "body": text,
                    "author": "Desconhecido",
                    "createDate": datetime.utcnow().isoformat(),
                    "sentiment": None,
                    "search": None
                })

        # Segue links internos
        base_url = '{uri.scheme}://{uri.netloc}'.format(uri=urlparse(start_url))
        for link_tag in soup.find_all('a', href=True):
            href = link_tag['href']
            full_url = urljoin(current_url, href)
            if urlparse(full_url).netloc == urlparse(start_url).netloc:
                if full_url not in visited and full_url.startswith(base_url):
                    queue.append(full_url)

    return comments

# Endpoint: /comments/scraping
@app.route('/comments/scraping', methods=['POST'])
def scraping_comments():
    data = request.get_json()
    url = data.get('url')

    comments = scrape_comments_from_page(url)

    if isinstance(comments, dict) and "error" in comments:
        return jsonify(comments), 400

    return jsonify({
        "status": "success",
        "message": f"{len(comments)} comentários coletados com scraping",
        "comments": comments
    })

# Endpoint: /comments/crawling
@app.route('/comments/crawling', methods=['POST'])
def crawling_comments():
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({"error": "URL é obrigatória"}), 400

    comments = crawl_comments_from_url(url)

    return jsonify({
        "status": "success",
        "message": f"{len(comments)} comentários coletados com crawling",
        "comments": comments
    })
