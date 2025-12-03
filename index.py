# index.py
# API para extrair comentários com adapters, perfis Selenium, extrator genérico (JSON-LD/heurística) e LLM fallback
# Agora com modo debug: logs detalhados + dump de HTML por etapa

from dotenv import load_dotenv
load_dotenv()

import os, re, time, json, hashlib, logging, pathlib
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, quote

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from bs4 import BeautifulSoup

# -------- Config Debug / Logs --------
app = Flask(__name__)
CORS(app, resources={r"*": {"origins": "*"}})

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = app.logger

# snapshots de html
DEBUG_HTML_DIR = pathlib.Path(os.getenv("DEBUG_HTML_DIR", "./_debug_html")).resolve()
DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)

# controles (podem ser sobrescritos no body do POST)
DUMP_HTML_DEFAULT = os.getenv("DUMP_HTML", "0") == "1"       # salva snapshots de HTML?
DUMP_MAX_MB = int(os.getenv("DUMP_MAX_MB", "5"))             # limite por arquivo
LOG_PROMPTS = os.getenv("LOG_PROMPTS", "0") == "1"           # loga trechos do prompt do LLM?

# -------- Config LLM (Groq) --------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# -------- HTTP / Parsing --------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
DEFAULT_TIMEOUT = 15
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

# -------- Limites / Orçamento LLM --------
MIN_BODY_LEN = 20
USE_SELENIUM_DEFAULT = False
MAX_CANDIDATES_PER_URL = 20
MAX_INPUT_CHARS = 8000
MAX_OUTPUT_TOKENS = 256
TOKENS_PER_MIN_BUDGET = 9000
MAX_COMMENTS_PER_URL = 200

# -------- Selenium opcional --------
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from webdriver_manager.chrome import ChromeDriverManager
except Exception:
    webdriver = None

# ==================== Utils / Debug ====================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def get_domain(url: str) -> str:
    return urlparse(url).netloc.lower()

def estimate_tokens(s: str) -> int:
    return max(1, len(s) // 4)  # ~4 chars/token

class TokenBudget:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self.window_start = time.time()
        self.used = 0
    def maybe_wait(self, tokens_needed: int):
        now = time.time()
        if now - self.window_start >= 60:
            self.window_start = now
            self.used = 0
        if self.used + tokens_needed > self.per_min:
            sleep_for = 60 - (now - self.window_start)
            if sleep_for > 0:
                logger.warning(f"[budget] aguardando {sleep_for:.1f}s (tpm)")
                time.sleep(sleep_for)
            self.window_start = time.time()
            self.used = 0
        self.used += tokens_needed

token_budget = TokenBudget(TOKENS_PER_MIN_BUDGET)

def safe_write(path: pathlib.Path, data: bytes):
    try:
        if len(data) > DUMP_MAX_MB * 1024 * 1024:
            data = data[: DUMP_MAX_MB * 1024 * 1024]
        path.write_bytes(data)
    except Exception as e:
        logger.warning(f"[debug] falha ao salvar {path.name}: {e}")

def dump_html(url: str, stage: str, html: str) -> Optional[str]:
    """Salva um snapshot do HTML e retorna o nome do arquivo (para GET /debug/html/<file>)."""
    try:
        uhash = sha1(url)
        name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{stage}_{uhash}.html"
        p = DEBUG_HTML_DIR / name
        safe_write(p, html.encode("utf-8", errors="ignore"))
        logger.info(f"[dump] {stage} -> {name} ({len(html)} bytes)")
        return name
    except Exception as e:
        logger.warning(f"[dump] erro: {e}")
        return None

def snippet(s: str, n: int = 400) -> str:
    s = s.replace("\n", " ")
    return (s[:n] + "…") if len(s) > n else s

# ==================== Perfis por site: configuração ====================

HARD_DOMAINS_SELENIUM = {
    "www.youtube.com", "youtube.com",
    "www.reclameaqui.com.br", "reclameaqui.com.br",
    "produto.mercadolivre.com.br", "mercadolivre.com.br", "lista.mercadolivre.com.br"
}

SITE_PROFILES = {
    "youtube.com": {
        "click_more": [
            "tp-yt-paper-button#more-replies",
            "ytd-button-renderer#more-replies button",
            "ytd-button-renderer#more-comments button",
            "tp-yt-paper-button#more"
        ],
        "comment_blocks": ["ytd-comment-renderer #content", "ytd-comment-renderer"],
        "comment_text": ["#content-text"],
        "author": ["#author-text span", "#author-text"],
        "scroll_steps": 30
    },
    "mercadolivre.com.br": {
        "click_more": [
            "button.andes-button--quiet",
            "[data-testid='review__expand']",
        ],
        "comment_blocks": [
            "[data-review-id]",
            "[data-testid='review__comment']",
            ".ui-review-card"
        ],
        "comment_text": [
            "[data-testid='review__comment']",
            ".ui-review-card__comment, .review-content"
        ],
        "author": [
            "[data-testid='review__reviewer']",
            ".ui-review-card__author, .review-username"
        ],
        "scroll_steps": 20
    },
    "reclameaqui.com.br": {
        "click_more": [".see-more, .load-more, button[aria-label*='mais']"],
        "comment_blocks": [".complain-status-comments .comment, .comment__body, .complaint-card__comment"],
        "comment_text": [".comment__body, .complaint-card__comment__content, .comment__text"],
        "author": [".comment__author, .complaint-card__comment__author, .author"],
        "scroll_steps": 25
    }
}

# ==================== Fetchers ====================

def fetch_html_requests(url: str, dump: bool = False, debug_paths: Optional[list] = None) -> Optional[str]:
    try:
        logger.info(f"[requests] GET {url}")
        r = requests.get(url, headers=BASE_HEADERS, timeout=DEFAULT_TIMEOUT)
        logger.info(f"[requests] {url} -> {r.status_code} ({len(r.text)} bytes)")
        r.raise_for_status()
        html = r.text
        if dump:
            name = dump_html(url, "requests", html)
            if debug_paths is not None and name:
                debug_paths.append(name)
        logger.debug(f"[requests] head: {snippet(html)}")
        return html
    except Exception as e:
        logger.warning(f"[requests] {url} -> {e}")
        return None

def fetch_html_selenium(url: str, dump: bool = False, debug_paths: Optional[list] = None) -> Optional[str]:
    if webdriver is None:
        logger.warning("Selenium indisponível; usando requests()")
        return fetch_html_requests(url, dump=dump, debug_paths=debug_paths)

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1366,768")
    options.add_argument(f"user-agent={UA}")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    try:
        logger.info(f"[selenium] GET {url}")
        driver.get(url)
        time.sleep(1.0)

        # tentativa de clicar abas comuns
        try:
            cand_xpaths = [
                "//a[contains(translate(@href,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'review')]",
                "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'review')]",
                "//a[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'avalia')]",
                "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'avalia')]",
            ]
            for xp in cand_xpaths:
                els = driver.find_elements(By.XPATH, xp)
                if els:
                    logger.info(f"[selenium] click '{xp}' ({len(els)})")
                    els[0].click()
                    time.sleep(1.0)
                    break
        except Exception as e:
            logger.debug(f"[selenium] clicks ignorados: {e}")

        # scroll
        last_h = 0
        for i in range(10):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.8)
            new_h = driver.execute_script("return document.body.scrollHeight")
            logger.debug(f"[selenium] scroll {i} -> h={new_h}")
            if new_h == last_h:
                break
            last_h = new_h

        html = driver.page_source
        logger.info(f"[selenium] page_source length={len(html)}")
        if dump:
            name = dump_html(url, "selenium", html)
            if debug_paths is not None and name:
                debug_paths.append(name)
        logger.debug(f"[selenium] head: {snippet(html)}")
        return html
    except Exception as e:
        logger.warning(f"[selenium] {url} -> {e}")
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass

def fetch_html(url: str, use_selenium: Optional[bool] = None, dump: bool = False, debug_paths: Optional[list] = None) -> Tuple[Optional[str], bool]:
    use_selenium = USE_SELENIUM_DEFAULT if use_selenium is None else use_selenium
    if use_selenium:
        html = fetch_html_selenium(url, dump=dump, debug_paths=debug_paths)
        return html, True if html is not None else False
    html = fetch_html_requests(url, dump=dump, debug_paths=debug_paths)
    if html is None:
        html = fetch_html_selenium(url, dump=dump, debug_paths=debug_paths)
        return html, True if html is not None else False
    return html, False

# ==================== Selenium helpers para perfis ====================

def selenium_click_all(driver, css_selectors: List[str], max_clicks: int = 30, sleep_s: float = 0.8):
    clicks = 0
    for sel in css_selectors:
        while clicks < max_clicks:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if not els:
                    break
                for el in els[:3]:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    time.sleep(0.2)
                    el.click()
                    time.sleep(sleep_s)
                    clicks += 1
                    logger.debug(f"[selenium] clicked '{sel}' (total={clicks})")
                    if clicks >= max_clicks:
                        break
            except Exception as e:
                logger.debug(f"[selenium] click '{sel}' erro: {e}")
                break

def selenium_smart_scroll(driver, steps: int = 20, sleep_s: float = 0.8):
    last_h = 0
    for i in range(steps):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(sleep_s)
        new_h = driver.execute_script("return document.body.scrollHeight")
        logger.debug(f"[selenium] smart_scroll {i} -> h={new_h}")
        if new_h == last_h:
            break
        last_h = new_h

def extract_with_site_profile(url: str, keyword: str, search: Optional[str], use_selenium: bool,
                              dump: bool = False, debug_paths: Optional[list] = None) -> Tuple[List[Dict[str, Any]], bool]:
    domain = get_domain(url)
    prof_key = next((k for k in SITE_PROFILES.keys() if k in domain), None)
    if not prof_key:
        return [], False

    profile = SITE_PROFILES[prof_key]
    must_use_selenium = use_selenium or (domain in HARD_DOMAINS_SELENIUM)
    logger.info(f"[profile:{prof_key}] must_use_selenium={must_use_selenium}")

    if webdriver is None and must_use_selenium:
        logger.info(f"[profile:{prof_key}] Selenium indisponível; fallback requests")
        html = fetch_html_requests(url, dump=dump, debug_paths=debug_paths)
        if not html:
            return [], False
        return extract_reviews_from_jsonld(html, keyword, search), False

    if not must_use_selenium:
        html = fetch_html_requests(url, dump=dump, debug_paths=debug_paths)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            out = []
            for sel in profile.get("comment_text", []):
                found = soup.select(sel)
                logger.info(f"[profile:{prof_key}] CSS '{sel}' -> {len(found)} nós")
                for el in found:
                    body = clean_text(el.get_text(" ", strip=True))
                    if len(body) >= MIN_BODY_LEN:
                        out.append({
                            "keyword": keyword,
                            "body": body,
                            "author": "Desconhecido",
                            "createDate": now_iso(),
                            "sentiment": None,
                            "search": search
                        })
            if out:
                uniq, seen = [], set()
                for it in out:
                    h = sha1(it["body"])
                    if h not in seen:
                        seen.add(h)
                        uniq.append(it)
                logger.info(f"[profile:{prof_key}] extraídos={len(uniq)} (requests)")
                return uniq, False

    # Selenium
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1366,768")
    options.add_argument(f"user-agent={UA}")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    out: List[Dict[str, Any]] = []
    try:
        logger.info(f"[profile:{prof_key}] selenium GET {url}")
        driver.get(url)
        time.sleep(2.0)

        selenium_smart_scroll(driver, steps=profile.get("scroll_steps", 20))
        selenium_click_all(driver, profile.get("click_more", []), max_clicks=40, sleep_s=0.9)
        selenium_smart_scroll(driver, steps=profile.get("scroll_steps", 20))

        blocks_sel = profile.get("comment_blocks", [])
        text_sel = profile.get("comment_text", [])
        author_sel = profile.get("author", [])

        blocks = []
        for blk in blocks_sel:
            found = driver.find_elements(By.CSS_SELECTOR, blk)
            logger.info(f"[profile:{prof_key}] blocks '{blk}' -> {len(found)}")
            blocks.extend(found)

        def first_text(el, selectors):
            for sel in selectors:
                try:
                    sub = el.find_element(By.CSS_SELECTOR, sel)
                    t = clean_text(sub.text)
                    if t:
                        return t
                except Exception:
                    continue
            return ""

        for i, b in enumerate(blocks):
            body = first_text(b, text_sel)
            if len(body) >= MIN_BODY_LEN:
                author = first_text(b, author_sel) or "Desconhecido"
                out.append({
                    "keyword": keyword, "body": body, "author": author,
                    "createDate": now_iso(), "sentiment": None, "search": search
                })
            if i < 3 and dump:  # salva amostra do bloco renderizado (texto) em arquivo txt
                try:
                    sample = f"[BLOCK {i} BODY]\n{body}\n"
                    fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_profileblk_{sha1(url)}_{i}.txt"
                    safe_write((DEBUG_HTML_DIR / fname), sample.encode("utf-8"))
                except Exception:
                    pass

        html = driver.page_source
        if dump:
            name = dump_html(url, f"profile_{prof_key}", html)
            if debug_paths is not None and name:
                debug_paths.append(name)

        uniq, seen = [], set()
        for it in out:
            h = sha1(it["body"])
            if h not in seen:
                seen.add(h)
                uniq.append(it)
        logger.info(f"[profile:{prof_key}] extraídos={len(uniq)} (selenium)")
        return uniq, True
    except Exception as e:
        logger.warning(f"[profile:{prof_key}] erro: {e}")
        return [], True
    finally:
        try:
            driver.quit()
        except Exception:
            pass

# ==================== Extratores sem LLM ====================

def extract_reviews_from_jsonld(html: str, keyword: str, search: Optional[str]) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Dict[str, Any]] = []

    def ensure_list(x):
        if x is None: return []
        return x if isinstance(x, list) else [x]

    total_scripts = 0
    total_reviews = 0

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        total_scripts += 1
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue

        nodes = ensure_list(data)
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") in ["Product", "CreativeWork", "Service", "Thing"]:
                reviews = ensure_list(node.get("review"))
                logger.debug(f"[jsonld] @type={node.get('@type')} reviews={len(reviews)}")
                for rv in reviews:
                    if not isinstance(rv, dict) or rv.get("@type") != "Review": continue
                    body = clean_text(rv.get("reviewBody") or rv.get("description") or "")
                    author = rv.get("author")
                    if isinstance(author, dict): author = author.get("name")
                    author = clean_text(author or "Desconhecido")
                    if len(body) >= MIN_BODY_LEN:
                        total_reviews += 1
                        out.append({
                            "keyword": keyword, "body": body, "author": author or "Desconhecido",
                            "createDate": now_iso(), "sentiment": None, "search": search
                        })

            if isinstance(node, dict) and node.get("@type") == "Review":
                body = clean_text(node.get("reviewBody") or node.get("description") or "")
                author = node.get("author")
                if isinstance(author, dict): author = author.get("name")
                author = clean_text(author or "Desconhecido")
                if len(body) >= MIN_BODY_LEN:
                    total_reviews += 1
                    out.append({
                        "keyword": keyword, "body": body, "author": author or "Desconhecido",
                        "createDate": now_iso(), "sentiment": None, "search": search
                    })

            if isinstance(node, list):
                for rv in node:
                    if not isinstance(rv, dict) or rv.get("@type") != "Review": continue
                    body = clean_text(rv.get("reviewBody") or rv.get("description") or "")
                    author = rv.get("author")
                    if isinstance(author, dict): author = author.get("name")
                    author = clean_text(author or "Desconhecido")
                    if len(body) >= MIN_BODY_LEN:
                        total_reviews += 1
                        out.append({
                            "keyword": keyword, "body": body, "author": author or "Desconhecido",
                            "createDate": now_iso(), "sentiment": None, "search": search
                        })

    logger.info(f"[jsonld] scripts={total_scripts} reviews_extraidos={total_reviews}")
    # dedup
    uniq, seen = [], set()
    for it in out:
        h = sha1(it["body"])
        if h not in seen:
            seen.add(h)
            uniq.append(it)
    return uniq

COMMON_COMMENT_CLASSES = [
    "review", "comment", "opinion", "feedback",
    "ugc", "rating", "testimonial", "reply", "replies",
    "comments-list", "comment-thread"
]

def extract_comment_candidates(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    blocks = []

    for tag in soup.find_all(attrs={"itemtype": re.compile("schema.org/(Review|Comment)", re.I)}):
        blocks.append(str(tag))

    for tag in soup.find_all(attrs={"itemprop": re.compile("(reviewBody|comment|description)", re.I)}):
        blocks.append(str(tag))

    for tag in soup.find_all(attrs={"data-testid": re.compile("(review|comment)", re.I)}):
        blocks.append(str(tag))
    for tag in soup.find_all(attrs={"data-test": re.compile("(review|comment)", re.I)}):
        blocks.append(str(tag))

    for cls in COMMON_COMMENT_CLASSES:
        for tag in soup.find_all(class_=re.compile(cls, re.I)):
            blocks.append(str(tag))

    for tag in soup.select("article, li, div, section"):
        classes = " ".join(tag.get("class", []))
        if re.search(r"(review|comment|opinion|rating|feedback|reply)", classes or "", re.I):
            blocks.append(str(tag))

    long_ps = [p for p in soup.find_all("p") if len(p.get_text(strip=True)) >= MIN_BODY_LEN]
    if long_ps:
        container = BeautifulSoup("<div></div>", "html.parser").div
        for p in long_ps[:40]:
            container.append(p)
        blocks.append(str(container))

    seen, unique = set(), []
    for b in blocks:
        text = BeautifulSoup(b, "html.parser").get_text(" ", strip=True)
        h = sha1(clean_text(text)[:600])
        if h not in seen:
            seen.add(h)
            unique.append(b)

    logger.info(f"[heuristic] candidatos={len(unique)} (antes do corte)")
    return unique[:MAX_CANDIDATES_PER_URL]

# ==================== LLM helpers ====================

def sanitize_chunk_for_llm(html_chunk: str) -> str:
    try:
        txt = BeautifulSoup(html_chunk, "html.parser").get_text("\n", strip=True)
    except Exception:
        txt = html_chunk
    txt = clean_text(txt)
    if len(txt) > MAX_INPUT_CHARS:
        txt = txt[:MAX_INPUT_CHARS]
    return txt

def build_prompt(keyword: str, html_chunk: str) -> str:
    text_chunk = sanitize_chunk_for_llm(html_chunk)
    return f"""
Você receberá um TRECHO DE TEXTO derivado de HTML que contém COMENTÁRIOS e possivelmente RESPOSTAS (replies).
Extraia CADA comentário (incluindo replies) e retorne APENAS um JSON válido (array) no formato:

[
  {{
    "keyword": "{keyword}",
    "body": "<texto do comentário>",
    "author": "<nome do autor ou 'Desconhecido'>",
    "createDate": "{now_iso()}",
    "sentiment": null,
    "search": null
  }}
]

Regras IMPORTANTES:
- Inclua replies como comentários separados (não precisa árvore).
- "body" sem HTML.
- Autor desconhecido => "Desconhecido".
- Se não encontrar comentários, retorne [].

TEXTO:
---
{text_chunk}
---
""".strip()

def call_groq(prompt: str, max_retries: int = 3) -> List[Dict[str, Any]]:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY não definida.")
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    # orçamento simples
    input_tokens = estimate_tokens(prompt) + MAX_OUTPUT_TOKENS
    token_budget.maybe_wait(input_tokens)

    if LOG_PROMPTS:
        logger.debug(f"[groq] prompt: {snippet(prompt, 1000)}")

    backoff = 1.0
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
        except Exception as e:
            logger.warning(f"[groq] erro de rede: {e}")
            time.sleep(backoff); backoff = min(backoff * 2, 8.0)
            continue

        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            logger.debug(f"[groq] len(content)={len(content)} sample={snippet(content)}")
            try:
                return json.loads(content)
            except Exception:
                m = re.search(r"(\[\s*\{.*?\}\s*\])", content, flags=re.S)
                if m:
                    try:
                        return json.loads(m.group(1))
                    except Exception:
                        pass
            break

        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            sleep_for = float(ra) if ra else backoff
            logger.warning(f"[groq] 429; aguardando {sleep_for:.1f}s e tentando de novo…")
            time.sleep(sleep_for); backoff = min(backoff * 2, 10.0)
            continue

        logger.warning(f"[groq] HTTP {resp.status_code}: {resp.text[:240]}")
        time.sleep(backoff); backoff = min(backoff * 2, 8.0)

    return []

def normalize_items(items: List[Dict[str, Any]], keyword: str, search: Optional[str]) -> List[Dict[str, Any]]:
    norm, seen = [], set()
    for it in items:
        body = clean_text(it.get("body", ""))
        if len(body) < MIN_BODY_LEN:
            continue
        author = clean_text(it.get("author", "")) or "Desconhecido"
        item = {
            "keyword": keyword, "body": body, "author": author,
            "createDate": it.get("createDate") or now_iso(),
            "sentiment": None, "search": search
        }
        h = sha1(body)
        if h not in seen:
            seen.add(h)
            norm.append(item)
    logger.info(f"[normalize] itens_validos={len(norm)}")
    return norm

# ==================== Adapters ====================

def is_reddit(url: str) -> bool:
    d = get_domain(url)
    return d.endswith("reddit.com") or d.endswith("redd.it")

def reddit_fetch_comments(url: str, keyword: str, search: Optional[str]) -> List[Dict[str, Any]]:
    if not url.endswith(".json"):
        if "/comments/" in url:
            api_url = url.split("?")[0].rstrip("/") + ".json"
        else:
            api_url = url.rstrip("/") + ".json"
    else:
        api_url = url
    try:
        logger.info(f"[reddit] GET {api_url}")
        r = requests.get(api_url, headers={"User-Agent": UA}, timeout=DEFAULT_TIMEOUT)
        logger.info(f"[reddit] {api_url} -> {r.status_code}")
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"[reddit] falha: {e}")
        return []

    out: List[Dict[str, Any]] = []

    def walk(node):
        if not isinstance(node, dict): return
        kind = node.get("kind")
        data_ = node.get("data", {}) if isinstance(node.get("data"), dict) else {}
        if kind == "t1":
            body = clean_text(data_.get("body", ""))
            author = data_.get("author") or "Desconhecido"
            if len(body) >= MIN_BODY_LEN:
                out.append({
                    "keyword": keyword, "body": body, "author": author,
                    "createDate": now_iso(), "sentiment": None, "search": search
                })
            replies = data_.get("replies")
            if isinstance(replies, dict):
                for child in replies.get("data", {}).get("children", []):
                    walk(child)
        elif kind in ("Listing", None):
            for child in (node.get("data", {}) or {}).get("children", []):
                walk(child)

    try:
        if isinstance(data, list) and len(data) > 1:
            for child in data[1].get("data", {}).get("children", []):
                walk(child)
        else:
            walk(data)
    except Exception as e:
        logger.warning(f"[reddit] parse recursivo falhou: {e}")

    uniq, seen = [], set()
    for it in out:
        h = sha1(it["body"])
        if h not in seen:
            seen.add(h)
            uniq.append(it)
    logger.info(f"[reddit] extraídos={len(uniq)}")
    return uniq

def is_shopee(url: str) -> bool:
    return "shopee." in get_domain(url)

_SHOPEE_ID_RE = re.compile(r"/i\.(\d+)\.(\d+)", re.I)

def parse_shopee_ids(url: str) -> Optional[Dict[str, int]]:
    m = _SHOPEE_ID_RE.search(url)
    if not m: return None
    try:
        return {"shopid": int(m.group(1)), "itemid": int(m.group(2))}
    except Exception:
        return None

def shopee_fetch_reviews(url: str, keyword: str, search: Optional[str], limit: int = 50, pages: int = 2) -> List[Dict[str, Any]]:
    ids = parse_shopee_ids(url)
    if not ids: return []
    base = f"https://{get_domain(url)}"
    out: List[Dict[str, Any]] = []

    for p in range(pages):
        offset = p * limit
        params = {
            "itemid": ids["itemid"], "shopid": ids["shopid"],
            "limit": str(limit), "offset": str(offset),
            "type": "0", "filter": "0", "flag": "1", "sort": "0"
        }
        try:
            logger.info(f"[shopee] GET ratings offset={offset}")
            resp = requests.get(base + "/api/v2/item/get_ratings", params=params,
                                headers={**BASE_HEADERS, "Referer": url}, timeout=DEFAULT_TIMEOUT)
            logger.info(f"[shopee] -> {resp.status_code}")
            if resp.status_code != 200:
                logger.warning(f"[shopee] HTTP {resp.status_code}: {resp.text[:200]}")
                break
            data = resp.json()
        except Exception as e:
            logger.warning(f"[shopee] erro: {e}")
            break

        ratings = (data or {}).get("data", {}).get("ratings") or []
        logger.info(f"[shopee] ratings={len(ratings)}")
        for r in ratings:
            body = clean_text((r.get("comment") or "") + " " + (r.get("comment_reply") or ""))
            author = clean_text((r.get("author_username") or r.get("author_shopid") or "")) or "Desconhecido"
            if len(body) >= MIN_BODY_LEN:
                out.append({
                    "keyword": keyword, "body": body, "author": author,
                    "createDate": now_iso(), "sentiment": None, "search": search
                })
        if not ratings: break

    uniq, seen = [], set()
    for it in out:
        h = sha1(it["body"])
        if h not in seen:
            seen.add(h)
            uniq.append(it)
    logger.info(f"[shopee] extraídos={len(uniq)}")
    return uniq

# ==================== Pipeline genérico via LLM ====================

def generic_extract_via_llm(url: str, keyword: str, search: Optional[str], use_selenium: bool,
                            max_candidates: int, max_comments: int,
                            dump: bool = False, debug_paths: Optional[list] = None) -> Tuple[List[Dict[str, Any]], bool, str]:
    html, used_sel = fetch_html(url, use_selenium, dump=dump, debug_paths=debug_paths)
    if not html:
        return [], used_sel, "none"

    jld = extract_reviews_from_jsonld(html, keyword, search)
    if jld:
        logger.info(f"[generic] JSON-LD extraiu {len(jld)}")
        return jld[:max_comments], used_sel, "jsonld"

    candidates = extract_comment_candidates(html)
    if not candidates:
        logger.info("[generic] 0 candidatos da heurística")
        return [], used_sel, "none"

    candidates = candidates[:max_candidates]
    logger.info(f"[generic] candidatos_enviados_llm={len(candidates)} (máx={max_candidates})")
    out_for_url: List[Dict[str, Any]] = []
    used_llm = False

    for i, chunk in enumerate(candidates):
        prompt = build_prompt(keyword, chunk)
        items: List[Dict[str, Any]] = []
        try:
            items = call_groq(prompt)
            used_llm = True
        except Exception as e:
            logger.error(f"[groq] falha: {e}")
        if items:
            logger.debug(f"[generic] LLM retornou {len(items)} itens para chunk {i}")
            out_for_url.extend(normalize_items(items, keyword, search))
        if len(out_for_url) >= max_comments:
            logger.info("[generic] atingiu maxCommentsPerUrl")
            break

    if out_for_url:
        src = "llm" if used_llm else "heuristic"
        logger.info(f"[generic] extraídos={len(out_for_url)} source={src}")
        return out_for_url[:max_comments], used_sel, src
    return [], used_sel, "none"

# ==================== Endpoints ====================

@app.route("/")
def root():
    return "Hello Comments LLM World!"

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})

@app.route("/debug/html/<path:fname>", methods=["GET"])
def debug_get_html(fname: str):
    """Abra no navegador: http://127.0.0.1:5000/debug/html/<nome-do-arquivo>"""
    return send_from_directory(DEBUG_HTML_DIR, fname, mimetype="text/html")

@app.route("/comments/extract", methods=["POST"])
def comments_extract():
    global LOG_PROMPTS  # <<< declare aqui, antes de qualquer uso

    """
    Body JSON:
    {
      "urls": ["https://...","https://..."],
      "keyword": "nome-produto-ou-empresa",
      "search": "termo-de-busca-opcional",
      "useSelenium": false,
      "maxCandidates": 20,
      "maxCommentsPerUrl": 200,
      "debug": { "dumpHtml": true, "logPrompts": false }
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    urls = data.get("urls") or []
    keyword = data.get("keyword")
    search = data.get("search")
    use_selenium = bool(data.get("useSelenium", USE_SELENIUM_DEFAULT))
    max_candidates = int(data.get("maxCandidates", MAX_CANDIDATES_PER_URL))
    max_comments = int(data.get("maxCommentsPerUrl", MAX_COMMENTS_PER_URL))

    debug_cfg = data.get("debug") or {}
    dump_html_flag = bool(debug_cfg.get("dumpHtml", DUMP_HTML_DEFAULT))
    log_prompts_flag = bool(debug_cfg.get("logPrompts", LOG_PROMPTS))

    # permite ligar LOG_PROMPTS por request
    old_log_prompts = LOG_PROMPTS
    LOG_PROMPTS = log_prompts_flag

    if not urls or not isinstance(urls, list) or not keyword:
        return jsonify({"error": "Parâmetros inválidos: 'urls' (lista) e 'keyword' são obrigatórios."}), 400

    logger.info(f"[extract] urls={len(urls)} keyword='{keyword}' useSelenium={use_selenium} dumpHtml={dump_html_flag}")

    all_out: List[Dict[str, Any]] = []
    per_url_stats: List[Dict[str, Any]] = []

    try:
        for url in urls:
            start_t = time.time()
            domain = get_domain(url)
            extracted: List[Dict[str, Any]] = []
            source = "none"
            used_selenium_flag = False
            used_llm_flag = False
            debug_paths: List[str] = []

            # 0) Perfil por site
            prof_out, prof_used_sel = extract_with_site_profile(
                url, keyword, search, use_selenium, dump=dump_html_flag, debug_paths=debug_paths
            )
            if prof_out:
                extracted = prof_out
                source = "profile"
                used_selenium_flag = prof_used_sel

            # 1) Adapters
            if not extracted and is_reddit(url):
                extracted = reddit_fetch_comments(url, keyword, search)
                source = "reddit"
            elif not extracted and is_shopee(url):
                extracted = shopee_fetch_reviews(url, keyword, search, limit=50, pages=2)
                source = "shopee"

            # 2) Genérico
            if not extracted:
                if not GROQ_API_KEY:
                    logger.info("[extract] sem GROQ_API_KEY; rodando apenas JSON-LD/heurística básica")
                    html, used_sel = fetch_html(url, use_selenium, dump=dump_html_flag, debug_paths=debug_paths)
                    used_selenium_flag |= bool(used_sel)
                    extracted = extract_reviews_from_jsonld(html or "", keyword, search) if html else []
                    source = "jsonld" if extracted else "none"
                    if not extracted and html:
                        soup = BeautifulSoup(html, "html.parser")
                        ps = [clean_text(p.get_text(strip=True)) for p in soup.find_all("p")]
                        for t in ps:
                            if len(t) >= 120:
                                extracted.append({
                                    "keyword": keyword, "body": t, "author": "Desconhecido",
                                    "createDate": now_iso(), "sentiment": None, "search": search
                                })
                                if len(extracted) >= max_comments: break
                        if extracted and source == "none":
                            source = "heuristic"
                else:
                    extracted, used_sel, src = generic_extract_via_llm(
                        url, keyword, search, use_selenium, max_candidates, max_comments,
                        dump=dump_html_flag, debug_paths=debug_paths
                    )
                    used_selenium_flag |= bool(used_sel)
                    source = src
                    used_llm_flag = (src == "llm")

            elapsed = round(time.time() - start_t, 2)
            all_out.extend(extracted)

            stat = {
                "url": url,
                "ok": True,
                "domain": domain,
                "comments": len(extracted),
                "elapsedSec": elapsed,
                "source": source,
                "usedSelenium": used_selenium_flag,
                "usedLLM": used_llm_flag
            }
            if dump_html_flag and debug_paths:
                from urllib.parse import quote
                stat["debugHtml"] = [f"/debug/html/{quote(f)}" for f in debug_paths]
            per_url_stats.append(stat)
            logger.info(f"[extract] {domain} -> {len(extracted)} comentários | source={source} | {elapsed}s")

        return jsonify({
            "status": "success",
            "message": f"{len(all_out)} comentários extraídos",
            "stats": per_url_stats,
            "comments": all_out
        })
    finally:
        # restaura a flag global para não “vazar” entre requests
        LOG_PROMPTS = old_log_prompts


# ==================== Run ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    logger.info(f"Starting Flask on http://127.0.0.1:{port} (LOG_LEVEL={LOG_LEVEL})")
    app.run(host="0.0.0.0", port=port, debug=True)
