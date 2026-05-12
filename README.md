# mining-comments-api (v3 - LLM-first)

API de mineração de comentários do **Proraf Social**. Recebe URLs e retorna comentários estruturados extraídos via LLM (Llama 3.3 70B no Groq).

## Arquitetura

```
URL → Fetcher (requests/ScrapingBee) → HTML
       ↓
       Tenta JSON-LD (Schema.org Review)? → sim → retorna
       ↓ não
       Limpa HTML (remove scripts/CSS/etc) → texto/HTML enxuto
       ↓
       Envia ao Groq (Llama 3.3 70B) → JSON estruturado
       ↓
       Normaliza + Deduplica → resposta JSON
```

### Estrutura do código

```
api/
  __init__.py
  index.py              # Flask app + endpoint /comments/extract
  fetcher.py            # requests + ScrapingBee
  cleaner.py            # limpeza de HTML para o LLM
  jsonld_extractor.py   # extrai Schema.org Review (rota rápida)
  llm_extractor.py      # chamada ao Groq + parsing + dedup
  utils.py              # helpers comuns
requirements.txt
vercel.json
.env.example
```

## Configuração

### 1. Obter chaves de API (todas gratuitas)

- **Groq** (obrigatório): https://console.groq.com/keys
- **ScrapingBee** (opcional, para Google Reviews e sites com JS): https://www.scrapingbee.com/

### 2. Configurar variáveis de ambiente

Copie `.env.example` para `.env` e preencha:

```bash
cp .env.example .env
# Editar .env com suas chaves
```

### 3. Instalar dependências

```bash
python -m venv venv
source venv/bin/activate   # Linux/Mac
# venv\Scripts\activate    # Windows
pip install -r requirements.txt
```

## Rodar localmente

```bash
python -m api.index
```

API disponível em `http://localhost:5000`.

## Deploy no Vercel

1. Faça push deste diretório para um repositório Git.
2. Importe no Vercel.
3. Configure as variáveis de ambiente no painel do Vercel:
   - `GROQ_API_KEY`
   - `SCRAPINGBEE_API_KEY` (opcional)
4. Vercel detecta `vercel.json` e faz o deploy automático.

## Uso

### `POST /comments/extract`

**Body:**

```json
{
  "urls": ["https://www.reclameaqui.com.br/empresa/xxx/"],
  "keyword": "nome-da-empresa-ou-produto",
  "search": "opcional",
  "forceJs": false,
  "maxCommentsPerUrl": 200
}
```

**Parâmetros:**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `urls` | `string[]` | sim | Lista de URLs para extrair comentários |
| `keyword` | `string` | sim | Termo da pesquisa (ecoado em cada comentário) |
| `search` | `string` | não | Termo de busca opcional |
| `forceJs` | `boolean` | não | Força uso do ScrapingBee mesmo em sites estáticos |
| `maxCommentsPerUrl` | `number` | não | Limite de comentários por URL (default: 200) |

**Resposta:**

```json
{
  "status": "success",
  "totalComments": 5,
  "stats": [
    {
      "url": "https://...",
      "domain": "reclameaqui.com.br",
      "source": "llm",
      "fetcher": "requests",
      "elapsedSec": 4.32,
      "commentsCount": 5
    }
  ],
  "comments": [
    {
      "keyword": "nome-da-empresa-ou-produto",
      "body": "Texto do comentário extraído...",
      "author": "Maria Silva",
      "createDate": "2024-03-15T14:22:00Z",
      "sentiment": null,
      "search": null
    }
  ]
}
```

**Campos de cada comentário:**

| Campo | Tipo | Descrição |
|---|---|---|
| `keyword` | `string` | Keyword passada na requisição |
| `body` | `string` | Texto do comentário |
| `author` | `string` | Autor ou "Desconhecido" |
| `createDate` | `string` (ISO) | Data do comentário se encontrada, senão data da coleta |
| `sentiment` | `null` | Sempre `null` (preenchido pela API de análise de sentimentos) |
| `search` | `string \| null` | Search passado na requisição |

**Campo `source` (em stats):**

| Valor | Significado |
|---|---|
| `jsonld` | Comentários extraídos de Schema.org JSON-LD (sem usar LLM) |
| `llm` | Comentários extraídos pelo Groq (Llama 3.3 70B) |
| `none` | Nenhum comentário encontrado |
| `error` | Erro inesperado (detalhes em `error`) |

**Campo `fetcher` (em stats):**

| Valor | Significado |
|---|---|
| `requests` | HTML obtido com `requests` puro |
| `scrapingbee` | HTML obtido via ScrapingBee (renderização de JS) |
| `none` | Não conseguiu obter HTML |

## Endpoints auxiliares

- `GET /` — Informações da API
- `GET /health` — Health check com status de configuração

## Limites e considerações

- **Vercel free tier:** timeout de 10s por requisição. Para muitas URLs ou páginas pesadas, considere chamar o endpoint múltiplas vezes com sublistas.
- **Groq free tier:** ~6000 tokens/minuto no Llama 3.3 70B. O `TokenBudget` interno respeita esse limite.
- **ScrapingBee free tier:** 1000 chamadas/mês.
- **JavaScript rendering:** apenas sites listados em `utils.needs_javascript_rendering` usam ScrapingBee automaticamente. Outros podem ser forçados via `forceJs: true`.

## Mudanças em relação às versões anteriores

Esta versão (v3) reescreve do zero a API. Principais diferenças:

| Aspecto | v1/v2 | v3 |
|---|---|---|
| Estratégia principal | Adapters por site + LLM como fallback | LLM-first com JSON-LD como otimização |
| Selenium | Tentava usar (não funciona no Vercel) | Removido — substituído por ScrapingBee |
| Linhas de código | ~1000 | ~600 (modular) |
| Sites cobertos | YouTube, Reddit, Shopee, etc. (não usados) | Genérico via LLM (qualquer site público) |
| Endpoints | `/scraping`, `/crawling`, `/extract` | Apenas `/comments/extract` |
| Resiliência a mudanças no HTML | Baixa (seletores CSS frágeis) | Alta (LLM lê contexto) |
