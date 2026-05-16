"""
Embeddings (Voyage AI - voyage-law-2) + busca via Supabase pgvector.
voyage-law-2 e especializado em texto juridico, 1024 dim, contexto 16K.
"""

import time
from typing import List, Dict, Iterable

import streamlit as st
import voyageai

import config
from db import client


VOYAGE_MODEL = "voyage-law-2"
VOYAGE_BATCH_SIZE = 64
VOYAGE_PAUSE_S = 1.0


@st.cache_resource
def _voyage() -> voyageai.Client:
    if not config.VOYAGE_API_KEY:
        st.error("Configure VOYAGE_API_KEY em .streamlit/secrets.toml")
        st.stop()
    return voyageai.Client(api_key=config.VOYAGE_API_KEY)


# ---------------------------------------------------------------------------
# Indexacao
# ---------------------------------------------------------------------------

def embed_and_store(process_id: str, chunks: List[Dict], progress_cb=None) -> int:
    """
    Gera embeddings em batch (Voyage aceita ate 128 textos por chamada) e
    insere os chunks na tabela `chunks` do Supabase.
    progress_cb opcional: funcao recebendo (concluidos, total).
    """
    total = len(chunks)
    done = 0
    rows_buffer: List[Dict] = []

    for batch in _batched(chunks, VOYAGE_BATCH_SIZE):
        texts = [c["text"] for c in batch]
        result = _voyage().embed(texts=texts, model=VOYAGE_MODEL, input_type="document")

        for chunk, emb in zip(batch, result.embeddings):
            rows_buffer.append({
                "process_id": process_id,
                "chunk_index": chunk["chunk_index"],
                "page_num": chunk["page_num"],
                "word_count": chunk["word_count"],
                "text": chunk["text"],
                "embedding": emb,
            })

        done += len(batch)
        if progress_cb:
            progress_cb(done, total)

        time.sleep(VOYAGE_PAUSE_S)

    # Insert em chunks de 100 linhas pra nao estourar payload do PostgREST
    for db_batch in _batched(rows_buffer, 100):
        client().table("chunks").insert(db_batch).execute()

    return total


# ---------------------------------------------------------------------------
# Busca
# ---------------------------------------------------------------------------

def search_chunks(process_id: str, query: str, top_k: int = None) -> List[Dict]:
    """Chama a funcao RPC `match_chunks` (cosine similarity em pgvector)."""
    top_k = top_k or config.TOP_K_CHUNKS

    query_emb = _voyage().embed(
        texts=[query],
        model=VOYAGE_MODEL,
        input_type="query",
    ).embeddings[0]

    res = client().rpc("match_chunks", {
        "query_embedding": query_emb,
        "match_process_id": process_id,
        "match_count": top_k,
    }).execute()

    return res.data or []


# ---------------------------------------------------------------------------

def _batched(items: Iterable, size: int):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------------------
# Biblioteca de Jurisprudencia (Voyage + pgvector via RPC match_jurisprudence)
# ---------------------------------------------------------------------------

def embed_and_store_jurisprudence(jurisprudence_id: str, chunks: List[Dict], progress_cb=None) -> int:
    """
    Gera embeddings em batch e insere em jurisprudence_chunks.
    chunks: lista de {"chunk_index", "text", "word_count"}.
    """
    total = len(chunks)
    done = 0
    rows: List[Dict] = []

    for batch in _batched(chunks, VOYAGE_BATCH_SIZE):
        texts = [c["text"] for c in batch]
        result = _voyage().embed(texts=texts, model=VOYAGE_MODEL, input_type="document")
        for chunk, emb in zip(batch, result.embeddings):
            rows.append({
                "jurisprudence_id": jurisprudence_id,
                "chunk_index": chunk["chunk_index"],
                "word_count": chunk.get("word_count", 0),
                "text": chunk["text"],
                "embedding": emb,
            })
        done += len(batch)
        if progress_cb:
            progress_cb(done, total)
        time.sleep(VOYAGE_PAUSE_S)

    for db_batch in _batched(rows, 100):
        client().table("jurisprudence_chunks").insert(db_batch).execute()
    return total


def search_jurisprudence(query: str, top_k: int = 5) -> List[Dict]:
    """
    Busca jurisprudencia relevante (pessoal + global do usuario) por similaridade.
    Retorna lista de chunks com metadados da peca (titulo, tribunal, etc).
    """
    query_emb = _voyage().embed(
        texts=[query],
        model=VOYAGE_MODEL,
        input_type="query",
    ).embeddings[0]

    res = client().rpc("match_jurisprudence", {
        "query_embedding": query_emb,
        "match_count": top_k,
    }).execute()

    return res.data or []
