# index.py
# API para extrair comentários: adapters por domínio + extrator genérico (JSON-LD, heurística) + LLM fallback

from dotenv import load_dotenv
load_dotenv()  # carrega variáveis do .env

import os, re, time, json, hashlib, logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup

# -------- Config Flask / Logs --------
app = Flask(__name__)
CORS(app, resources={r"*": {"origins": "*"}})
logging.basicConfig(level=logging.INFO)
logger = app.logger

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
USE_SELENIUM_DEFAULT = False   # usar Selenium só quando pedir
MAX_CANDIDATES_PER_URL = 20    # quantos blocos mandamos ao LLM
MAX_INPUT_CHARS = 8000         # corte do texto do chunk
MAX_OUTPUT_TOKENS = 256        # resposta curta (JSON)
TOKENS_PER_MIN_BUDGET = 9000   # orçamento por minuto (ajuste conforme sua cota)
MAX_COMMENTS_PER_URL = 200     # evita explodir resposta

# -------- Selenium opcional --------
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from webdriver_manager.chrome import ChromeDriverManager
except Exception:
    webdriver = None

# ==================== Utils ====================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def get_domain(url: str) -> str:
    return urlparse(url).netloc.lower()

def estimate_tokens(s: str) -> int:
    # Aproximação: ~4 chars por token
    return max(1, len(s) // 4)

class TokenBudget:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self.window_start = time.time()
        self.used = 0

    def maybe_wait(self, tokens_needed: int):
        now = time.time()
        # janela móvel de 60s
        if now - self.window_start >= 60:
            self.window_start = now
            self.used = 0
        if self.used + tokens_needed > self.per_min:
            sleep_for = 60 - (now - self.window_start)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self.window_start = time.time()
            self.used = 0
        self.used += tokens_needed

token_budget = TokenBudget(TOKENS_PER_MIN_BUDGET)

# ==================== Fetchers ====================

def fetch_html_requests(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=BASE_HEADERS, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"[requests] {url} -> {e}")
        return None

def fetch_html_selenium(url: str) -> Optional[str]:
    if webdriver is None:
        logger.warning("Selenium indisponível; usando requests()")
        return fetch_html_requests(url)

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1366,768")
    options.add_argument(f"user-agent={UA}")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    try:
        driver.get(url)

        # Tentativa best-effort de clicar abas de review/avaliações
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
                    els[0].click()
                    time.sleep(1.2)
                    break
        except Exception:
            pass

        # Scroll controlado
        last_h = 0
        for _ in range(10):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.8)
            new_h = driver.execute_script("return document.body.scrollHeight")
            if new_h == last_h:
                break
            last_h = new_h

        return driver.page_source
    except Exception as e:
        logger.warning(f"[selenium] {url} -> {e}")
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass

def fetch_html(url: str, use_selenium: Optional[bool] = None) -> Optional[str]:
    use_selenium = USE_SELENIUM_DEFAULT if use_selenium is None else use_selenium
    html = fetch_html_selenium(url) if use_selenium else fetch_html_requests(url)
    if html is None and not use_selenium:
        html = fetch_html_selenium(url)
    return html

# ==================== Extratores sem LLM ====================

# 1) JSON-LD Reviews genérico (muitos sites expõem @type: "Review" em script[type=application/ld+json])
def extract_reviews_from_jsonld(html: str, keyword: str, search: Optional[str]) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Dict[str, Any]] = []

    def ensure_list(x):
        if x is None:
            return []
        return x if isinstance(x, list) else [x]

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue

        nodes = ensure_list(data)
        for node in nodes:
            # Alguns sites colocam um "Product" com campo "review" (array ou objeto)
            if isinstance(node, dict) and node.get("@type") in ["Product", "CreativeWork", "Service", "Thing"]:
                reviews = ensure_list(node.get("review"))
                for rv in reviews:
                    if not isinstance(rv, dict):
                        continue
                    if rv.get("@type") != "Review":
                        continue
                    body = clean_text(rv.get("reviewBody") or rv.get("description") or "")
                    author = rv.get("author")
                    if isinstance(author, dict):
                        author = author.get("name")
                    author = clean_text(author or "Desconhecido")
                    if len(body) >= MIN_BODY_LEN:
                        out.append({
                            "keyword": keyword,
                            "body": body,
                            "author": author or "Desconhecido",
                            "createDate": now_iso(),
                            "sentiment": None,
                            "search": search
                        })
            # Ou o script é diretamente um Review/array de Review
            if isinstance(node, dict) and node.get("@type") == "Review":
                body = clean_text(node.get("reviewBody") or node.get("description") or "")
                author = node.get("author")
                if isinstance(author, dict):
                    author = author.get("name")
                author = clean_text(author or "Desconhecido")
                if len(body) >= MIN_BODY_LEN:
                    out.append({
                        "keyword": keyword,
                        "body": body,
                        "author": author or "Desconhecido",
                        "createDate": now_iso(),
                        "sentiment": None,
                        "search": search
                    })
            if isinstance(node, list):
                for rv in node:
                    if not isinstance(rv, dict):
                        continue
                    if rv.get("@type") != "Review":
                        continue
                    body = clean_text(rv.get("reviewBody") or rv.get("description") or "")
                    author = rv.get("author")
                    if isinstance(author, dict):
                        author = author.get("name")
                    author = clean_text(author or "Desconhecido")
                    if len(body) >= MIN_BODY_LEN:
                        out.append({
                            "keyword": keyword,
                            "body": body,
                            "author": author or "Desconhecido",
                            "createDate": now_iso(),
                            "sentiment": None,
                            "search": search
                        })
    # dedup por corpo
    uniq, seen = [], set()
    for it in out:
        h = sha1(it["body"])
        if h not in seen:
            seen.add(h)
            uniq.append(it)
    return uniq

# 2) Heurística HTML (schema.org/Review + classes comuns)
COMMON_COMMENT_CLASSES = [
    "review", "comment", "opinion", "feedback",
    "ugc", "rating", "testimonial"
]

def extract_comment_candidates(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    blocks = []

    # schema.org Review (itemtype)
    for tag in soup.find_all(attrs={"itemtype": re.compile("schema.org/Review", re.I)}):
        blocks.append(str(tag))

    # classes comuns
    for cls in COMMON_COMMENT_CLASSES:
        for tag in soup.find_all(class_=re.compile(cls, re.I)):
            blocks.append(str(tag))

    # tags gerais com classes de review
    for tag in soup.select("article, li, div"):
        classes = " ".join(tag.get("class", []))
        if re.search(r"(review|comment|opinion|rating|feedback)", classes or "", re.I):
            blocks.append(str(tag))

    # fallback: parágrafos longos
    long_ps = [p for p in soup.find_all("p") if len(p.get_text(strip=True)) >= MIN_BODY_LEN]
    if long_ps:
        container = BeautifulSoup("<div></div>", "html.parser").div
        for p in long_ps[:30]:
            container.append(p)
        blocks.append(str(container))

    # dedup
    seen, unique = set(), []
    for b in blocks:
        text = BeautifulSoup(b, "html.parser").get_text(" ", strip=True)
        h = sha1(clean_text(text)[:500])
        if h not in seen:
            seen.add(h)
            unique.append(b)

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
Você receberá um TRECHO DE TEXTO derivado de HTML que provavelmente contém COMENTÁRIOS/AVALIAÇÕES de usuários.
Extraia cada comentário identificado e retorne APENAS um JSON válido (array) no formato:

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
- Apenas JSON válido (sem explicações antes ou depois).
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
        raise RuntimeError("GROQ_API_KEY não definida (coloque no .env ou var de ambiente).")

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }

    # orçamento simples: entrada + folga de saída
    input_tokens = estimate_tokens(prompt) + MAX_OUTPUT_TOKENS
    token_budget.maybe_wait(input_tokens)

    backoff = 1.0
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
        except Exception as e:
            logger.warning(f"[groq] erro de rede: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 8.0)
            continue

        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
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
            time.sleep(sleep_for)
            backoff = min(backoff * 2, 10.0)
            continue

        logger.warning(f"[groq] HTTP {resp.status_code}: {resp.text[:240]}")
        time.sleep(backoff)
        backoff = min(backoff * 2, 8.0)

    return []

def normalize_items(items: List[Dict[str, Any]], keyword: str, search: Optional[str]) -> List[Dict[str, Any]]:
    norm, seen = [], set()
    for it in items:
        body = clean_text(it.get("body", ""))
        if len(body) < MIN_BODY_LEN:
            continue
        author = clean_text(it.get("author", "")) or "Desconhecido"
        item = {
            "keyword": keyword,
            "body": body,
            "author": author,
            "createDate": it.get("createDate") or now_iso(),
            "sentiment": None,
            "search": search
        }
        h = sha1(body)
        if h not in seen:
            seen.add(h)
            norm.append(item)
    return norm

# ==================== Adapters por domínio ====================

# --- Reddit (.json do post) ---
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
        r = requests.get(api_url, headers={"User-Agent": UA}, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"[reddit] falha em {api_url}: {e}")
        return []

    out = []
    try:
        children = data[1]["data"]["children"]
        for c in children:
            if c.get("kind") != "t1":
                continue
            body = clean_text(c["data"].get("body", ""))
            author = c["data"].get("author") or "Desconhecido"
            if len(body) >= MIN_BODY_LEN:
                out.append({
                    "keyword": keyword,
                    "body": body,
                    "author": author,
                    "createDate": now_iso(),
                    "sentiment": None,
                    "search": search
                })
            # (Opcional) percorrer replies recursivamente
    except Exception:
        pass

    return out

# --- Shopee (API de ratings do front) ---
def is_shopee(url: str) -> bool:
    return "shopee." in get_domain(url)

_SHOPEE_ID_RE = re.compile(r"/i\.(\d+)\.(\d+)", re.I)

def parse_shopee_ids(url: str) -> Optional[Dict[str, int]]:
    m = _SHOPEE_ID_RE.search(url)
    if not m:
        return None
    try:
        return {"shopid": int(m.group(1)), "itemid": int(m.group(2))}
    except Exception:
        return None

def shopee_fetch_reviews(url: str, keyword: str, search: Optional[str], limit: int = 50, pages: int = 2) -> List[Dict[str, Any]]:
    ids = parse_shopee_ids(url)
    if not ids:
        return []
    base = f"https://{get_domain(url)}"
    out: List[Dict[str, Any]] = []

    for p in range(pages):
        offset = p * limit
        params = {
            "itemid": ids["itemid"],
            "shopid": ids["shopid"],
            "limit": str(limit),
            "offset": str(offset),
            "type": "0",
            "filter": "0",
            "flag": "1",
            "sort": "0"
        }
        try:
            resp = requests.get(
                base + "/api/v2/item/get_ratings",
                params=params,
                headers={**BASE_HEADERS, "Referer": url},
                timeout=DEFAULT_TIMEOUT
            )
            if resp.status_code != 200:
                logger.warning(f"[shopee] HTTP {resp.status_code}: {resp.text[:200]}")
                break
            data = resp.json()
        except Exception as e:
            logger.warning(f"[shopee] erro: {e}")
            break

        ratings = (data or {}).get("data", {}).get("ratings") or []
        for r in ratings:
            body = clean_text((r.get("comment") or "") + " " + (r.get("comment_reply") or ""))
            author = clean_text((r.get("author_username") or r.get("author_shopid") or "")) or "Desconhecido"
            if len(body) >= MIN_BODY_LEN:
                out.append({
                    "keyword": keyword,
                    "body": body,
                    "author": author,
                    "createDate": now_iso(),
                    "sentiment": None,
                    "search": search
                })

        if not ratings:
            break

    # dedup
    uniq, seen = [], set()
    for it in out:
        h = sha1(it["body"])
        if h not in seen:
            seen.add(h)
            uniq.append(it)
    return uniq

# ==================== Pipeline genérico via LLM ====================

def generic_extract_via_llm(url: str, keyword: str, search: Optional[str], use_selenium: bool,
                            max_candidates: int, max_comments: int) -> List[Dict[str, Any]]:
    html = fetch_html(url, use_selenium)
    if not html:
        return []

    # 1) Tentar JSON-LD antes do LLM (barato e robusto)
    jld = extract_reviews_from_jsonld(html, keyword, search)
    if jld:
        return jld[:max_comments]

    # 2) Heurística HTML -> LLM
    candidates = extract_comment_candidates(html)
    if not candidates:
        return []

    candidates = candidates[:max_candidates]
    out_for_url: List[Dict[str, Any]] = []

    for chunk in candidates:
        prompt = build_prompt(keyword, chunk)
        items: List[Dict[str, Any]] = []
        try:
            items = call_groq(prompt)
        except Exception as e:
            logger.error(f"[groq] falha: {e}")
        if items:
            out_for_url.extend(normalize_items(items, keyword, search))

        if len(out_for_url) >= max_comments:
            break

    # dedup final
    unique, seen = [], set()
    for it in out_for_url:
        h = sha1(it["body"])
        if h not in seen:
            seen.add(h)
            unique.append(it)
    return unique[:max_comments]

# ==================== Endpoints ====================

@app.route("/")
def root():
    return "Hello Comments LLM World!"

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})

@app.route("/comments/extract", methods=["POST"])
def comments_extract():
    """
    Body JSON:
    {
      "urls": ["https://...","https://..."],
      "keyword": "nome-produto-ou-empresa",
      "search": "termo-de-busca-opcional",
      "useSelenium": false,           // opcional (default False)
      "maxCandidates": 20,            // opcional (limite de blocos por URL)
      "maxCommentsPerUrl": 200        // opcional (teto de comentários por URL)
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    urls = data.get("urls") or []
    keyword = data.get("keyword")
    search = data.get("search")
    use_selenium = bool(data.get("useSelenium", USE_SELENIUM_DEFAULT))
    max_candidates = int(data.get("maxCandidates", MAX_CANDIDATES_PER_URL))
    max_comments = int(data.get("maxCommentsPerUrl", MAX_COMMENTS_PER_URL))

    if not urls or not isinstance(urls, list) or not keyword:
        return jsonify({"error": "Parâmetros inválidos: 'urls' (lista) e 'keyword' são obrigatórios."}), 400

    all_out: List[Dict[str, Any]] = []
    per_url_stats: List[Dict[str, Any]] = []

    for url in urls:
        start_t = time.time()
        domain = get_domain(url)
        extracted: List[Dict[str, Any]] = []

        # 1) Adapters (não usam LLM)
        if is_reddit(url):
            extracted = reddit_fetch_comments(url, keyword, search)
        elif is_shopee(url):
            extracted = shopee_fetch_reviews(url, keyword, search, limit=50, pages=2)

        # 2) Se ainda vazio, pipeline genérico (JSON-LD -> heurística -> LLM)
        if not extracted:
            # Se for usar LLM, exige chave
            if not GROQ_API_KEY:
                logger.info("Sem GROQ_API_KEY; rodando APENAS extratores sem LLM.")
                html = fetch_html(url, use_selenium)
                extracted = extract_reviews_from_jsonld(html or "", keyword, search) if html else []
                if not extracted and html:
                    # como fallback final sem LLM, tentar heurística e extrair <p> como "comentário"
                    # (ruidoso, mas melhor que nada em alguns cenários controlados)
                    soup = BeautifulSoup(html, "html.parser")
                    ps = [clean_text(p.get_text(strip=True)) for p in soup.find_all("p")]
                    for t in ps:
                        if len(t) >= 120:  # bem conservador
                            extracted.append({
                                "keyword": keyword,
                                "body": t,
                                "author": "Desconhecido",
                                "createDate": now_iso(),
                                "sentiment": None,
                                "search": search
                            })
                            if len(extracted) >= max_comments:
                                break
            else:
                extracted = generic_extract_via_llm(url, keyword, search, use_selenium, max_candidates, max_comments)

        elapsed = round(time.time() - start_t, 2)
        all_out.extend(extracted)

        per_url_stats.append({
            "url": url,
            "ok": True,
            "domain": domain,
            "comments": len(extracted),
            "elapsedSec": elapsed,
            "usedLLM": bool(not (is_reddit(url) or is_shopee(url)) and GROQ_API_KEY and len(extracted) > 0)
        })

    return jsonify({
        "status": "success",
        "message": f"{len(all_out)} comentários extraídos",
        "stats": per_url_stats,
        "comments": all_out
    })

# ==================== Run ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    logger.info(f"Starting Flask on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
