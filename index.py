from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin, urlparse
from collections import deque
from flask_cors import CORS
import os

app = Flask(__name__)
CORS(app, origins='*')

@app.route('/')
def hello():
    return 'Hello Comments World!'

def scrape_comments_from_page(url, keyword, search):
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
            comments.append({
                "keyword": keyword,
                "body": text,
                "author": "Desconhecido",
                "createDate": datetime.utcnow().isoformat(),
                "sentiment": None,
                "search": search
            })

    return comments

def crawl_comments_from_url(start_url, keyword, search, max_pages=5):
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
                    "keyword": keyword,
                    "body": text,
                    "author": "Desconhecido",
                    "createDate": datetime.utcnow().isoformat(),
                    "sentiment": None,
                    "search": search
                })

        base_url = '{uri.scheme}://{uri.netloc}'.format(uri=urlparse(start_url))
        for link_tag in soup.find_all('a', href=True):
            href = link_tag['href']
            full_url = urljoin(current_url, href)
            if urlparse(full_url).netloc == urlparse(start_url).netloc:
                if full_url not in visited and full_url.startswith(base_url):
                    queue.append(full_url)

    return comments

@app.route('/comments/scraping', methods=['POST'])
def scraping_comments():
    data = request.get_json()
    urls = data.get('urls')
    keyword = data.get('keyword')
    search = data.get('search')

    if not urls or not isinstance(urls, list) or not keyword:
        return jsonify({"error": "urls (lista) e keyword são obrigatórios"}), 400

    all_comments = []

    for url in urls:
        comments = scrape_comments_from_page(url, keyword, search)
        if isinstance(comments, dict) and "error" in comments:
            continue
        all_comments.extend(comments)

    return jsonify({
        "status": "success",
        "message": f"{len(all_comments)} comentários coletados com scraping",
        "comments": all_comments
    })

@app.route('/comments/crawling', methods=['POST'])
def crawling_comments():
    data = request.get_json()
    urls = data.get('urls')
    keyword = data.get('keyword')
    search = data.get('search')

    if not urls or not isinstance(urls, list) or not keyword:
        return jsonify({"error": "urls (lista) e keyword são obrigatórios"}), 400

    all_comments = []

    for url in urls:
        comments = crawl_comments_from_url(url, keyword, search)
        all_comments.extend(comments)

    return jsonify({
        "status": "success",
        "message": f"{len(all_comments)} comentários coletados com crawling",
        "comments": all_comments
    })

# Permite rodar localmente com `python index.py`
# if __name__ == "__main__":
#     port = int(os.environ.get("PORT", 5000))
#     app.run(host="0.0.0.0", port=port)
