from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime
import hashlib

app = Flask(__name__)

def scrape_comments_from_page(url):
    try:
        response = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
    except Exception as e:
        return {"error": f"Erro ao acessar a página: {str(e)}"}

    soup = BeautifulSoup(response.text, 'html.parser')

    # DEBUG: salvar HTML num arquivo local para inspecionar
    with open('pagina_raspada.html', 'w', encoding='utf-8') as f:
        f.write(soup.prettify())

    comments = []

    # Tenta pegar todos os <p> visíveis que não estejam vazios
    for el in soup.find_all('p'):
        text = el.get_text(strip=True)
        if text and len(text) > 20:  # ignora textos muito curtos
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

    with open('scraped_comments.json', 'w', encoding='utf-8') as f:
        json.dump(comments, f, ensure_ascii=False, indent=4)

    return jsonify({
        "status": "success",
        "message": f"{len(comments)} comentários coletados com scraping",
        "comments": comments
    })


if __name__ == '__main__':
    app.run(port=5000)
