"""
Testes da camada de revisao automatica (reviewer.review_ai_answer).

Como rodar:
    pip install pytest
    pytest tests/ -v

Os testes mockam o cliente Groq do reviewer.py para nao gastar API real.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# Garante que streamlit.cache_resource nao quebre fora do contexto Streamlit
@pytest.fixture(autouse=True)
def _stub_streamlit(monkeypatch):
    """Stub minimo de streamlit para o reviewer poder importar."""
    import sys, types
    if "streamlit" not in sys.modules:
        st_mod = types.ModuleType("streamlit")
        st_mod.cache_resource = lambda f: f
        st_mod.cache_data = lambda *a, **kw: (lambda f: f)
        st_mod.error = lambda *a, **kw: None
        st_mod.stop = lambda: None
        sys.modules["streamlit"] = st_mod


@pytest.fixture
def chunks_example():
    """Trechos de processo fictícios para usar nos testes."""
    return [
        {"page_num": 12, "text": "Em 14 de marco de 2023, o reu Joao Silva foi denunciado pelo art. 155 do CP."},
        {"page_num": 45, "text": "Audiencia de instrucao designada para 10 de novembro de 2024."},
        {"page_num": 78, "text": "A testemunha Maria Santos relatou ter visto o reu na cena do crime."},
    ]


def _mock_review_response(payload: dict):
    """Cria um mock do retorno do Groq com JSON serializado."""
    msg = MagicMock()
    msg.content = json.dumps(payload, ensure_ascii=False)
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ---------------------------------------------------------------------------
# 1. Resposta com fato inventado deve ser REJEITADA
# ---------------------------------------------------------------------------

def test_rejeita_fato_inventado(chunks_example):
    import reviewer

    # Resposta cita "Pedro Silva" que NAO esta nos chunks
    raw = "O reu Pedro Silva foi denunciado em 2020, conforme fls. 12."

    judge_payload = {
        "approved": False,
        "risk_level": "high",
        "issues": ["fato_nao_ancorado_no_processo", "data_incorreta"],
        "corrected_answer": "Nao foi possivel validar a resposta gerada. Revisar manualmente.",
        "confidence": "high",
    }

    with patch.object(reviewer, "_reviewer_groq") as mock_client_fn:
        mock_client_fn.return_value.chat.completions.create.return_value = \
            _mock_review_response(judge_payload)

        result = reviewer.review_ai_answer(
            raw_answer=raw,
            context_chunks=chunks_example,
            question="Quem e o reu?",
            task_type="chat",
        )

    assert result["approved"] is False
    assert result["risk_level"] == "high"
    assert result["corrected_answer"] == reviewer.SAFE_FALLBACK
    assert "bloqueado_pela_revisao" in result["issues"]


# ---------------------------------------------------------------------------
# 2. Resposta sem base no contexto deve ser REJEITADA
# ---------------------------------------------------------------------------

def test_rejeita_resposta_sem_contexto(chunks_example):
    import reviewer

    # Resposta totalmente desconectada dos chunks
    raw = (
        "A jurisprudencia do STF (HC 999.999/SP, Min. Fictico) afirma que "
        "o trafico privilegiado nao admite substituicao por penas restritivas."
    )

    judge_payload = {
        "approved": False,
        "risk_level": "high",
        "issues": ["jurisprudencia_inventada", "fato_nao_ancorado_no_processo"],
        "corrected_answer": "Nao foi possivel validar.",
        "confidence": "high",
    }

    with patch.object(reviewer, "_reviewer_groq") as mock_client_fn:
        mock_client_fn.return_value.chat.completions.create.return_value = \
            _mock_review_response(judge_payload)

        result = reviewer.review_ai_answer(
            raw_answer=raw,
            context_chunks=chunks_example,
            question="Qual a tese aplicavel?",
            task_type="teses",
        )

    assert result["approved"] is False
    assert result["risk_level"] == "high"
    assert result["corrected_answer"] == reviewer.SAFE_FALLBACK
    # Issues do reviewer devem estar la
    assert "jurisprudencia_inventada" in result["issues"]


# ---------------------------------------------------------------------------
# 3. Resposta valida deve ser APROVADA e retornar corrected_answer
# ---------------------------------------------------------------------------

def test_aprova_resposta_ancorada(chunks_example):
    import reviewer

    raw = (
        "Joao Silva foi denunciado pelo art. 155 do CP em 14/03/2023 (fls. 12). "
        "A audiencia esta marcada para 10/11/2024 (fls. 45)."
    )

    judge_payload = {
        "approved": True,
        "risk_level": "low",
        "issues": [],
        "corrected_answer": raw + "\n\n_Esta resposta deve ser revisada por defensor humano._",
        "confidence": "high",
    }

    with patch.object(reviewer, "_reviewer_groq") as mock_client_fn:
        mock_client_fn.return_value.chat.completions.create.return_value = \
            _mock_review_response(judge_payload)

        result = reviewer.review_ai_answer(
            raw_answer=raw,
            context_chunks=chunks_example,
            question="Resumo do processo",
            task_type="summary",
        )

    assert result["approved"] is True
    assert result["risk_level"] == "low"
    # Corrected_answer pode incluir o aviso adicionado pelo reviewer
    assert "Joao Silva" in result["corrected_answer"]
    assert result["corrected_answer"] != reviewer.SAFE_FALLBACK


# ---------------------------------------------------------------------------
# 4. Falha tecnica deve impedir exibicao (fail-closed)
# ---------------------------------------------------------------------------

def test_fail_closed_em_erro_de_LLM(chunks_example):
    import reviewer

    with patch.object(reviewer, "_reviewer_groq") as mock_client_fn:
        mock_client_fn.return_value.chat.completions.create.side_effect = \
            RuntimeError("conexao perdida")

        result = reviewer.review_ai_answer(
            raw_answer="qualquer resposta",
            context_chunks=chunks_example,
            question="?",
            task_type="chat",
        )

    assert result["approved"] is False
    assert result["risk_level"] == "high"
    assert result["corrected_answer"] == reviewer.SAFE_FALLBACK
    assert "erro_tecnico_na_revisao" in result["issues"]


def test_fail_closed_em_json_invalido(chunks_example):
    import reviewer

    bad_resp = MagicMock()
    bad_resp.choices = [MagicMock(message=MagicMock(content="isso nao e JSON"))]

    with patch.object(reviewer, "_reviewer_groq") as mock_client_fn:
        mock_client_fn.return_value.chat.completions.create.return_value = bad_resp

        result = reviewer.review_ai_answer(
            raw_answer="resposta",
            context_chunks=chunks_example,
            question="?",
            task_type="chat",
        )

    assert result["approved"] is False
    assert result["risk_level"] == "high"
    assert "resposta_revisor_invalida" in result["issues"]


# ---------------------------------------------------------------------------
# 5. Resposta vazia da IA principal deve ser bloqueada antes mesmo do LLM
# ---------------------------------------------------------------------------

def test_resposta_vazia_e_bloqueada(chunks_example):
    import reviewer

    result = reviewer.review_ai_answer(
        raw_answer="   ",
        context_chunks=chunks_example,
        question="?",
        task_type="chat",
    )
    assert result["approved"] is False
    assert "resposta_vazia" in result["issues"]
    assert result["corrected_answer"] == reviewer.SAFE_FALLBACK


# ---------------------------------------------------------------------------
# 6. corrected_answer vazio do revisor: ainda bloqueia
# ---------------------------------------------------------------------------

def test_revisor_aprovou_mas_corrected_vazio(chunks_example):
    import reviewer

    judge_payload = {
        "approved": True,
        "risk_level": "low",
        "issues": [],
        "corrected_answer": "   ",
        "confidence": "medium",
    }

    with patch.object(reviewer, "_reviewer_groq") as mock_client_fn:
        mock_client_fn.return_value.chat.completions.create.return_value = \
            _mock_review_response(judge_payload)

        result = reviewer.review_ai_answer(
            raw_answer="resposta",
            context_chunks=chunks_example,
            question="?",
            task_type="chat",
        )

    assert result["approved"] is False
    assert "corrected_answer_vazio" in result["issues"]
    assert result["corrected_answer"] == reviewer.SAFE_FALLBACK


# ---------------------------------------------------------------------------
# 7. Risk level invalido vindo do LLM e normalizado para 'high'
# ---------------------------------------------------------------------------

def test_normaliza_risk_level_invalido(chunks_example):
    import reviewer

    judge_payload = {
        "approved": True,
        "risk_level": "EXTREMO",       # invalido
        "issues": [],
        "corrected_answer": "ok",
        "confidence": "PERFECT",       # invalido
    }

    with patch.object(reviewer, "_reviewer_groq") as mock_client_fn:
        mock_client_fn.return_value.chat.completions.create.return_value = \
            _mock_review_response(judge_payload)

        result = reviewer.review_ai_answer(
            raw_answer="resposta",
            context_chunks=chunks_example,
            question="?",
            task_type="chat",
        )

    # risk normalizado para 'high' -> bloqueia
    assert result["risk_level"] == "high"
    assert result["approved"] is False
    assert result["confidence"] == "low"
