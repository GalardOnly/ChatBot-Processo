"""
Pipeline RAG: pergunta -> busca vetorial -> Groq (Llama 3.3 70B) -> resposta com paginas.
"""

from typing import List, Dict, Tuple

import streamlit as st
from groq import Groq

import config
from vector import search_chunks


GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_TOKENS = 1024


@st.cache_resource
def _groq() -> Groq:
    if not config.GROQ_API_KEY:
        st.error("Configure GROQ_API_KEY em .streamlit/secrets.toml")
        st.stop()
    return Groq(api_key=config.GROQ_API_KEY)


SYSTEM_PROMPT = """Voce e um assistente juridico especializado em atuacao defensiva.
Voce responde perguntas EXCLUSIVAMENTE com base nos trechos do processo fornecidos.

Regras obrigatorias:
- Use apenas as informacoes dos trechos recuperados. Nunca invente fatos, datas, nomes ou provas.
- Sempre que possivel, mencione a pagina de origem da informacao (ex: "Na pagina 42...").
- Se a informacao nao estiver nos trechos, diga claramente: "Nao encontrei essa informacao no processo."
- Quando identificar algo util para a defesa, explique por que aquele ponto pode ser relevante.
- Quando houver duvida ou ambiguidade, recomende conferencia humana pelo defensor.
- Escreva em portugues brasileiro, linguagem clara e objetiva.
- Nunca faca afirmacoes juridicas definitivas - voce e assistente de apoio, nao o defensor."""


def answer_question(process_id: str, question: str) -> Tuple[str, List[Dict]]:
    """Retorna (resposta, lista de fontes com page_num/chunk_index/excerpt/score)."""
    chunks = search_chunks(process_id, question)

    if not chunks:
        return (
            "Nao encontrei trechos relevantes no processo para responder essa pergunta. "
            "Tente reformular a pergunta.",
            [],
        )

    context = _format_context(chunks)
    user_msg = (
        f"Trechos recuperados do processo:\n\n{context}\n\n---\n\n"
        f"Pergunta do defensor:\n{question}\n\n"
        f"Responda com base nos trechos acima. "
        f"Quando citar uma informacao, indique a pagina de origem."
    )

    response = _groq().chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    answer = response.choices[0].message.content.strip()

    sources = [
        {
            "page_num": c["page_num"],
            "chunk_index": c["chunk_index"],
            "excerpt": (c["text"][:160] + "...") if len(c["text"]) > 160 else c["text"],
            "score": round(c.get("similarity", 0.0), 4),
        }
        for c in chunks
    ]

    return answer, sources


def _format_context(chunks: List[Dict]) -> str:
    parts = []
    for i, c in enumerate(chunks, start=1):
        parts.append(f"[Trecho {i} - Pagina {c['page_num']}]\n{c['text']}")
    return "\n\n---\n\n".join(parts)
