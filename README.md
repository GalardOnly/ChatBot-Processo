# Defensor IA

Assistente jurídico para Defensoria Pública. Sobe um PDF de processo, indexa o
conteúdo com embeddings especializados em direito (Voyage `voyage-law-2`) e
permite **conversar com os autos** via LLM (Groq Llama 3.3 70B).

Stack:

- **Streamlit** — UI + lógica em Python puro
- **Supabase** — Auth (e-mail/senha) e Postgres + pgvector (storage persistente com RLS)
- **Voyage AI** (`voyage-law-2`) — embeddings 1024D especializados em texto jurídico
- **Groq** (`llama-3.3-70b-versatile`) — geração de respostas
- **PyMuPDF** — extração de texto dos PDFs

---

## Estrutura

```
.
├── app.py                # entry point Streamlit (UI: login, upload, chat)
├── config.py             # leitura de secrets / env vars
├── pdf.py                # extração + chunking dos PDFs
├── vector.py             # embeddings Voyage + busca pgvector via RPC
├── chat.py               # RAG + chamada Groq
├── db.py                 # cliente Supabase (auth + CRUD)
├── supabase_schema.sql   # schema SQL: tabelas, índices, RLS, função match_chunks
├── requirements.txt
├── .streamlit/
│   ├── config.toml             # tema + uploads
│   └── secrets.toml.example    # template das chaves
└── .env.example
```

---

## Setup local (15 minutos)

### 1. Supabase

1. Crie um projeto grátis em [supabase.com](https://supabase.com) (free tier dá 500 MB)
2. No menu lateral, vá em **SQL Editor → New query**
3. Cole o conteúdo de `supabase_schema.sql` e clique em **Run**
4. Verifique em **Table editor** que existem as tabelas `processes`, `chunks`, `messages`
5. Em **Settings → API**, copie:
   - `Project URL` → vai em `SUPABASE_URL`
   - `anon public` key → vai em `SUPABASE_ANON_KEY`
6. (Opcional) Em **Authentication → Providers → Email**, desative *"Confirm email"*
   se quiser pular o e-mail de confirmação para testes

### 2. Chaves de API

- **Voyage AI** — [voyageai.com](https://www.voyageai.com/) → API Keys (free tier: 50M tokens)
- **Groq** — [console.groq.com](https://console.groq.com/) → API Keys (free tier generoso)

### 3. Secrets

Copie o template e preencha as chaves:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Edite `.streamlit/secrets.toml`:

```toml
SUPABASE_URL = "https://SEU_PROJETO.supabase.co"
SUPABASE_ANON_KEY = "eyJ..."
VOYAGE_API_KEY = "pa-..."
GROQ_API_KEY = "gsk_..."

MAX_FILE_SIZE_MB = 50
CHUNK_SIZE = 400
CHUNK_OVERLAP = 50
TOP_K_CHUNKS = 6
```

### 4. Rodar

```bash
python -m venv .venv
.\.venv\Scripts\activate          # Windows
# source .venv/bin/activate         # Linux/macOS

pip install -r requirements.txt
streamlit run app.py
```

Abra `http://localhost:8501`. Crie uma conta, suba um PDF, converse com ele.

---

## Deploy no Streamlit Community Cloud (grátis)

1. Suba este repositório no GitHub
2. Acesse [share.streamlit.io](https://share.streamlit.io) e faça login com GitHub
3. **New app** → escolha o repo, branch `main`, arquivo `app.py`
4. Em **Advanced settings → Secrets**, cole o conteúdo do seu `secrets.toml`
5. Deploy. Em ~2 minutos o app está no ar com URL pública

O Streamlit Cloud reinicia o app quando ele fica inativo, mas o **storage é
persistente** porque toda a informação está no Supabase — não no disco do app.

---

## Como funciona o RAG

1. **Upload** — `pdf.py` lê o PDF com PyMuPDF, limpa ruído (hifenação, colunas) e
   divide em chunks de ~400 palavras preservando a página de origem.
2. **Indexação** — `vector.py` chama Voyage `voyage-law-2` em batches de 64
   textos, gera embeddings de 1024 dimensões e insere na tabela `chunks` do
   Supabase (com índice HNSW para busca rápida).
3. **Pergunta** — `vector.search_chunks` embeda a pergunta com `input_type=query`
   e chama a função SQL `match_chunks` (cosine similarity em pgvector) que
   retorna os 6 trechos mais similares **do processo atual**.
4. **Resposta** — `chat.py` monta um prompt com os trechos recuperados + a
   pergunta e chama Groq Llama 3.3 70B com instrução de citar a página de origem.

---

## Segurança e privacidade

- **RLS (Row Level Security)** ativo em todas as tabelas: cada defensor só vê
  seus próprios processos, chunks e mensagens. Mesmo com a `anon key` exposta,
  as policies do Supabase impedem acesso cruzado.
- Senhas são gerenciadas pelo Supabase Auth (bcrypt).
- O PDF original **não é armazenado** — só o texto extraído fica no banco.
- **LGPD**: o defensor pode apagar um processo a qualquer momento (cascade
  remove chunks e mensagens). Para apagar a conta inteira, abra um ticket no
  Supabase ou implemente um botão de "Apagar conta" chamando
  `auth.admin.deleteUser` via Edge Function.

---

## Limites e custos

| Recurso              | Free tier                          | Observação                          |
|----------------------|-------------------------------------|--------------------------------------|
| Supabase             | 500 MB DB, 50K MAU                 | suficiente para ~10K páginas        |
| Voyage `voyage-law-2`| 50M tokens grátis (total)          | ~50 mil páginas de processo         |
| Groq Llama 3.3 70B   | ~30 req/min, 14.400 req/dia        | mais que suficiente para 1 defensor |
| Streamlit Cloud      | 1 app público / conta              | app fica público mas com auth       |

Para uso intensivo (mais de um defensor, dezenas de processos por dia), considere
mover para o tier pago do Supabase ($25/mês, dá 8 GB) e Voyage ($0,12/M tokens).

---

## Próximos passos sugeridos

- [ ] OCR automático para PDFs escaneados (`ocrmypdf` antes da extração)
- [ ] Re-ranking com `voyage-rerank-2` para melhorar precisão dos trechos
- [ ] Botão "Apagar conta" + Edge Function chamando `auth.admin.deleteUser`
- [ ] Export de conversas em DOCX (`python-docx`)
- [ ] Modo "compare processos" (busca cross-process)
