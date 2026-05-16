"""
Helpers de seguranca: sanitizacao de logs, escape de markdown, scan de upload,
checagem HIBP de senha vazada.

Centraliza decisoes para evitar duplicacao e facilitar auditoria.
"""

import hashlib
import logging
import re
from typing import Dict

import requests


# =========================================================================
# Sanitizacao de PII para logs (LGPD art. 6 III - minimizacao)
# =========================================================================

# Padroes de PII brasileira e secrets comuns que NAO podem entrar em logs
_PII_PATTERNS = [
    # E-mails
    (re.compile(r"\b[\w\.\-+]+@[\w\.\-]+\.[A-Za-z]{2,}\b"), "[email]"),
    # CPF (com ou sem formatacao)
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}\-?\d{2}\b"), "[cpf]"),
    # RG (com ou sem formatacao, varia por estado)
    (re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}\-?[\dxX]\b"), "[rg]"),
    # Telefone BR
    (re.compile(r"\(?\+?\d{2,3}\)?\s?\d{4,5}\-?\d{4}"), "[tel]"),
    # CEP
    (re.compile(r"\b\d{5}\-?\d{3}\b"), "[cep]"),
    # Numero de processo CNJ (NNNNNNN-DD.YYYY.J.TR.OOOO)
    (re.compile(r"\b\d{7}\-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b"), "[processo]"),
    # JWT (3 segmentos base64)
    (re.compile(r"\beyJ[\w\-]+\.[\w\-]+\.[\w\-]+\b"), "[jwt]"),
    # Supabase keys (novo e legado)
    (re.compile(r"sb_(?:publishable|secret)_[\w\-]+"), "[sb_key]"),
    # Voyage / Groq / OpenAI tokens
    (re.compile(r"\bpa\-[\w\-]{20,}\b"), "[voyage_key]"),
    (re.compile(r"\bgsk_[\w]{20,}\b"), "[groq_key]"),
    (re.compile(r"\bsk\-[\w]{20,}\b"), "[api_key]"),
    # UUIDs (podem ser process_id/user_id - evita ligar log a recurso)
    (re.compile(r"\b[0-9a-f]{8}\-[0-9a-f]{4}\-[0-9a-f]{4}\-[0-9a-f]{4}\-[0-9a-f]{12}\b", re.I), "[uuid]"),
]

_MAX_LOG_LEN = 500


def sanitize_log_message(msg: str) -> str:
    """
    Remove PII e secrets de uma string antes de enviar para o logger.

    Decisao de seguranca: nunca confiar que mensagens de excecao do Supabase,
    Voyage, Groq ou PyMuPDF sao seguras para logar - elas podem conter o
    payload original que causou o erro (incluindo trechos de PDF).

    Sempre aplique esta funcao antes de:
        logging.warning(...), logging.error(...), logger.exception(...).
    """
    if not msg:
        return ""
    s = str(msg)
    for pat, repl in _PII_PATTERNS:
        s = pat.sub(repl, s)
    if len(s) > _MAX_LOG_LEN:
        s = s[:_MAX_LOG_LEN] + "...[truncado]"
    return s


def safe_log_warning(prefix: str, msg) -> None:
    """Helper: loga warning com mensagem sanitizada."""
    logging.warning(f"{prefix}: {sanitize_log_message(str(msg))}")


def safe_log_error(prefix: str, msg) -> None:
    """Helper: loga error com mensagem sanitizada."""
    logging.error(f"{prefix}: {sanitize_log_message(str(msg))}")


# =========================================================================
# Escape de markdown / HTML para conteudo de usuario ou PDF
# =========================================================================

_MD_ESCAPE_TABLE = str.maketrans({
    "\\": "\\\\",
    "`": "\\`",
    "*": "\\*",
    "_": "\\_",
    "[": "\\[",
    "]": "\\]",
    "<": "&lt;",
    ">": "&gt;",
    "|": "\\|",
})


def safe_text(s) -> str:
    """
    Escapa caracteres ativos de markdown e HTML basico.

    Decisao de seguranca: conteudo vindo de PDF, titulo de jurisprudencia,
    comentario de feedback ou qualquer campo livre pode conter markdown
    malicioso (links javascript:, imagens externas) ou HTML que quebra layout.
    Sempre passe por aqui antes de exibir via st.markdown.
    """
    if not s:
        return ""
    return str(s).translate(_MD_ESCAPE_TABLE)


# =========================================================================
# Checagem HIBP (Have I Been Pwned) - senha vazada
# =========================================================================

_HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"


def is_password_pwned(password: str, timeout: float = 3.0) -> bool:
    """
    Verifica se a senha esta em vazamentos conhecidos via HIBP.

    Usa k-anonymity: envia apenas os primeiros 5 chars do SHA-1.
    A senha em texto puro NUNCA sai do processo Python.

    Retorna:
      True  - senha esta em vazamento conhecido (bloquear)
      False - nao foi encontrada OU API offline (fail-open por UX,
              mas a chamada e' opcional na camada de signup)
    """
    if not password:
        return False
    try:
        sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
        prefix, suffix = sha1[:5], sha1[5:]
        resp = requests.get(
            _HIBP_RANGE_URL.format(prefix=prefix),
            headers={"Add-Padding": "true", "User-Agent": "DefensorIA-Auth"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return False
        for line in resp.text.splitlines():
            parts = line.strip().split(":", 1)
            if len(parts) == 2 and parts[0].upper() == suffix and int(parts[1]) > 0:
                return True
        return False
    except Exception as e:
        # Fail-open: se HIBP estiver fora, nao bloqueia signup
        # (defensa em profundidade: ainda exigimos 12+ chars e politica forte)
        safe_log_warning("[HIBP] check offline", e)
        return False


# =========================================================================
# Scan de upload (interface plugavel - stub para ClamAV/VirusTotal)
# =========================================================================

class ScanResult(dict):
    """Resultado do scan: {clean: bool, scanner: str, detail: str}."""
    @property
    def clean(self) -> bool:
        return bool(self.get("clean", False))


def scan_uploaded_file(file_bytes: bytes, filename: str = "") -> ScanResult:
    """
    Interface unica para scanning de upload. Plugavel via config.SCAN_ENGINE:
      - "none"        -> nao executa scan (retorna clean=True)
      - "clamav"      -> chama clamdscan via subprocess (a implementar)
      - "virustotal"  -> chama API VirusTotal (a implementar)

    Se config.SCAN_FAIL_CLOSED = True e o engine nao estiver disponivel,
    retorna clean=False (rejeita o upload).
    """
    import config

    engine = (config.SCAN_ENGINE or "none").lower()
    fail_closed = bool(config.SCAN_FAIL_CLOSED)

    if engine == "none":
        return ScanResult(clean=True, scanner="none", detail="scan desativado")

    # Placeholders - implementar quando integrar ClamAV/VirusTotal
    if engine in ("clamav", "virustotal"):
        if fail_closed:
            return ScanResult(
                clean=False,
                scanner=engine,
                detail=f"engine {engine} configurado mas nao implementado (fail-closed)",
            )
        return ScanResult(
            clean=True,
            scanner=engine,
            detail=f"engine {engine} nao implementado - permitido por fail-open",
        )

    # Engine desconhecido
    return ScanResult(
        clean=not fail_closed,
        scanner="unknown",
        detail=f"engine '{engine}' desconhecido",
    )
