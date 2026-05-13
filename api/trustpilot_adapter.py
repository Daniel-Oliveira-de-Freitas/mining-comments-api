"""
trustpilot_adapter.py
Adapter específico para o Trustpilot.

O Trustpilot bloqueia agressivamente scraping de HTML (403 com requests,
500 com ScrapingBee básico por causa do Cloudflare Bot Management).

Solução: usar a API interna que o próprio site consome para carregar reviews.
O endpoint público não requer autenticação e retorna JSON estruturado.

Endpoint usado:
  GET https://www.trustpilot.com/api/categoriespages.jsonld?businessUnitId=<id>
  GET https://www.trustpilot.com/api/v1/business-units/<id>/reviews

Para obter o businessUnitId, buscamos na API de search:
  GET https://api.trustpilot.com/v1/business-units/search?query=<domain>

Referência: https://support.trustpilot.com/hc/en-us/articles/201657028
"""

import re
import logging
import requests
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

from .utils import clean_text, now_iso, sha1

logger = logging.getLogger(__name__)

TRUSTPILOT_DOMAINS = {"trustpilot.com", "br.trustpilot.com"}

# Headers que imitam o browser para as chamadas à API interna
API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://br.trustpilot.com/",
    "Origin": "https://br.trustpilot.com",
}

MIN_BODY_LEN = 30


def is_trustpilot_url(url: str) -> bool:
    """Verifica se a URL é do Trustpilot."""
    domain = urlparse(url).netloc.lower()
    return any(tp in domain for tp in TRUSTPILOT_DOMAINS)


def _extract_domain_from_url(url: str) -> Optional[str]:
    """
    Extrai o domínio da empresa a partir de uma URL do Trustpilot.
    Ex: https://br.trustpilot.com/review/shopmundodigital.com.br
         → shopmundodigital.com.br
    """
    path = urlparse(url).path  # /review/shopmundodigital.com.br
    parts = [p for p in path.split("/") if p]
    # Formato esperado: /review/<domain> ou /<domain>
    if "review" in parts:
        idx = parts.index("review")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    elif parts:
        return parts[-1]
    return None


def _find_business_unit_id(company_domain: str) -> Optional[str]:
    """
    Busca o businessUnitId do Trustpilot a partir do domínio da empresa.
    Usa a API de busca pública do Trustpilot.
    """
    search_url = "https://www.trustpilot.com/api/categoriespages.jsonld"

    # Tenta o endpoint de busca por nome de domínio
    search_api = f"https://www.trustpilot.com/search?query={company_domain}"

    # Endpoint mais direto: página de reviews da empresa em JSON-LD
    # O businessUnitId aparece no HTML em data-business-unit-id="..."
    review_page_url = f"https://br.trustpilot.com/review/{company_domain}"

    try:
        logger.info(f"[trustpilot] buscando businessUnitId para {company_domain}")
        response = requests.get(review_page_url, headers=API_HEADERS, timeout=15)

        if response.status_code == 200:
            # Tenta extrair o businessUnitId do HTML/JSON embutido
            text = response.text

            # Padrão 1: data-business-unit-id="<id>"
            m = re.search(r'data-business-unit-id=["\']([a-f0-9]+)["\']', text)
            if m:
                return m.group(1)

            # Padrão 2: "businessUnitId":"<id>"
            m = re.search(r'"businessUnitId"\s*:\s*"([a-f0-9]+)"', text)
            if m:
                return m.group(1)

            # Padrão 3: /review/<domain>?... com id no JSON de estado
            m = re.search(r'"id"\s*:\s*"([a-f0-9]{24})"', text)
            if m:
                return m.group(1)

        logger.warning(f"[trustpilot] não foi possível obter businessUnitId (status {response.status_code})")
    except Exception as e:
        logger.warning(f"[trustpilot] erro ao buscar businessUnitId: {e}")

    return None


def _fetch_reviews_via_api(business_unit_id: str, keyword: str, search: Optional[str]) -> List[Dict[str, Any]]:
    """
    Busca reviews usando a API interna do Trustpilot.
    Retorna até 20 reviews da primeira página.
    """
    api_url = (
        f"https://www.trustpilot.com/api/v1/business-units/{business_unit_id}/reviews"
        f"?perPage=20&language=all"
    )

    try:
        logger.info(f"[trustpilot] buscando reviews via API interna: {api_url}")
        response = requests.get(api_url, headers=API_HEADERS, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning(f"[trustpilot] falha na API interna de reviews: {e}")
        return []

    reviews = data.get("reviews", [])
    if not reviews:
        logger.info("[trustpilot] API retornou 0 reviews")
        return []

    result = []
    seen = set()

    for rv in reviews:
        body = clean_text(rv.get("text", ""))
        if len(body) < MIN_BODY_LEN:
            continue

        h = sha1(body)
        if h in seen:
            continue
        seen.add(h)

        # Autor
        consumer = rv.get("consumer", {})
        author = clean_text(consumer.get("displayName", "")) or "Desconhecido"

        # Data
        create_date = rv.get("createdAt") or rv.get("publishedAt") or now_iso()

        # Nota (1-5) — útil como contexto, vai no body
        rating = rv.get("rating", {}).get("stars") if isinstance(rv.get("rating"), dict) else rv.get("rating")
        body_with_rating = f"[{rating}★] {body}" if rating else body

        result.append({
            "keyword": keyword,
            "body": body_with_rating,
            "author": author,
            "createDate": create_date,
            "sentiment": None,
            "search": search,
        })

    logger.info(f"[trustpilot] {len(result)} reviews extraídos via API")
    return result


def extract_from_trustpilot(url: str, keyword: str, search: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Pipeline completo de extração para URLs do Trustpilot.

    Estratégia:
    1. Extrai o domínio da empresa a partir da URL.
    2. Obtém o businessUnitId via página de reviews.
    3. Usa a API interna para buscar reviews em JSON.

    Args:
        url: URL do Trustpilot (ex: https://br.trustpilot.com/review/shopmundodigital.com.br)
        keyword: keyword da pesquisa
        search: search ID opcional

    Returns:
        Lista de comentários no formato padrão, ou lista vazia em caso de falha.
    """
    company_domain = _extract_domain_from_url(url)
    if not company_domain:
        logger.warning(f"[trustpilot] não foi possível extrair domínio de {url}")
        return []

    logger.info(f"[trustpilot] domínio extraído: {company_domain}")

    business_unit_id = _find_business_unit_id(company_domain)
    if not business_unit_id:
        logger.warning(f"[trustpilot] businessUnitId não encontrado para {company_domain}")
        return []

    logger.info(f"[trustpilot] businessUnitId: {business_unit_id}")
    return _fetch_reviews_via_api(business_unit_id, keyword, search)
