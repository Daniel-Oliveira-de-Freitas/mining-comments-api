from flask import Flask, request, jsonify
from goose3 import Goose
import json
import os

app = Flask(__name__)

@app.route('/')
def hello():
    return 'Hello World of the Comments!'

@app.route('/fakeNewsDetection/comments', methods=['POST'])
def filter_comments():
    req_data = request.get_json()

    if not req_data:
        return jsonify({"error": "JSON inválido ou não enviado."}), 400

    urls = req_data.get("urls", [])
    keyword = req_data.get("keyword", "").lower()
    result = []

    for url in urls:
        try:
            g = Goose()
            article = g.extract(url=url)
            text = article.cleaned_text or ""

            comentarios = text.split('\n')
            comentarios_filtrados = [c for c in comentarios if keyword in c.lower()]

            if comentarios_filtrados:
                result.append({
                    "url": url,
                    "comentarios_filtrados": comentarios_filtrados
                })

        except Exception as e:
            result.append({
                "url": url,
                "erro": str(e)
            })

    # Salva resultado em JSON local (opcional)
    output_path = os.path.join(os.getcwd(), 'comentarios_filtrados.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)

    return jsonify(result)
