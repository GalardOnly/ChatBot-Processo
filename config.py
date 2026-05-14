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


# Valores derivados
SUPABASE_URL      = get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = get("SUPABASE_ANON_KEY", "")
VOYAGE_API_KEY    = get("VOYAGE_API_KEY", "")
GROQ_API_KEY      = get("GROQ_API_KEY", "")

MAX_FILE_SIZE_MB = get_int("MAX_FILE_SIZE_MB", 50)
CHUNK_SIZE       = get_int("CHUNK_SIZE", 400)
CHUNK_OVERLAP    = get_int("CHUNK_OVERLAP", 50)
TOP_K_CHUNKS     = get_int("TOP_K_CHUNKS", 6)
