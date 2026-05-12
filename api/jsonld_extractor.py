"""
jsonld_extractor.py
Extrator de comentários a partir de dados estruturados Schema.org (JSON-LD).

Muitos sites declaram seus reviews/comentários em JSON-LD para SEO.
Quando disponível, isso é a fonte ideal: zero ambiguidade, zero custo de LLM.

Sites brasileiros que comumente usam JSON-LD para reviews:
- Mercado Livre (em algumas páginas de produto)
- Reclame Aqui (em páginas de empresa)
- Blogs (artigos com Schema.org/Article)
"""

import json
import logging
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup

from .utils import clean_text, now_iso, sha1

logger = logging.getLogger(__name__)

MIN_BODY_LEN = 30


def _ensure_list(value):
    """Garante que o valor é uma lista (alguns JSON-LD usam objeto único)."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _extract_author(author_field):
    """Normaliza o campo de autor (pode ser string ou objeto {name: ...})."""
    if isinstance(author_field, dict):
        return clean_text(author_field.get("name", ""))
    return clean_text(str(author_field or ""))


def _process_review_node(node: Dict[str, Any], keyword: str, search: Optional[str]) -> Optional[Dict[str, Any]]:
    """Converte um nó @type=Review em um item de comentário no nosso formato."""
    if not isinstance(node, dict) or node.get("@type") != "Review":
        return None

    body = clean_text(node.get("reviewBody") or node.get("description") or "")
    if len(body) < MIN_BODY_LEN:
        return None

    author = _extract_author(node.get("author")) or "Desconhecido"
    date_published = node.get("datePublished") or now_iso()

    return {
        "keyword": keyword,
        "body": body,
        "author": author,
        "createDate": date_published,
        "sentiment": None,
        "search": search,
    }


def extract_from_jsonld(html: str, keyword: str, search: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Extrai comentários de blocos Schema.org JSON-LD encontrados no HTML.

    Args:
        html: HTML da página
        keyword: keyword da pesquisa (preenchida em cada comentário)
        search: termo de busca opcional

    Returns:
        Lista de comentários (pode ser vazia se a página não tiver JSON-LD).
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    found: List[Dict[str, Any]] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        for top_node in _ensure_list(payload):
            if not isinstance(top_node, dict):
                continue

            # Caso 1: Produto/Serviço/CreativeWork com array "review"
            if top_node.get("@type") in {"Product", "CreativeWork", "Service", "Thing", "Organization", "LocalBusiness"}:
                for review in _ensure_list(top_node.get("review")):
                    item = _process_review_node(review, keyword, search)
                    if item:
                        found.append(item)

            # Caso 2: Review direto no topo
            if top_node.get("@type") == "Review":
                item = _process_review_node(top_node, keyword, search)
                if item:
                    found.append(item)

    # Deduplicação por corpo
    unique, seen = [], set()
    for item in found:
        h = sha1(item["body"])
        if h not in seen:
            seen.add(h)
            unique.append(item)

    if unique:
        logger.info(f"[jsonld] {len(unique)} comentários extraídos via Schema.org")

    return unique
