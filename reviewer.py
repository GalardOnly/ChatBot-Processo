"""
Camada de revisao automatica (LLM-as-judge) das respostas geradas pela IA.

Fluxo:
1. chat.py gera a resposta inicial (Groq)
2. review_ai_answer(...) chama um SEGUNDO LLM que audita criticamente
3. Se aprovada, retorna corrected_answer (eventualmente igual a original)
4. Se reprovada / risco alto / erro, retorna SAFE_FALLBACK

Decisao de seguranca: FAIL-CLOSED. Qualquer falha tecnica na revisao
bloqueia a resposta original e exibe mensagem segura. Defensor nunca ve
resposta nao revisada.

Logs nunca contem o conteudo do processo, da pergunta ou da resposta -
apenas IDs internos, risk_level e flags estruturados.
"""

import json
import time
import uuid
from typing import Dict, List, Optional

import streamlit as st
from groq import Groq

import config
import security


# =========================================================================
# Configuracao do reviewer
# =========================================================================

REVIEWER_MODEL       = "llama-3.3-70b-versatile"
REVIEWER_MAX_TOKENS  = 1800
REVIEWER_TEMPERATURE = 0.0   # deterministico para auditoria
REVIEWER_TIMEOUT_S   = 30

# Maximo de chars de contexto enviado ao reviewer (controla custo/latencia)
_MAX_CTX_CHARS    = 18000
_MAX_ANSWER_CHARS = 6000

# Resposta segura quando bloqueamos / erro tecnico
SAFE_FALLBACK = (
    "Nao foi possivel gerar uma resposta confiavel para esta solicitacao "
    "com base nos trechos recuperados do processo. Recomendo conferir "
    "manualmente as folhas correspondentes ou reformular a pergunta. "
    "Esta resposta deve sempre ser revisada por um defensor humano."
)


REVIEW_SYSTEM_PROMPT = """Voce e um AUDITOR juridico independente. Sua tarefa e
revisar criticamente respostas geradas por outro assistente de IA sobre
processos judiciais, ANTES de essas respostas serem exibidas a um defensor
publico.

A revisao deve ser RIGOROSA e CONSERVADORA. Em caso de duvida sobre se um
fato esta no processo, REPROVE. Falsos positivos sao preferiveis a deixar
passar alucinacao para o defensor.

CRITERIOS DE AVALIACAO:

1. ANCORAGEM NO CONTEXTO
   - Cada nome, data, crime, valor, pagina, decisao ou fato citado na resposta
     deve aparecer literalmente nos trechos fornecidos.
   - Citacoes de fls. devem corresponder a paginas reais dos trechos.
   - Se a resposta menciona qualquer coisa NAO encontravel no contexto,
     issue: "fato_nao_ancorado_no_processo".

2. ALUCINACAO DE JURISPRUDENCIA
   - Acordaos, sumulas, REsp, HC, AgRg, numeros de processo de tribunal
     so podem ser citados se aparecerem no bloco de jurisprudencia anexada.
   - Issue: "jurisprudencia_inventada".

3. CONTRADICOES INTERNAS
   - A resposta nao pode afirmar duas coisas incompativeis entre si.
   - Issue: "contradicao_interna".

4. EXCESSO DE CERTEZA
   - Linguagem absoluta sobre fatos ambiguos ou pontos sem prova clara.
   - Conclusoes juridicas definitivas (ex: 'o reu e culpado', 'a prescricao
     consumou-se') sem todos os elementos no processo.
   - Issue: "excesso_de_certeza" ou "conclusao_juridica_sem_fundamento".

5. CONFUSAO / GENERICIDADE
   - Texto vago, repetitivo, generico ou que nao responde a pergunta.
   - Issue: "linguagem_confusa_ou_generica".

6. PII DESNECESSARIA
   - CPF, RG, endereco completo, telefone de pessoas privadas expostos
     SEM necessidade defensiva clara.
   - Issue: "pii_exposta_sem_necessidade".

7. FORMATO
   - Se a tarefa exigia tabela, lista, secoes - verificar conformidade.
   - Issue: "formato_nao_respeitado".

8. AVISO DE REVISAO HUMANA
   - Para conclusoes juridicas, a resposta deve indicar (ou voce deve
     inserir) aviso de que precisa ser revisada por defensor humano.
   - Se faltar, ajustar em corrected_answer.

ACAO DO REVISOR:

- Se a resposta esta SOLIDA (sem nenhum issue), copie a resposta tal qual
  em corrected_answer, marque approved=true, risk_level="low".

- Se tem PROBLEMAS PEQUENOS (formato, aviso ausente), CORRIJA voce mesmo
  em corrected_answer, marque approved=true, risk_level="low" ou "medium".

- Se tem PROBLEMAS GRAVES (fato inventado, jurisprudencia inventada,
  conclusao sem base), marque approved=false, risk_level="high",
  liste todos os issues, e em corrected_answer escreva apenas:
  "Nao foi possivel validar a resposta gerada. Revisar manualmente os
  trechos do processo."

CONFIDENCE:
- "high": tem certeza da sua avaliacao
- "medium": avaliacao razoavel mas pode ter passado algo
- "low": contexto ambiguo, melhor o defensor revisar

FORMATO DE SAIDA OBRIGATORIO (JSON estrito):
{
  "approved": true|false,
  "risk_level": "low" | "medium" | "high",
  "issues": ["lista", "de", "issues_encontrados"],
  "corrected_answer": "texto final que sera exibido ao defensor",
  "confidence": "low" | "medium" | "high"
}

Responda APENAS o JSON, sem cercas de codigo, sem prefacio, sem markdown."""


# =========================================================================
# Cliente Groq dedicado (reaproveita o mesmo client_resource)
# =========================================================================

@st.cache_resource
def _reviewer_groq() -> Groq:
    if not config.GROQ_API_KEY:
        st.error("Configure GROQ_API_KEY em .streamlit/secrets.toml")
        st.stop()
    return Groq(api_key=config.GROQ_API_KEY, timeout=REVIEWER_TIMEOUT_S)


# =========================================================================
# Funcao publica
# =========================================================================

def review_ai_answer(
    raw_answer: str,
    context_chunks: List[Dict],
    question: Optional[str] = None,
    task_type: str = "chat",
    jurisprudence_chunks: Optional[List[Dict]] = None,
) -> Dict:
    """
    Revisa criticamente a resposta antes de exibir ao usuario.

    Args:
        raw_answer: resposta bruta gerada pelo LLM principal.
        context_chunks: trechos do processo usados como base.
        question: pergunta do defensor (quando aplicavel).
        task_type: tipo da tarefa ('chat', 'summary', 'probatoria',
                   'prescricao', 'audiencia', 'teses' etc).
        jurisprudence_chunks: trechos de jurisprudencia anexada (opcional).

    Returns:
        dict {approved, risk_level, issues, corrected_answer, confidence}

    Fail-closed: erro tecnico -> approved=False, risk=high, fallback seguro.
    """
    # ID de auditoria para correlacionar logs sem expor conteudo
    audit_id = uuid.uuid4().hex[:12]

    if not raw_answer or not raw_answer.strip():
        return _block_result(
            audit_id,
            "high",
            ["resposta_vazia"],
            reason="raw answer empty",
        )

    try:
        review_input = _build_review_input(
            question=question,
            context_chunks=context_chunks,
            raw_answer=raw_answer,
            task_type=task_type,
            jurisprudence_chunks=jurisprudence_chunks,
        )
    except Exception as e:
        security.safe_log_error(
            f"[reviewer:{audit_id}] erro montando input", e
        )
        return _block_result(audit_id, "high", ["erro_tecnico_na_revisao"])

    try:
        start_t = time.monotonic()
        response = _reviewer_groq().chat.completions.create(
            model=REVIEWER_MODEL,
            max_tokens=REVIEWER_MAX_TOKENS,
            temperature=REVIEWER_TEMPERATURE,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {"role": "user",   "content": review_input},
            ],
        )
        elapsed = time.monotonic() - start_t
    except Exception as e:
        security.safe_log_error(f"[reviewer:{audit_id}] LLM falhou", e)
        return _block_result(audit_id, "high", ["erro_tecnico_na_revisao"])

    raw_json = (response.choices[0].message.content or "").strip()
    try:
        review = json.loads(raw_json)
    except Exception as e:
        security.safe_log_error(
            f"[reviewer:{audit_id}] JSON invalido", e
        )
        return _block_result(audit_id, "high", ["resposta_revisor_invalida"])

    review = _normalize_and_validate(review)
    review = _apply_block_rules(review, audit_id, raw_answer)

    # Log estruturado SEM conteudo
    security.safe_log_warning(
        f"[reviewer:{audit_id}]",
        f"task={task_type} approved={review['approved']} "
        f"risk={review['risk_level']} issues={','.join(review['issues']) or 'none'} "
        f"confidence={review['confidence']} took_ms={int(elapsed * 1000)}"
    )

    return review


# =========================================================================
# Helpers internos
# =========================================================================

def _build_review_input(
    question: Optional[str],
    context_chunks: List[Dict],
    raw_answer: str,
    task_type: str,
    jurisprudence_chunks: Optional[List[Dict]] = None,
) -> str:
    """Monta o input do reviewer, truncando para nao estourar contexto."""
    ctx_parts: List[str] = []
    used = 0
    for i, c in enumerate(context_chunks, start=1):
        text = c.get("text", "")
        page = c.get("page_num", "?")
        chunk_str = f"[Trecho {i} - fls. {page}]\n{text}"
        if used + len(chunk_str) > _MAX_CTX_CHARS:
            break
        ctx_parts.append(chunk_str)
        used += len(chunk_str)

    ctx_block = "\n\n---\n\n".join(ctx_parts) if ctx_parts else "(nenhum trecho)"

    juris_block = ""
    if jurisprudence_chunks:
        juris_lines = []
        used_j = 0
        for j, jc in enumerate(jurisprudence_chunks, start=1):
            title = jc.get("title", "?")
            court = jc.get("court", "")
            case = jc.get("case_number", "")
            ref = " / ".join(filter(None, [court, case]))
            j_str = f"[Ref {j}] {title}" + (f" - {ref}" if ref else "") + \
                    f"\n{jc.get('text','')}"
            if used_j + len(j_str) > 4000:
                break
            juris_lines.append(j_str)
            used_j += len(j_str)
        juris_block = "\n\n---\n\n".join(juris_lines)

    answer_trunc = raw_answer
    if len(answer_trunc) > _MAX_ANSWER_CHARS:
        answer_trunc = answer_trunc[:_MAX_ANSWER_CHARS] + "...[truncada para revisao]"

    pieces = [
        f"TIPO DA TAREFA: {task_type}",
    ]
    if question:
        q_trunc = question if len(question) < 2000 else question[:2000] + "..."
        pieces.append(f"\nPERGUNTA DO DEFENSOR:\n{q_trunc}")
    pieces.append(
        "\n=== INICIO DOS TRECHOS DO PROCESSO (autos reais) ===\n"
        f"{ctx_block}\n=== FIM DOS TRECHOS ==="
    )
    if juris_block:
        pieces.append(
            "\n=== INICIO DA JURISPRUDENCIA ANEXADA ===\n"
            f"{juris_block}\n=== FIM DA JURISPRUDENCIA ==="
        )
    pieces.append(
        "\n=== RESPOSTA A REVISAR (gerada por outro LLM) ===\n"
        f"{answer_trunc}\n=== FIM DA RESPOSTA ==="
    )
    pieces.append(
        "\nAvalie a resposta acima segundo os criterios do system prompt. "
        "Retorne APENAS o JSON estrito especificado."
    )
    return "\n".join(pieces)


_VALID_LEVELS = {"low", "medium", "high"}


def _normalize_and_validate(review: Dict) -> Dict:
    """Garante campos obrigatorios e tipos corretos (defensive)."""
    if not isinstance(review, dict):
        review = {}

    approved = bool(review.get("approved", False))
    risk = str(review.get("risk_level", "high")).strip().lower()
    if risk not in _VALID_LEVELS:
        risk = "high"

    issues = review.get("issues", [])
    if not isinstance(issues, list):
        issues = [str(issues)]
    issues = [str(x)[:120] for x in issues if x][:20]

    corrected = review.get("corrected_answer", "")
    if not isinstance(corrected, str):
        corrected = str(corrected)
    corrected = corrected.strip()

    confidence = str(review.get("confidence", "low")).strip().lower()
    if confidence not in _VALID_LEVELS:
        confidence = "low"

    return {
        "approved": approved,
        "risk_level": risk,
        "issues": issues,
        "corrected_answer": corrected,
        "confidence": confidence,
    }


def _apply_block_rules(review: Dict, audit_id: str, raw_answer: str) -> Dict:
    """
    Regras de bloqueio:
    - approved=False -> exibe SAFE_FALLBACK
    - risk_level='high' -> exibe SAFE_FALLBACK
    - corrected_answer vazio -> exibe SAFE_FALLBACK e marca approved=False
    """
    if not review["approved"] or review["risk_level"] == "high":
        review["approved"] = False
        review["corrected_answer"] = SAFE_FALLBACK
        if "bloqueado_pela_revisao" not in review["issues"]:
            review["issues"].append("bloqueado_pela_revisao")
        return review

    if not review["corrected_answer"]:
        review["approved"] = False
        review["risk_level"] = "high"
        review["corrected_answer"] = SAFE_FALLBACK
        review["issues"].append("corrected_answer_vazio")
        return review

    return review


def _block_result(audit_id: str, risk: str, issues: List[str],
                  reason: str = "") -> Dict:
    """Resultado bloqueante padrao quando ha erro tecnico."""
    security.safe_log_warning(
        f"[reviewer:{audit_id}] bloqueio",
        f"risk={risk} issues={','.join(issues)} reason={reason}"
    )
    return {
        "approved": False,
        "risk_level": risk,
        "issues": issues,
        "corrected_answer": SAFE_FALLBACK,
        "confidence": "low",
    }
