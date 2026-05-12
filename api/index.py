"""
index.py
API Flask para mineração de comentários do Proraf Social.

Pipeline de extração:
  1. Busca o HTML da URL (requests ou ScrapingBee para sites com JS).
  2. Tenta extrair via Schema.org JSON-LD (zero custo, quando disponível).
  3. Se JSON-LD não retornar nada, limpa o HTML e envia ao LLM (Groq).
  4. Normaliza, deduplica e retorna ao cliente.

Endpoint único: POST /comments/extract

Configuração via variáveis de ambiente:
  GROQ_API_KEY          - obrigatória, chave da API do Groq
  SCRAPINGBEE_API_KEY   - opcional, para sites com JavaScript (Google Reviews etc.)
  GROQ_MODEL            - opcional, default: llama-3.3-70b-versatile
  GROQ_TPM_BUDGET       - opcional, default: 6000
  LOG_LEVEL             - opcional, default: INFO
"""

import os
import logging
import time
from typing import List, Dict, Any

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify
from flask_cors import CORS

from .utils import now_iso, get_domain
from .fetcher import fetch_html
from .cleaner import clean_html_for_llm
from .jsonld_extractor import extract_from_jsonld
from .llm_extractor import extract_via_llm


# ==================== Setup Flask ====================

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ==================== Pipeline ====================

def extract_comments_from_url(
    url: str,
    keyword: str,
    search: str = None,
    force_js: bool = False,
    max_comments: int = 200,
) -> Dict[str, Any]:
    """
    Pipeline completo de extração para uma única URL.

    Returns:
        {
          "url": str,
          "domain": str,
          "source": "jsonld" | "llm" | "none",
          "fetcher": "requests" | "scrapingbee" | "none",
          "elapsedSec": float,
          "comments": [...]
        }
    """
    start = time.time()
    result = {
        "url": url,
        "domain": get_domain(url),
        "source": "none",
        "fetcher": "none",
        "elapsedSec": 0.0,
        "comments": [],
    }

    # 1. Buscar HTML
    html, fetcher_used = fetch_html(url, force_js=force_js)
    result["fetcher"] = fetcher_used
    if not html:
        result["elapsedSec"] = round(time.time() - start, 2)
        logger.warning(f"[pipeline] falha ao buscar HTML de {url}")
        return result

    # 2. Tentar JSON-LD primeiro (rápido, sem custo de LLM)
    jsonld_comments = extract_from_jsonld(html, keyword, search)
    if jsonld_comments:
        result["source"] = "jsonld"
        result["comments"] = jsonld_comments[:max_comments]
        result["elapsedSec"] = round(time.time() - start, 2)
        return result

    # 3. Fallback: limpar HTML e enviar ao LLM
    cleaned = clean_html_for_llm(html)
    if not cleaned:
        result["elapsedSec"] = round(time.time() - start, 2)
        return result

    llm_comments = extract_via_llm(cleaned, keyword, search)
    if llm_comments:
        result["source"] = "llm"
        result["comments"] = llm_comments[:max_comments]

    result["elapsedSec"] = round(time.time() - start, 2)
    return result


# ==================== Endpoints ====================

@app.route("/")
def root():
    return jsonify({
        "service": "mining-comments-api",
        "version": "3.0",
        "endpoint": "POST /comments/extract",
    })


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "time": now_iso(),
        "groq_configured": bool(os.getenv("GROQ_API_KEY")),
        "scrapingbee_configured": bool(os.getenv("SCRAPINGBEE_API_KEY")),
    })


@app.route("/comments/extract", methods=["POST"])
def comments_extract():
    """
    Endpoint principal de extração de comentários.

    Body JSON:
    {
      "urls": ["https://...", "https://..."],     // obrigatório
      "keyword": "produto-ou-empresa",            // obrigatório
      "search": "termo-de-busca",                 // opcional
      "forceJs": false,                           // opcional, força ScrapingBee
      "maxCommentsPerUrl": 200                    // opcional, default 200
    }

    Resposta:
    {
      "status": "success",
      "totalComments": N,
      "stats": [{url, source, fetcher, elapsedSec, comments_count}, ...],
      "comments": [...]
    }
    """
    data = request.get_json(force=True, silent=True) or {}

    urls = data.get("urls")
    keyword = data.get("keyword")
    search = data.get("search")
    force_js = bool(data.get("forceJs", False))
    max_comments = int(data.get("maxCommentsPerUrl", 200))

    # Validação
    if not urls or not isinstance(urls, list):
        return jsonify({"error": "'urls' (lista) é obrigatório"}), 400
    if not keyword or not isinstance(keyword, str):
        return jsonify({"error": "'keyword' (string) é obrigatório"}), 400

    # Processa cada URL
    all_comments: List[Dict[str, Any]] = []
    stats: List[Dict[str, Any]] = []

    for url in urls:
        try:
            result = extract_comments_from_url(url, keyword, search, force_js, max_comments)
        except Exception as e:
            logger.exception(f"[extract] erro inesperado em {url}: {e}")
            stats.append({
                "url": url,
                "domain": get_domain(url),
                "source": "error",
                "fetcher": "none",
                "elapsedSec": 0.0,
                "commentsCount": 0,
                "error": str(e),
            })
            continue

        all_comments.extend(result["comments"])
        stats.append({
            "url": result["url"],
            "domain": result["domain"],
            "source": result["source"],
            "fetcher": result["fetcher"],
            "elapsedSec": result["elapsedSec"],
            "commentsCount": len(result["comments"]),
        })

    return jsonify({
        "status": "success",
        "totalComments": len(all_comments),
        "stats": stats,
        "comments": all_comments,
    })


# ==================== Run ====================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    logger.info(f"Iniciando Flask em http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
