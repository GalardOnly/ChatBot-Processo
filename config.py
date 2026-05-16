"""
Carrega configuracoes do Streamlit secrets ou variaveis de ambiente.
Permite rodar tanto local (com .env) quanto no Streamlit Cloud (com secrets).
"""

import os
from typing import Any

import streamlit as st
from dotenv import load_dotenv


load_dotenv()


def get(key: str, default: Any = None) -> Any:
    """Le primeiro de st.secrets (Streamlit Cloud), depois de variaveis de ambiente."""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except (FileNotFoundError, AttributeError):
        pass
    return os.environ.get(key, default)


def get_int(key: str, default: int) -> int:
    val = get(key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool) -> bool:
    val = get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def get_list(key: str, default: list) -> list:
    """Le lista de string separada por virgula."""
    val = get(key, None)
    if val is None:
        return default
    if isinstance(val, (list, tuple)):
        return [str(v).strip() for v in val if str(v).strip()]
    return [v.strip() for v in str(val).split(",") if v.strip()]


# =========================================================================
# Credenciais externas
# =========================================================================
SUPABASE_URL      = get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = get("SUPABASE_ANON_KEY", "")
VOYAGE_API_KEY    = get("VOYAGE_API_KEY", "")
GROQ_API_KEY      = get("GROQ_API_KEY", "")

# =========================================================================
# Limites de uso (Seguranca A4 e DoS)
# =========================================================================
MAX_FILE_SIZE_MB = get_int("MAX_FILE_SIZE_MB", 50)
CHUNK_SIZE       = get_int("CHUNK_SIZE", 400)
CHUNK_OVERLAP    = get_int("CHUNK_OVERLAP", 50)
TOP_K_CHUNKS     = get_int("TOP_K_CHUNKS", 6)

# Limites adicionais contra zip-bomb / processo gigante (Audit A4)
MAX_PAGES        = get_int("MAX_PAGES",  1000)   # paginas extraidas do PDF
MAX_CHUNKS       = get_int("MAX_CHUNKS", 3000)   # blocos antes de embedar

# =========================================================================
# Autenticacao (Audit M1, M2)
# =========================================================================
MIN_PASSWORD_LEN     = get_int("MIN_PASSWORD_LEN", 12)
HIBP_CHECK_ENABLED   = get_bool("HIBP_CHECK_ENABLED", True)
# Lista de dominios autorizados para signup direto (ex: defensoria.gov.br).
# Vazio = qualquer dominio, mas sera marcado como pendente de aprovacao.
ALLOWED_EMAIL_DOMAINS = get_list("ALLOWED_EMAIL_DOMAINS", [])
# Se True, signup sem dominio whitelisted exige approved_by_admin = true
REQUIRE_ADMIN_APPROVAL = get_bool("REQUIRE_ADMIN_APPROVAL", True)

# =========================================================================
# PDFs e uploads (Audit M7, B1)
# =========================================================================
# Isolar PyMuPDF em subprocess (mitiga CVEs do parser)
ISOLATE_PDF_PARSING  = get_bool("ISOLATE_PDF_PARSING", False)
PDF_SUBPROCESS_TIMEOUT_S = get_int("PDF_SUBPROCESS_TIMEOUT_S", 60)

# Scan antivirus (plugavel)
SCAN_ENGINE       = get("SCAN_ENGINE", "none")          # "none" | "clamav" | "virustotal"
SCAN_FAIL_CLOSED  = get_bool("SCAN_FAIL_CLOSED", False) # se True, scan indisponivel = rejeita upload

# =========================================================================
# Rate limit no banco (Audit M4)
# Substituem progressivamente os limites por sessao Streamlit
# =========================================================================
RATE_LIMIT_CHAT_MAX     = get_int("RATE_LIMIT_CHAT_MAX",     30)
RATE_LIMIT_CHAT_WIN_S   = get_int("RATE_LIMIT_CHAT_WIN_S",   600)
RATE_LIMIT_UPLOAD_MAX   = get_int("RATE_LIMIT_UPLOAD_MAX",    8)
RATE_LIMIT_UPLOAD_WIN_S = get_int("RATE_LIMIT_UPLOAD_WIN_S", 3600)
