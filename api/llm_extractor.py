"""
llm_extractor.py
Extrator de comentários via LLM (Groq API).

Este é o componente principal do novo pipeline. Em vez de adapters específicos
por site (com seletores CSS frágeis), enviamos o HTML limpo para um LLM
open-source (Llama 3.3 70B) hospedado no Groq, que retorna um JSON estruturado.

Vantagens:
- Funciona em qualquer site sem código específico.
- Resiliente a mudanças no HTML do site.
- Identifica autores e datas quando presentes no contexto.
- Free tier do Groq é generoso (~30 req/min, suficiente para uso acadêmico).

Documentação Groq: https://console.groq.com/docs
"""

import os
import json
import logging
import time
from typing import List, Dict, Any, Optional

import requests

from .utils import clean_text, now_iso, sha1, estimate_tokens

logger = logging.getLogger(__name__)

# Configuração
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Limites
MAX_OUTPUT_TOKENS = 4096  # Espaço para JSON com vários comentários
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
MIN_BODY_LEN = 30  # Aumentado para filtrar fragmentos curtos que raramente são opiniões reais


# ==================== Token Budget (rate limiting) ====================

class TokenBudget:
    """
    Rate limiter simples por minuto. Groq free tier ~6000 tokens/min para llama-3.3-70b.
    """

    def __init__(self, tokens_per_minute: int = 6000):
        self.limit = tokens_per_minute
        self.window_start = time.time()
        self.used = 0

    def wait_if_needed(self, tokens_needed: int) -> None:
        now = time.time()
        elapsed = now - self.window_start

        # Reset da janela após 60s
        if elapsed >= 60:
            self.window_start = now
            self.used = 0
            return

        # Se vai estourar o orçamento, espera o restante da janela
        if self.used + tokens_needed > self.limit:
            wait_for = 60 - elapsed
            if wait_for > 0:
                logger.info(f"[token-budget] aguardando {wait_for:.1f}s (limite TPM)")
                time.sleep(wait_for)
            self.window_start = time.time()
            self.used = 0

        self.used += tokens_needed


_token_budget = TokenBudget(int(os.getenv("GROQ_TPM_BUDGET", "6000")))


# ==================== Prompt ====================

SYSTEM_PROMPT = """Você é um extrator especializado em identificar OPINIÕES INDIVIDUAIS de usuários em páginas web.
Sua tarefa é extrair APENAS comentários autorais — textos escritos por uma PESSOA expressando uma EXPERIÊNCIA, OPINIÃO ou RELATO sobre um produto, serviço, marca ou pessoa.

O QUE EXTRAIR (✓):
- Reviews/avaliações de produtos ou serviços ("Comprei e adorei...", "Não recomendo porque...")
- Reclamações individuais ("Tive um problema com o atendimento...")
- Depoimentos de clientes
- Respostas/replies de outros usuários a esses comentários
- Posts de fóruns/comunidades expressando opinião pessoal

O QUE NUNCA EXTRAIR (✗):
- Estatísticas, métricas, números agregados ("A empresa recebeu 2955 reclamações", "Nota média 5.3/10", "Tempo médio de resposta: 20 dias")
- Resumos institucionais ou descrições da empresa pelo próprio site ("A empresa atende ao Reclame Aqui", "Sobre nossa missão...")
- Títulos, cabeçalhos, manchetes
- Descrições de produto fornecidas pelo vendedor ("Produto novo, frete grátis")
- FAQs, termos de uso, políticas
- Menus, navegação, propagandas, anúncios
- Botões, links, calls-to-action ("Clique aqui", "Saiba mais")
- Texto de interface do site (rótulos de campos, mensagens de status)

REGRAS DE EXTRAÇÃO:
1. Cada comentário individual = um item separado no JSON (replies também).
2. Se o autor estiver visível, use o nome exato. Se não, use "Desconhecido".
3. Se a data estiver visível, use formato ISO. Se não, omita o campo createDate.
4. Comentários com menos de 30 caracteres devem ser descartados (provavelmente não são opiniões reais).
5. Se o texto parecer estatística, métrica ou descrição institucional, NÃO inclua.
6. Se NÃO encontrar nenhum comentário individual de usuário, retorne {"comments": []}. É melhor retornar lista vazia do que extrair conteúdo irrelevante.
7. Retorne SEMPRE um JSON válido no formato {"comments": [...]}, sem explicações."""


def build_user_prompt(content: str, keyword: str) -> str:
    """Monta o prompt do usuário com o conteúdo a ser analisado."""
    return f"""Extraia todos os comentários do seguinte conteúdo de página web.
Contexto da pesquisa: "{keyword}"

CONTEÚDO:
---
{content}
---

Retorne um JSON no formato:
{{
  "comments": [
    {{
      "body": "texto exato do comentário",
      "author": "nome do autor ou 'Desconhecido'",
      "createDate": "data ISO se disponível, senão omitir"
    }}
  ]
}}

Se não encontrar comentários, retorne {{"comments": []}}."""


# ==================== Chamada à API ====================

def _parse_llm_response(raw_content: str) -> List[Dict[str, Any]]:
    """
    Tenta extrair a lista de comentários da resposta do LLM.
    Lida com JSON puro, JSON envolvido em texto, ou array direto.
    """
    if not raw_content:
        return []

    # Tentativa 1: JSON direto (caso esperado com response_format=json_object)
    try:
        data = json.loads(raw_content)
        if isinstance(data, dict) and "comments" in data:
            comments = data["comments"]
            return comments if isinstance(comments, list) else []
        if isinstance(data, list):
            return data  # Caso o modelo retorne array direto
    except json.JSONDecodeError:
        pass

    # Tentativa 2: extrair primeiro objeto JSON balanceado do texto
    import re
    # Procura o primeiro `{` e tenta encontrar o `}` correspondente
    start = raw_content.find("{")
    if start == -1:
        # Pode ser um array direto: tenta encontrar `[`
        start = raw_content.find("[")
        if start == -1:
            logger.warning(f"[llm] não foi possível parsear resposta: {raw_content[:200]}...")
            return []

    # Contador de chaves para encontrar JSON balanceado
    depth = 0
    open_char = raw_content[start]
    close_char = "}" if open_char == "{" else "]"

    end = -1
    in_string = False
    escape_next = False
    for i in range(start, len(raw_content)):
        ch = raw_content[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end > start:
        try:
            data = json.loads(raw_content[start:end])
            if isinstance(data, dict) and "comments" in data:
                comments = data["comments"]
                return comments if isinstance(comments, list) else []
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    logger.warning(f"[llm] não foi possível parsear resposta: {raw_content[:200]}...")
    return []


def call_groq(content: str, keyword: str) -> List[Dict[str, Any]]:
    """
    Chama a API do Groq para extrair comentários do conteúdo.

    Args:
        content: HTML limpo ou texto da página
        keyword: keyword da pesquisa (vai no prompt para dar contexto ao LLM)

    Returns:
        Lista de comentários (cada um com body, author, createDate opcionalmente).
        Retorna lista vazia em caso de erro.
    """
    if not GROQ_API_KEY:
        logger.error("[llm] GROQ_API_KEY não configurada")
        return []

    user_prompt = build_user_prompt(content, keyword)

    # Reserva orçamento de tokens
    estimated_tokens = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(user_prompt) + MAX_OUTPUT_TOKENS
    _token_budget.wait_if_needed(estimated_tokens)

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,  # Determinístico, queremos extração fiel
        "max_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},  # Garante JSON válido
    }

    backoff = 1.5

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            logger.warning(f"[llm] erro de rede (tentativa {attempt + 1}): {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)
            continue

        if response.status_code == 200:
            try:
                raw_content = response.json()["choices"][0]["message"]["content"]
                return _parse_llm_response(raw_content)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                logger.error(f"[llm] resposta malformada: {e}")
                return []

        if response.status_code == 429:  # Rate limit
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else backoff
            logger.warning(f"[llm] rate limit; aguardando {wait:.1f}s")
            time.sleep(wait)
            backoff = min(backoff * 2, 15)
            continue

        # Outros erros: registra e tenta novamente
        logger.warning(
            f"[llm] HTTP {response.status_code} (tentativa {attempt + 1}): {response.text[:200]}"
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, 10)

    logger.error(f"[llm] esgotou {MAX_RETRIES + 1} tentativas")
    return []


# ==================== Normalização e dedup ====================

def normalize_comments(
    raw_items: List[Dict[str, Any]],
    keyword: str,
    search: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Normaliza os comentários retornados pelo LLM para o schema esperado pelo JHipster.

    Schema final:
        {
          "keyword": str,
          "body": str,
          "author": str,
          "createDate": ISO timestamp,
          "sentiment": null,
          "search": str | null
        }
    """
    normalized = []
    seen_hashes = set()

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        body = clean_text(item.get("body", ""))
        if len(body) < MIN_BODY_LEN:
            continue

        # Deduplicação
        body_hash = sha1(body)
        if body_hash in seen_hashes:
            continue
        seen_hashes.add(body_hash)

        author = clean_text(item.get("author", "")) or "Desconhecido"
        create_date = item.get("createDate") or now_iso()

        normalized.append({
            "keyword": keyword,
            "body": body,
            "author": author,
            "createDate": create_date,
            "sentiment": None,
            "search": search,
        })

    return normalized


def extract_via_llm(
    content: str,
    keyword: str,
    search: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Pipeline completo: chama o LLM e normaliza a resposta.
    """
    if not content or not content.strip():
        return []

    raw_items = call_groq(content, keyword)
    if not raw_items:
        return []

    return normalize_comments(raw_items, keyword, search)
