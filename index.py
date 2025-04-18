# api/index.py
from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
from datetime import datetime

app = Flask(__name__)

@app.route('/')
def hello():
    return 'Hello Comments World!'

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

# Não use app.run() no Vercel!
