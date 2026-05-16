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
import security


# ---------------------------------------------------------------------------
# Cliente base (anon) + cliente autenticado
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cliente Supabase: ISOLADO POR SESSAO (Audit C1 - race condition fix)
#
# Decisao de seguranca: NAO usar @st.cache_resource para o cliente, porque
# postgrest.auth(token) MUTA o cliente compartilhado. Sob sessoes concorrentes
# o JWT do usuario B pode "vazar" para a requisicao do usuario A, fazendo a
# query rodar com permissoes erradas. Mantemos um cliente por sessao em
# st.session_state, recriando-o quando o token muda.
# ---------------------------------------------------------------------------

_ANON_CLIENT_KEY  = "_supabase_anon_client"   # nunca recebe auth() - read-only public
_AUTH_CLIENT_KEY  = "_supabase_auth_client"   # vinculado ao JWT desta sessao
_AUTH_TOKEN_KEY   = "_supabase_auth_token"    # token usado pela ultima vez


def _ensure_supabase_configured() -> None:
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        st.error("Configure SUPABASE_URL e SUPABASE_ANON_KEY em .streamlit/secrets.toml")
        st.stop()


def anon_client() -> Client:
    """
    Cliente anonimo (sem JWT). Usado APENAS para login/signup/refresh
    e operacoes que dependem da policy 'public'. Mantido por sessao para
    nao recriar a cada chamada.
    """
    _ensure_supabase_configured()
    c = st.session_state.get(_ANON_CLIENT_KEY)
    if c is None:
        c = create_client(config.SUPABASE_URL, config.SUPABASE_ANON_KEY)
        st.session_state[_ANON_CLIENT_KEY] = c
    return c


def _refresh_session_if_needed() -> None:
    """Renova JWT se faltar menos de 5 minutos."""
    session = st.session_state.get("session")
    if not session:
        return
    try:
        expires_at = getattr(session, "expires_at", None)
        if expires_at is None:
            return
        if time.time() > float(expires_at) - 300:
            result = anon_client().auth.refresh_session(session.refresh_token)
            if result and result.session:
                st.session_state.session = result.session
                # token mudou -> invalida cliente autenticado para recriar
                st.session_state.pop(_AUTH_CLIENT_KEY, None)
                st.session_state.pop(_AUTH_TOKEN_KEY, None)
    except Exception as e:
        security.safe_log_warning("[db] refresh JWT falhou", e)


def client() -> Client:
    """
    Cliente autenticado da sessao atual. Cada usuario tem o seu, armazenado
    em st.session_state (NAO em cache global). Se nao houver sessao, cai no
    cliente anonimo (que so passara em queries com policy public).
    """
    _ensure_supabase_configured()
    _refresh_session_if_needed()

    session = st.session_state.get("session")
    if not session:
        return anon_client()

    token = session.access_token
    cached_token = st.session_state.get(_AUTH_TOKEN_KEY)
    cached = st.session_state.get(_AUTH_CLIENT_KEY)

    # Recria se: cliente nunca foi criado OU token mudou (refresh)
    if cached is None or cached_token != token:
        cached = create_client(config.SUPABASE_URL, config.SUPABASE_ANON_KEY)
        cached.postgrest.auth(token)
        st.session_state[_AUTH_CLIENT_KEY] = cached
        st.session_state[_AUTH_TOKEN_KEY]  = token
    return cached


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def sign_up(email: str, password: str):
    return anon_client().auth.sign_up({"email": email, "password": password})


def sign_in(email: str, password: str):
    return anon_client().auth.sign_in_with_password({"email": email, "password": password})


def sign_out():
    try:
        anon_client().auth.sign_out()
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
    _list_processes_cached.clear()  # invalida cache
    return process_id


@st.cache_data(ttl=30, show_spinner=False)
def _list_processes_cached(user_id: str) -> List[Dict]:
    res = client().table("processes") \
        .select("id, filename, total_pages, total_chunks, created_at, expires_at") \
        .order("created_at", desc=True) \
        .execute()
    return res.data or []


def list_processes() -> List[Dict]:
    """Lista processos do usuario (cache 30s)."""
    user_id = current_user_id() or ""
    return _list_processes_cached(user_id)


def delete_process(process_id: str) -> None:
    """
    Deleta um processo e todos os seus dados (chunks, mensagens).
    ON DELETE CASCADE no schema cuida dos registros dependentes.
    LGPD art. 18, VI: direito a eliminacao de dados.
    """
    _log_access(process_id=process_id, action="delete_process")
    # RLS garante que so deleta se for do usuario
    client().table("processes").delete().eq("id", process_id).execute()
    _list_processes_cached.clear()  # invalida cache
    _list_messages_cached.clear()


# ---------------------------------------------------------------------------
# Messages (historico de chat)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=10, show_spinner=False)
def _list_messages_cached(process_id: str, user_id: str) -> List[Dict]:
    res = client().table("messages") \
        .select("id, role, content, sources, action_key, created_at, "
                "message_feedback(rating, comment, user_id)") \
        .eq("process_id", process_id) \
        .order("created_at") \
        .execute()
    rows = res.data or []
    for r in rows:
        fbs = r.pop("message_feedback", None) or []
        mine = next((fb for fb in fbs if fb.get("user_id") == user_id), None)
        r["my_feedback"] = (
            {"rating": mine["rating"], "comment": mine.get("comment")}
            if mine else None
        )
    return rows


def list_messages(process_id: str) -> List[Dict]:
    """
    Lista mensagens do processo com o feedback do usuario embutido (cache 10s).
    Cada item tem chave 'my_feedback' = {'rating', 'comment'} ou None.
    """
    user_id = current_user_id() or ""
    return _list_messages_cached(process_id, user_id)


def save_message(
    process_id: str,
    role: str,
    content: str,
    sources: Optional[List[Dict]] = None,
    action_key: Optional[str] = None,
) -> int:
    """
    Insere uma mensagem e retorna o id (bigint) gerado.

    Audit P1.5: mensagens de role='assistant' SEMPRE passam pela RPC
    save_assistant_message (SECURITY DEFINER) - o cliente nao pode inserir
    diretamente porque a policy de INSERT da tabela messages so aceita
    role='user'. Isso impede que um usuario forje respostas falsas no
    proprio historico via PostgREST.
    """
    msg_id = None

    if role == "assistant":
        # Insercao via RPC controlada (security definer no banco)
        res = client().rpc("save_assistant_message", {
            "p_process_id": process_id,
            "p_content":    content,
            "p_sources":    sources,
            "p_action_key": action_key,
        }).execute()
        msg_id = res.data if isinstance(res.data, int) else (
            res.data[0] if isinstance(res.data, list) and res.data else None
        )
    elif role == "user":
        payload = {
            "user_id":    current_user_id(),
            "process_id": process_id,
            "role":       "user",
            "content":    content,
            "sources":    sources,
        }
        if action_key:
            payload["action_key"] = action_key
        res = client().table("messages").insert(payload).execute()
        msg_id = res.data[0]["id"] if res.data else None
        log_action = f"action_{action_key}" if action_key else "chat"
        _log_access(process_id=process_id, action=log_action)
    else:
        raise ValueError(f"role invalido: {role!r}")

    _list_messages_cached.clear()
    return msg_id


# ---------------------------------------------------------------------------
# LGPD - Consentimento (LGPD art. 7, I e art. 8)
# ---------------------------------------------------------------------------

TERM_VERSION = "1.0"


def has_accepted_lgpd() -> bool:
    """
    Verifica se o usuario ja aceitou o Termo de Consentimento LGPD.

    Audit P1.2: FAIL-CLOSED. Em qualquer falha (rede, tabela inexistente,
    timeout), retorna False - melhor pedir consentimento de novo do que
    liberar acesso a dados sensiveis sem registro de aceite.
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
        return bool(res.data) and len(res.data) > 0
    except Exception as e:
        security.safe_log_error("[LGPD] falha ao consultar consentimento", e)
        return False  # fail-closed: nao libera acesso em caso de erro


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
    _list_jurisprudence_cached.clear()  # invalida cache
    return juris_id


@st.cache_data(ttl=30, show_spinner=False)
def _list_jurisprudence_cached(user_id: str) -> List[Dict]:
    res = client().table("jurisprudence") \
        .select("id, user_id, title, court, case_number, rapporteur, judgment_date, tags, total_chunks, created_at") \
        .order("created_at", desc=True) \
        .execute()
    return res.data or []


def list_jurisprudence() -> List[Dict]:
    """Lista as pecas visiveis ao usuario (cache 30s)."""
    user_id = current_user_id() or ""
    return _list_jurisprudence_cached(user_id)


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
    _list_jurisprudence_cached.clear()  # invalida cache


# ---------------------------------------------------------------------------
# Feedback do usuario (avaliacao das respostas do assistente)
# ---------------------------------------------------------------------------

def save_feedback(
    message_id: int,
    rating: str,
    comment: Optional[str] = None,
) -> None:
    """
    Salva (upsert) o feedback do usuario para uma mensagem do assistente.
    rating: 'positive' ou 'negative'.
    """
    if rating not in ("positive", "negative"):
        raise ValueError("rating deve ser 'positive' ou 'negative'")

    user_id = current_user_id()
    if not user_id:
        return

    payload = {
        "message_id": message_id,
        "user_id": user_id,
        "rating": rating,
        "comment": comment,
    }
    try:
        # upsert exige unique constraint (message_id, user_id) - definida no schema
        client().table("message_feedback") \
            .upsert(payload, on_conflict="message_id,user_id") \
            .execute()
        _log_access(action=f"feedback_{rating}")
        _list_messages_cached.clear()  # invalida cache
    except Exception:
        pass


def delete_feedback(message_id: int) -> None:
    """Remove o voto do usuario para uma mensagem (caso ele queira retirar)."""
    user_id = current_user_id()
    if not user_id:
        return
    try:
        client().table("message_feedback") \
            .delete() \
            .eq("message_id", message_id) \
            .eq("user_id", user_id) \
            .execute()
        _log_access(action="feedback_remove")
        _list_messages_cached.clear()  # invalida cache
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Rate limit no banco (Audit P1.6 / M4)
# ---------------------------------------------------------------------------

def check_rate_limit_db(action: str, max_calls: int, window_s: int) -> Dict:
    """
    Verifica e registra atomicamente uma chamada de acao no banco.
    Resiste a refresh, nova aba ou nova sessao (rate limit por user_id).

    Retorna dict {allowed: bool, count: int, max: int, retry_after_s: int}.
    Em caso de falha de rede, retorna allowed=False (fail-closed) para
    nao virar bypass acidental.
    """
    try:
        res = client().rpc("check_and_record_rate_limit", {
            "p_action":    action,
            "p_max_calls": max_calls,
            "p_window_s":  window_s,
        }).execute()
        if isinstance(res.data, dict):
            return res.data
        # PostgREST as vezes retorna como string JSON
        if isinstance(res.data, str):
            import json as _json
            try:
                return _json.loads(res.data)
            except Exception:
                pass
    except Exception as e:
        security.safe_log_warning("[rate_limit] falha na RPC", e)
    return {"allowed": False, "count": 0, "max": max_calls,
            "retry_after_s": window_s, "error": True}


# ---------------------------------------------------------------------------
# User status: aprovacao por whitelist de dominio ou admin (Audit P2.9)
# ---------------------------------------------------------------------------

def get_or_create_user_status(allowed_domains: Optional[List[str]] = None) -> str:
    """
    Verifica o status do usuario no primeiro acesso. Cria registro como
    'approved' se o dominio do email estiver na whitelist, ou 'pending'
    caso contrario. Retorna 'approved' | 'pending' | 'rejected'.

    Em falha, retorna 'pending' (fail-closed - LGPD/seguranca).
    """
    try:
        res = client().rpc("get_or_create_user_status", {
            "p_allowed_domains": allowed_domains or [],
        }).execute()
        return str(res.data or "pending")
    except Exception as e:
        security.safe_log_warning("[user_status] falha na RPC", e)
        return "pending"
