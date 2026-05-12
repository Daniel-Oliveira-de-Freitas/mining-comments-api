"""
utils.py
Funções utilitárias reaproveitadas/refinadas das versões anteriores.
"""

import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse


def now_iso() -> str:
    """ISO timestamp em UTC."""
    return datetime.now(timezone.utc).isoformat()


def sha1(text: str) -> str:
    """SHA-1 hash de uma string (usado para deduplicação)."""
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def clean_text(s: str) -> str:
    """Remove espaços e quebras de linha redundantes."""
    return re.sub(r"\s+", " ", s or "").strip()


def get_domain(url: str) -> str:
    """Extrai o domínio de uma URL (lowercase, sem schema)."""
    return urlparse(url).netloc.lower()


def estimate_tokens(s: str) -> int:
    """Estimativa grosseira de tokens (~4 chars/token)."""
    return max(1, len(s) // 4)


def needs_javascript_rendering(url: str) -> bool:
    """
    Heurística simples: alguns domínios são conhecidos por exigir
    renderização de JavaScript para mostrar comentários/reviews.
    """
    domain = get_domain(url)
    js_domains = {
        "google.com",
        "google.com.br",
        "maps.google.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "facebook.com",
    }
    return any(d in domain for d in js_domains)
