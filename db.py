"""
Cliente Supabase compartilhado entre os modulos da app.
- Auth: sign_up, sign_in, sign_out
- CRUD: processes, chunks (via vector.py), messages
- LGPD: consentimento, log de acesso, exportacao, exclusao de dados
"""

import json
import time
from datetime import datetime
from typing import Dict, List, Optional

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


def _refresh_session_if_needed() -> None:
    """
    Renova o token JWT se estiver a menos de 5 minutos de expirar.
    Seguranca M2: evita que sessoes longas percam dados silenciosamente
    quando o token expira (padrao Supabase: 1 hora).
    """
    session = st.session_state.get("session")
    if not session:
        return
    try:
        expires_at = getattr(session, "expires_at", None)
        if expires_at is None:
            return
        # Renova se faltar menos de 5 minutos (300 segundos)
        if time.time() > float(expires_at) - 300:
            result = _base_client().auth.refresh_session(session.refresh_token)
            if result and result.session:
                st.session_state.session = result.session
    except Exception:
        # Falha silenciosa: a proxima chamada ao banco retornara 401
        # e o usuario precisara fazer login novamente
        pass


def client() -> Client:
    """
    Retorna um cliente Supabase com o JWT do usuario logado.
    Renova o token automaticamente se estiver proximo de expirar (M2).
    As policies RLS usam auth.uid() que e resolvido pelo JWT.
    """
    _refresh_session_if_needed()
    c = _base_client()
    session = st.session_state.get("session")
    if session:
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
    process_id = res.data[0]["id"]
    # Log LGPD: upload de processo
    _log_access(process_id=process_id, action="upload")
    return process_id


def list_processes() -> List[Dict]:
    res = client().table("processes") \
        .select("id, filename, total_pages, total_chunks, created_at, expires_at") \
        .order("created_at", desc=True) \
        .execute()
    return res.data or []


def delete_process(process_id: str) -> None:
    """
    Deleta um processo e todos os seus dados (chunks, mensagens).
    ON DELETE CASCADE no schema cuida dos registros dependentes.
    LGPD art. 18, VI: direito a eliminacao de dados.
    """
    _log_access(process_id=process_id, action="delete_process")
    # RLS garante que so deleta se for do usuario
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


def save_message(
    process_id: str,
    role: str,
    content: str,
    sources: Optional[List[Dict]] = None,
    action_key: Optional[str] = None,
) -> None:
    client().table("messages").insert({
        "user_id": current_user_id(),
        "process_id": process_id,
        "role": role,
        "content": content,
        "sources": sources,
    }).execute()
    # Log LGPD: interacao com processo
    if role == "user":
        log_action = f"action_{action_key}" if action_key else "chat"
        _log_access(process_id=process_id, action=log_action)


# ---------------------------------------------------------------------------
# LGPD - Consentimento (LGPD art. 7, I e art. 8)
# ---------------------------------------------------------------------------

TERM_VERSION = "1.0"


def has_accepted_lgpd() -> bool:
    """
    Verifica se o usuario ja aceitou o Termo de Consentimento LGPD.
    Retorna True se houver pelo menos um registro de aceite.
    """
    user_id = current_user_id()
    if not user_id:
        return False
    try:
        res = client().table("lgpd_consents") \
            .select("id") \
            .eq("user_id", user_id) \
            .eq("term_version", TERM_VERSION) \
            .limit(1) \
            .execute()
        return len(res.data) > 0
    except Exception:
        # Em caso de erro (ex: tabela ainda nao criada), nao bloqueia o usuario
        return True


def _get_ip_hint() -> str:
    """
    Retorna os primeiros 2 octetos do IP do usuario (ex: "191.26.*.*").
    Seguranca B3: registra IP parcial para audit trail LGPD sem identificar
    o usuario de forma exata (principio da minimizacao - LGPD art. 6, III).
    Usa st.context.ip_address disponivel no Streamlit 1.31+.
    """
    try:
        ip = getattr(st.context, "ip_address", None)
        if not ip:
            return ""
        parts = str(ip).split(".")
        if len(parts) == 4:
            # IPv4: oculta 3o e 4o octeto
            return f"{parts[0]}.{parts[1]}.*.*"
        # IPv6: retorna apenas o prefixo /32
        return str(ip)[:8] + "..."
    except Exception:
        return ""


def record_lgpd_consent() -> None:
    """
    Registra o aceite do Termo de Consentimento LGPD pelo usuario.
    LGPD art. 8, par. 1: o consentimento deve ser documentado.
    Inclui ip_hint (B3) para audit trail sem expor o IP completo.
    """
    user_id = current_user_id()
    if not user_id:
        return
    try:
        record = {
            "user_id": user_id,
            "term_version": TERM_VERSION,
            "accepted_at": datetime.utcnow().isoformat(),
        }
        ip_hint = _get_ip_hint()
        if ip_hint:
            record["ip_hint"] = ip_hint
        client().table("lgpd_consents").insert(record).execute()
    except Exception:
        pass  # Nao bloqueia o fluxo se falhar; logar em producao


# ---------------------------------------------------------------------------
# LGPD - Log de acesso a dados (LGPD art. 37)
# ---------------------------------------------------------------------------

def _log_access(action: str, process_id: Optional[str] = None) -> None:
    """
    Registra internamente uma operacao de acesso a dados no log de auditoria.
    Chamado automaticamente pelas funcoes de CRUD.
    Falhas sao silenciosas para nao quebrar o fluxo principal.
    """
    user_id = current_user_id()
    if not user_id:
        return
    try:
        record = {
            "user_id": user_id,
            "action": action,
        }
        if process_id:
            record["process_id"] = process_id
        client().table("data_access_log").insert(record).execute()
    except Exception:
        pass


def log_action(action: str, process_id: Optional[str] = None) -> None:
    """Versao publica de _log_access para uso externo (ex: app.py)."""
    _log_access(action=action, process_id=process_id)


# ---------------------------------------------------------------------------
# LGPD - Exportacao de dados (LGPD art. 18, I e II)
# ---------------------------------------------------------------------------

def export_user_data() -> Dict:
    """
    Exporta todos os dados do usuario como dicionario Python.
    Chama a funcao RPC do Supabase que monta o JSON completo.
    LGPD art. 18, II: direito de acesso aos dados.
    """
    user_id = current_user_id()
    if not user_id:
        return {}
    try:
        res = client().rpc("export_user_data", {"p_user_id": user_id}).execute()
        return res.data if res.data else {}
    except Exception:
        # Fallback: monta exportacao local se a funcao RPC nao existir ainda
        return _export_local(user_id)


def _export_local(user_id: str) -> Dict:
    """Exportacao local como fallback (sem funcao RPC no banco)."""
    try:
        processes = list_processes()
        all_data = {
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "user_id": user_id,
            "processes": [],
        }
        for proc in processes:
            msgs = list_messages(proc["id"])
            proc_data = {**proc, "messages": msgs}
            all_data["processes"].append(proc_data)
        return all_data
    except Exception:
        return {"error": "Falha ao exportar dados. Tente novamente."}


def export_user_data_json() -> str:
    """Retorna os dados do usuario como string JSON formatada para download."""
    data = export_user_data()
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# LGPD - Eliminacao de dados (LGPD art. 18, VI)
# ---------------------------------------------------------------------------

def request_deletion(reason: str = "") -> None:
    """
    Registra um pedido formal de exclusao de dados (LGPD art. 18, VI).
    O pedido fica registrado antes de executar a exclusao.
    """
    user_id = current_user_id()
    if not user_id:
        return
    try:
        client().table("deletion_requests").insert({
            "user_id": user_id,
            "reason": reason,
            "status": "pending",
        }).execute()
    except Exception:
        pass


def delete_all_user_data() -> bool:
    """
    Elimina TODOS os dados do usuario (processos, chunks, mensagens, logs).
    Nao exclui a conta de autenticacao (precisa ser feita separadamente).
    LGPD art. 18, VI: direito a eliminacao de dados tratados com base em consentimento.
    Retorna True se bem-sucedido.
    """
    user_id = current_user_id()
    if not user_id:
        return False
    try:
        # Registra o pedido antes de executar
        request_deletion(reason="Exclusao solicitada pelo usuario via interface")
        # Chama a funcao RPC de exclusao
        client().rpc("delete_user_data", {"p_user_id": user_id}).execute()
        return True
    except Exception:
        # Fallback: deleta diretamente pela API
        try:
            client().table("processes").delete().eq("user_id", user_id).execute()
            client().table("jurisprudence").delete().eq("user_id", user_id).execute()
            client().table("data_access_log").delete().eq("user_id", user_id).execute()
            client().table("lgpd_consents").delete().eq("user_id", user_id).execute()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Biblioteca de Jurisprudencia (pessoal por usuario)
# ---------------------------------------------------------------------------

def create_jurisprudence(
    title: str,
    full_text: str,
    court: Optional[str] = None,
    case_number: Optional[str] = None,
    rapporteur: Optional[str] = None,
    judgment_date: Optional[str] = None,
    tags: Optional[List[str]] = None,
    source_url: Optional[str] = None,
    total_chunks: int = 0,
) -> str:
    """Cria uma nova peca de jurisprudencia para o usuario logado."""
    user_id = current_user_id()
    record = {
        "user_id": user_id,
        "title": title,
        "full_text": full_text,
        "total_chunks": total_chunks,
    }
    if court:         record["court"] = court
    if case_number:   record["case_number"] = case_number
    if rapporteur:    record["rapporteur"] = rapporteur
    if judgment_date: record["judgment_date"] = judgment_date
    if tags:          record["tags"] = tags
    if source_url:    record["source_url"] = source_url

    res = client().table("jurisprudence").insert(record).execute()
    juris_id = res.data[0]["id"]
    _log_access(action="add_jurisprudence")
    return juris_id


def list_jurisprudence() -> List[Dict]:
    """Lista as pecas visiveis ao usuario (pessoais + globais)."""
    res = client().table("jurisprudence") \
        .select("id, user_id, title, court, case_number, rapporteur, judgment_date, tags, total_chunks, created_at") \
        .order("created_at", desc=True) \
        .execute()
    return res.data or []


def get_jurisprudence(juris_id: str) -> Optional[Dict]:
    """Recupera uma peca completa (com full_text)."""
    res = client().table("jurisprudence") \
        .select("*") \
        .eq("id", juris_id) \
        .limit(1) \
        .execute()
    return res.data[0] if res.data else None


def delete_jurisprudence(juris_id: str) -> None:
    """Deleta uma peca (cascade limpa os chunks). RLS impede deletar globais."""
    _log_access(action="delete_jurisprudence")
    client().table("jurisprudence").delete().eq("id", juris_id).execute()
