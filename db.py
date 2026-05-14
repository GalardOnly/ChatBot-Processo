"""
Cliente Supabase compartilhado entre os modulos da app.
- Auth: sign_up, sign_in, sign_out
- CRUD: processes, chunks (via vector.py), messages
- Helper: cliente autenticado com o JWT do usuario logado
"""

from typing import List, Dict, Optional

import streamlit as st
from supabase import create_client, Client

import config


# ---------------------------------------------------------------------------
# Cliente base (anon) + cliente autenticado
# ---------------------------------------------------------------------------

@st.cache_resource
def _base_client() -> Client:
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        st.error("Configure SUPABASE_URL e SUPABASE_ANON_KEY em .streamlit/secrets.toml")
        st.stop()
    return create_client(config.SUPABASE_URL, config.SUPABASE_ANON_KEY)


def client() -> Client:
    """
    Retorna um cliente Supabase. Se houver sessao na st.session_state,
    aplica o JWT para que as policies RLS funcionem corretamente.
    """
    c = _base_client()
    session = st.session_state.get("session")
    if session:
        # PostgREST usa o JWT do header para resolver auth.uid()
        c.postgrest.auth(session.access_token)
    return c


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def sign_up(email: str, password: str):
    return _base_client().auth.sign_up({"email": email, "password": password})


def sign_in(email: str, password: str):
    return _base_client().auth.sign_in_with_password({"email": email, "password": password})


def sign_out():
    try:
        _base_client().auth.sign_out()
    except Exception:
        pass


def current_user_id() -> Optional[str]:
    session = st.session_state.get("session")
    return session.user.id if session else None


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

def create_process(filename: str, total_pages: int, total_chunks: int) -> str:
    user_id = current_user_id()
    res = client().table("processes").insert({
        "user_id": user_id,
        "filename": filename,
        "total_pages": total_pages,
        "total_chunks": total_chunks,
    }).execute()
    return res.data[0]["id"]


def list_processes() -> List[Dict]:
    res = client().table("processes") \
        .select("id, filename, total_pages, total_chunks, created_at") \
        .order("created_at", desc=True) \
        .execute()
    return res.data or []


def delete_process(process_id: str) -> None:
    # RLS garante que so deleta se for do usuario; ON DELETE CASCADE limpa chunks/messages
    client().table("processes").delete().eq("id", process_id).execute()


# ---------------------------------------------------------------------------
# Messages (historico de chat)
# ---------------------------------------------------------------------------

def list_messages(process_id: str) -> List[Dict]:
    res = client().table("messages") \
        .select("role, content, sources, created_at") \
        .eq("process_id", process_id) \
        .order("created_at") \
        .execute()
    return res.data or []


def save_message(process_id: str, role: str, content: str, sources: Optional[List[Dict]] = None) -> None:
    client().table("messages").insert({
        "user_id": current_user_id(),
        "process_id": process_id,
        "role": role,
        "content": content,
        "sources": sources,
    }).execute()
