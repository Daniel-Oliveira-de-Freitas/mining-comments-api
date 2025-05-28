import os
import time
import requests
from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

app = Flask(__name__)

# Coloque sua chave da API Groq aqui (ou defina via variável de ambiente)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")


def scroll_to_bottom(driver):
    last_height = driver.execute_script("return document.body.scrollHeight")
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height


def scrape_full_html_from_page(url):
    options = Options()
    options.add_argument("--headless=chrome")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    try:
        driver.get(url)

        # Tentar clicar na aba de avaliações se existir
        # temos que mudar essa parte depois pra entender o que ele ta fazendo
        try:
            aba_avaliacoes = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//a[contains(@href, '#reviews')]"))
            )
            aba_avaliacoes.click()
            time.sleep(3)
        except Exception:
            print("Aba de avaliações não encontrada ou não clicável. Continuando...")

        # Rola até o fim da página para carregar todo o conteúdo dinâmico
        scroll_to_bottom(driver)

        # Pega o HTML completo da página
        full_html = driver.page_source
        print(full_html)
        return full_html

    except Exception as e:
        print(f"Erro ao acessar a URL ou extrair o HTML: {e}")
        return None
    finally:
        driver.quit()


def send_comments_to_llm(html_snippet, keyword):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    html_snippet = """
    <!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Comentário de Exemplo</title>
    <style>
        .ui-reviewcomment {
            border: 1px solid #ccc;
            padding: 10px;
            margin: 10px;
            font-family: Arial, sans-serif;
            background-color: #f9f9f9;
        }
        .ui-reviewauthor {
            font-weight: bold;
            margin-bottom: 5px;
        }
        .ui-reviewdate {
            font-size: 0.9em;
            color: #666;
            margin-bottom: 5px;
        }
        .ui-reviewcontent {
            font-size: 1em;
        }
    </style>
</head>
<body>

<div class="ui-reviewcomment">
    <div class="ui-reviewauthor">João Silva</div>
    <div class="ui-reviewdate">27 de Maio de 2025</div>
    <div class="ui-reviewcontent">
        Ótimo produto! Chegou rápido e a qualidade superou minhas expectativas.
    </div>
</div>

</body>
</html>
    """

    prompt = f"""
Você receberá blocos HTML contendo comentários de usuários.
Extraia os dados abaixo para cada comentário encontrado:

- "keyword": sempre retorne "{keyword}"
- "body": o texto do comentário
- "author": o nome do autor do comentário (se disponível)
- "createDate": data atual em formato ISO 8601
- "sentiment": null
- "search": null

Retorne uma lista de objetos JSON com esse formato exato para cada comentário.

HTML dos comentários:
{html_snippet}
    """

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 1
    }

    response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)

    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    else:
        print("Erro ao chamar a API Groq:", response.status_code, response.text)
        return None


@app.route('/analisar-comentarios', methods=['POST'])
def analisar_comentarios():
    data = request.get_json()
    urls = data.get("urls")  # Lista de URLs
    keyword = data.get("keyword")

    if not urls or not keyword:
        return jsonify({"error": "Parâmetros 'urls' (lista) e 'keyword' são obrigatórios."}), 400

    resultados = []

    for url in urls:
        html = scrape_full_html_from_page(url)
        if not html:
            resultados.append({"error": "Falha ao extrair comentários", "url": url})
            continue

        resultado = send_comments_to_llm(html, keyword)
        if resultado:
            resultados.append({"url": url, "resultado": resultado})
        else:
            resultados.append({"error": "Erro ao processar com a LLM", "url": url})

    return jsonify(resultados)


@app.route('/')
def hello():
    return 'Hello Comments World!'


if __name__ == '__main__':
    app.run(debug=True)
