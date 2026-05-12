"""
cleaner.py
Limpa o HTML antes de enviá-lo ao LLM.

Por que limpar antes?
- Um HTML típico tem 80-95% de "ruído" (scripts, CSS, navegação, footer).
- Cada token enviado ao LLM custa tempo de inferência (e dinheiro, no caso pago).
- Groq tem limite de 8192 tokens de contexto no Llama-3.3-70b-versatile,
  então mandar HTML cru frequentemente extrapola o limite.

Estratégia:
1. Remover tags claramente irrelevantes: <script>, <style>, <noscript>, <svg>, <link>, <meta>.
2. Remover atributos pesados (style, class, on*, data-*) que não ajudam a identificar comentários.
3. Preservar estrutura semântica: <article>, <section>, <div>, <p>, <li>, <blockquote>.
4. Cortar o HTML a um tamanho máximo seguro.
"""

import re
from bs4 import BeautifulSoup, Comment

# Tags que nunca contêm comentários e podem ser removidas
TAGS_TO_REMOVE = [
    "script",
    "style",
    "noscript",
    "svg",
    "link",
    "meta",
    "head",
    "iframe",
    "nav",
    "footer",
    "header",
    "form",
    "button",
    "input",
    "select",
    "option",
]

# Atributos a manter (todos os outros são removidos)
ATTRS_TO_KEEP = {"itemtype", "itemprop", "datetime"}

# Tamanho máximo do HTML limpo enviado ao LLM (em caracteres)
# Llama-3.3-70b tem ~8k tokens de contexto, ~4 chars/token, deixando margem para o prompt.
MAX_CLEANED_CHARS = 24000


def remove_unwanted_tags(soup: BeautifulSoup) -> None:
    """Remove tags inteiras que não contêm comentários relevantes."""
    for tag_name in TAGS_TO_REMOVE:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Remover comentários HTML
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()


def strip_attributes(soup: BeautifulSoup) -> None:
    """Remove atributos que não ajudam a identificar comentários."""
    for tag in soup.find_all(True):
        new_attrs = {}
        for k, v in tag.attrs.items():
            if k in ATTRS_TO_KEEP:
                new_attrs[k] = v
        tag.attrs = new_attrs


def clean_html_for_llm(html: str, max_chars: int = MAX_CLEANED_CHARS) -> str:
    """
    Pipeline completo de limpeza para envio ao LLM.

    Args:
        html: HTML bruto
        max_chars: Tamanho máximo do output (será cortado se exceder)

    Returns:
        HTML limpo, com estrutura preservada mas sem ruído.
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    remove_unwanted_tags(soup)
    strip_attributes(soup)

    cleaned = str(soup)
    # Compacta espaços em branco entre tags
    cleaned = re.sub(r">\s+<", "><", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)

    # Corta se exceder o tamanho máximo
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]

    return cleaned


def extract_text_only(html: str, max_chars: int = MAX_CLEANED_CHARS) -> str:
    """
    Versão alternativa: extrai apenas o texto, descartando totalmente o HTML.
    Útil como fallback se o LLM tiver dificuldade com HTML preservado.
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    remove_unwanted_tags(soup)

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if len(text) > max_chars:
        text = text[:max_chars]

    return text
