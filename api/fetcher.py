"""
fetcher.py
Responsável por buscar o HTML de uma URL.

Estratégia:
1. Para sites estáticos (Reclame Aqui, Mercado Livre, notícias): usa `requests` puro.
2. Para sites com conteúdo dinâmico (Google Reviews, etc.): usa ScrapingBee,
   um serviço de browser headless gerenciado (free tier: 1000 chamadas/mês).

ScrapingBee é a escolha por rodar no Vercel sem precisar de Chromium instalado.
Documentação: https://www.scrapingbee.com/documentation/
"""

import os
import logging
import requests
from typing import Optional, Tuple

from .utils import needs_javascript_rendering

logger = logging.getLogger(__name__)

# Configuração
SCRAPINGBEE_API_KEY = os.getenv("SCRAPINGBEE_API_KEY")
SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"

DEFAULT_TIMEOUT = 25  # ScrapingBee pode levar mais tempo que requests puro

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


def fetch_via_requests(url: str) -> Optional[str]:
    """Busca HTML usando requests puro. Retorna None em caso de erro."""
    try:
        logger.info(f"[fetch:requests] GET {url}")
        response = requests.get(url, headers=BASE_HEADERS, timeout=15)
        response.raise_for_status()
        logger.info(f"[fetch:requests] OK ({len(response.text)} bytes)")
        return response.text
    except Exception as e:
        logger.warning(f"[fetch:requests] falhou em {url}: {e}")
        return None


def fetch_via_scrapingbee(url: str, render_js: bool = True) -> Optional[str]:
    """
    Busca HTML usando ScrapingBee (browser headless gerenciado).
    Necessário para sites com conteúdo carregado via JavaScript.
    """
    if not SCRAPINGBEE_API_KEY:
        logger.warning("[fetch:scrapingbee] SCRAPINGBEE_API_KEY não configurada")
        return None

    params = {
        "api_key": SCRAPINGBEE_API_KEY,
        "url": url,
        "render_js": "true" if render_js else "false",
        # premium_proxy ajuda com bloqueios mas consome mais créditos
        # "premium_proxy": "true",
        # Espera o conteúdo dinâmico carregar
        "wait": "2000",
    }

    try:
        logger.info(f"[fetch:scrapingbee] GET {url} (render_js={render_js})")
        response = requests.get(SCRAPINGBEE_URL, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        logger.info(f"[fetch:scrapingbee] OK ({len(response.text)} bytes)")
        return response.text
    except Exception as e:
        logger.warning(f"[fetch:scrapingbee] falhou em {url}: {e}")
        return None


def fetch_html(url: str, force_js: bool = False) -> Tuple[Optional[str], str]:
    """
    Busca HTML usando a estratégia mais adequada para a URL.

    Args:
        url: URL a buscar
        force_js: Se True, força uso do ScrapingBee independente do domínio

    Returns:
        (html, method) onde method é "requests", "scrapingbee" ou "none"
    """
    use_js = force_js or needs_javascript_rendering(url)

    if use_js:
        html = fetch_via_scrapingbee(url, render_js=True)
        if html:
            return html, "scrapingbee"
        # Fallback para requests se ScrapingBee falhar (ou não estiver configurado)
        logger.info("[fetch] ScrapingBee falhou, tentando requests como fallback")

    html = fetch_via_requests(url)
    if html:
        return html, "requests"

    # Última tentativa: se ainda não tentamos ScrapingBee, tentar agora
    if not use_js and SCRAPINGBEE_API_KEY:
        logger.info("[fetch] requests falhou, tentando ScrapingBee como fallback")
        html = fetch_via_scrapingbee(url, render_js=True)
        if html:
            return html, "scrapingbee"

    return None, "none"
