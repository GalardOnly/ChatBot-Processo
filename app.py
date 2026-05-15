"""
Assistente Juridico para Defensoria - UI Streamlit.

Rodar local:  streamlit run app.py
Deploy:       https://share.streamlit.io
"""

import streamlit as st

import config
import db
import pdf as pdf_mod
import vector as vec
import chat as chat_mod


# ---------------------------------------------------------------------------
# Setup geral
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Defensor IA",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _init_state():
    st.session_state.setdefault("session", None)
    st.session_state.setdefault("selected_process", None)
    st.session_state.setdefault("pending_question", None)
    st.session_state.setdefault("pending_action", None)


_init_state()


# ---------------------------------------------------------------------------
# Tela de Auth
# ---------------------------------------------------------------------------

def render_auth():
    st.title("⚖️ Defensor IA")
    st.caption("Assistente para Defensoria Publica - leitura de processos via IA")

    tab_login, tab_signup = st.tabs(["Entrar", "Criar conta"])

    with tab_login:
        with st.form("login"):
            email = st.text_input("E-mail", autocomplete="email")
            password = st.text_input("Senha", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("Entrar", type="primary", use_container_width=True)
        if submitted:
            try:
                res = db.sign_in(email, password)
                st.session_state.session = res.session
                st.rerun()
            except Exception as e:
                st.error(f"Falha no login: {_friendly_error(e)}")

    with tab_signup:
        with st.form("signup"):
            email = st.text_input("E-mail", key="su_email")
            password = st.text_input("Senha", type="password", key="su_pw",
                                     help="Use uma senha forte. Minimo 8 caracteres.")
            password2 = st.text_input("Confirme a senha", type="password", key="su_pw2")
            submitted = st.form_submit_button("Criar conta", type="primary", use_container_width=True)
        if submitted:
            if password != password2:
                st.error("As senhas nao conferem.")
            elif len(password) < 8:
                st.error("A senha precisa ter pelo menos 8 caracteres.")
            else:
                try:
                    db.sign_up(email, password)
                    st.success("Conta criada. Verifique seu e-mail (se confirmacao estiver ativa) e faca login.")
                except Exception as e:
                    st.error(f"Falha no cadastro: {_friendly_error(e)}")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar():
    with st.sidebar:
        st.markdown("### ⚖️ Defensor IA")

        user_email = st.session_state.session.user.email
        st.caption(f"Logado: **{user_email}**")

        if st.button("➕ Novo processo", use_container_width=True, type="primary"):
            st.session_state.selected_process = None
            st.session_state.pending_question = None
            st.session_state.pending_action = None
            st.rerun()

        st.divider()
        st.markdown("**Processos**")

        processes = db.list_processes()
        if not processes:
            st.caption("Nenhum processo ainda. Suba um PDF para comecar.")
        else:
            for proc in processes:
                is_active = (
                    st.session_state.selected_process
                    and st.session_state.selected_process["id"] == proc["id"]
                )
                label = f"{'📂' if is_active else '📄'} {proc['filename']}"
                if st.button(label, key=f"proc_{proc['id']}", use_container_width=True):
                    st.session_state.selected_process = proc
                    st.session_state.pending_question = None
                    st.session_state.pending_action = None
                    st.rerun()
                st.caption(
                    f"&nbsp;&nbsp;{proc['total_pages']} pgs · {proc['total_chunks']} blocos",
                    unsafe_allow_html=True,
                )

        st.divider()
        if st.button("Sair", use_container_width=True):
            db.sign_out()
            st.session_state.session = None
            st.session_state.selected_process = None
            st.rerun()


# ---------------------------------------------------------------------------
# Tela de Upload
# ---------------------------------------------------------------------------

def render_upload():
    st.title("Novo processo")
    st.write("Envie o PDF do processo para iniciar a conversa com o assistente.")

    uploaded = st.file_uploader(
        "Arraste o PDF aqui ou clique para selecionar",
        type=["pdf"],
        accept_multiple_files=False,
        label_visibility="collapsed",
    )

    if not uploaded:
        with st.container(border=True):
            st.markdown("#### Como funciona")
            st.markdown(
                "1. Envie o PDF do processo\n"
                "2. O sistema le, divide em blocos e indexa o conteudo\n"
                "3. Voce conversa para extrair fatos, prazos e fundamentos\n"
                "4. Use os botoes de **Acoes** para rodar analises estruturadas"
            )
        return

    file_bytes = uploaded.getvalue()
    size_mb = len(file_bytes) / (1024 * 1024)

    col1, col2, _ = st.columns([2, 1, 1])
    col1.metric("Arquivo", uploaded.name)
    col2.metric("Tamanho", f"{size_mb:.1f} MB")

    if size_mb > config.MAX_FILE_SIZE_MB:
        st.error(f"Arquivo acima do limite de {config.MAX_FILE_SIZE_MB} MB.")
        return

    if not st.button("Analisar processo", type="primary"):
        return

    _process_pdf(uploaded.name, file_bytes)


def _process_pdf(filename: str, file_bytes: bytes):
    """Pipeline completo: extrair -> chunkar -> embedding -> salvar."""
    with st.status("Processando o PDF...", expanded=True) as status:
        st.write("\U0001f50d Extraindo texto pagina a pagina...")
        pages = pdf_mod.extract_pages(file_bytes)
        if not pages:
            status.update(label="Falha", state="error")
            st.error(
                "Nao foi possivel extrair texto. O PDF pode ser uma imagem escaneada. "
                "Tente converter com OCR antes de enviar (ex: Adobe Acrobat, CamScanner)."
            )
            return
        st.write(f"✅ {len(pages)} paginas com texto util.")

        st.write("✂️ Dividindo em blocos...")
        chunks = pdf_mod.chunk_pages(pages)
        st.write(f"✅ {len(chunks)} blocos preparados.")

        st.write("\U0001f4be Registrando processo...")
        process_id = db.create_process(filename, len(pages), len(chunks))

        st.write(f"\U0001f9e0 Gerando embeddings (voyage-law-2) em {(len(chunks) + 63) // 64} batches...")
        progress = st.progress(0.0)

        def on_progress(done, total):
            progress.progress(done / total if total else 1.0)

        vec.embed_and_store(process_id, chunks, progress_cb=on_progress)
        progress.progress(1.0)

        status.update(label=f"Pronto! {len(pages)} paginas, {len(chunks)} blocos.", state="complete")

    st.session_state.selected_process = {
        "id": process_id,
        "filename": filename,
        "total_pages": len(pages),
        "total_chunks": len(chunks),
    }
    st.rerun()


# ---------------------------------------------------------------------------
# Tela de Chat
# ---------------------------------------------------------------------------

def render_chat():
    proc = st.session_state.selected_process
    st.title(proc["filename"])
    st.caption(f"{proc['total_pages']} paginas · {proc['total_chunks']} blocos indexados")

    _render_action_panel()

    history = db.list_messages(proc["id"])

    if not history:
        with st.chat_message("assistant"):
            st.markdown(
                "Processo indexado. Use um dos botoes de **Acoes** acima para rodar "
                "uma analise estruturada, ou faca uma pergunta livre abaixo."
            )

    for msg in history:
        with st.chat_message(msg["role"]):
            # Se for resposta de prescricao, mostra painel visual antes do texto
            if msg["role"] == "assistant" and msg.get("sources"):
                engine_meta = _get_prescricao_meta(msg["sources"])
                if engine_meta:
                    _render_prescricao_panel(engine_meta)

            st.markdown(msg["content"])

            if msg["role"] == "assistant" and msg.get("sources"):
                chunk_sources = [s for s in msg["sources"] if s.get("type") != "prescricao_engine"]
                if chunk_sources:
                    _render_sources(chunk_sources)

    pending_action = st.session_state.pop("pending_action", None)
    pending_question = st.session_state.pop("pending_question", None)
    user_input = st.chat_input("Pergunte algo sobre o processo...")

    if pending_action:
        _run_action_and_save(proc["id"], pending_action)
        st.rerun()
    elif pending_question or user_input:
        _answer_and_save(proc["id"], pending_question or user_input)
        st.rerun()


def _render_action_panel():
    st.markdown("##### Acoes")
    actions = list(chat_mod.ACTIONS.items())
    cols = st.columns(len(actions))
    for col, (key, meta) in zip(cols, actions):
        label = f"{meta['icon']} {meta['label']}"
        if col.button(label, key=f"act_{key}", use_container_width=True,
                      help=meta["description"]):
            st.session_state.pending_action = key
            st.rerun()
    st.divider()


def _answer_and_save(process_id: str, question: str):
    db.save_message(process_id, "user", question)
    with st.spinner("Pensando..."):
        answer, sources = chat_mod.answer_question(process_id, question)
    db.save_message(process_id, "assistant", answer, sources=sources)


def _run_action_and_save(process_id: str, action_key: str):
    action = chat_mod.ACTIONS[action_key]
    user_message = f"**{action['icon']} {action['label']}**"
    db.save_message(process_id, "user", user_message)
    with st.spinner(f"Executando: {action['label']}..."):
        answer, sources = chat_mod.run_action(process_id, action_key)
    db.save_message(process_id, "assistant", answer, sources=sources)


def _render_sources(sources):
    with st.expander(f"Fontes utilizadas ({len(sources)})"):
        for s in sources:
            score = s.get("score", 0)
            st.markdown(
                f"**fls. {s['page_num']}** · similaridade {score:.2f}\n\n"
                f"> {s['excerpt']}"
            )


# ---------------------------------------------------------------------------
# Painel visual de prescricao (motor deterministico)
# ---------------------------------------------------------------------------

def _get_prescricao_meta(sources: list) -> dict | None:
    """Extrai metadados do motor de prescricao dos sources, se existirem."""
    for s in sources:
        if isinstance(s, dict) and s.get("type") == "prescricao_engine":
            return s
    return None


_RISCO_CONFIG = {
    "CONSUMADA":     ("\U0001f6a8", "error",   "PRESCRICAO POSSIVELMENTE CONSUMADA"),
    "ALTO":          ("⚠️",  "warning", "RISCO ALTO de prescricao"),
    "MODERADO":      ("⚡",  "warning", "RISCO MODERADO de prescricao"),
    "BAIXO":         ("✅",  "success", "RISCO BAIXO de prescricao"),
    "INDETERMINADO": ("❓",  "info",    "Risco INDETERMINADO - revisar manualmente"),
}


def _render_prescricao_panel(meta: dict):
    """Renderiza o painel visual com os dados calculados pelo motor Python."""
    risco = meta.get("risco", "INDETERMINADO")
    icon, level, label = _RISCO_CONFIG.get(risco, _RISCO_CONFIG["INDETERMINADO"])

    # Badge de risco
    msg_fn = getattr(st, level, st.info)
    msg_fn(f"{icon} **{label}**  ·  Calculado em Python puro com base nos trechos recuperados")

    # Metricas principais
    pena = meta.get("pena_max")
    prazo = meta.get("prazo")
    marcos = meta.get("marcos", [])
    intervalos = meta.get("intervalos", [])
    alertas = meta.get("alertas", [])

    col1, col2, col3 = st.columns(3)
    col1.metric("Pena max. em abstrato", f"{pena} anos" if pena else "--")
    col2.metric("Prazo prescricional (art. 109)", f"{prazo} anos" if prazo else "--")
    col3.metric("Marcos identificados", len(marcos))

    # Tabela de marcos
    if marcos:
        st.markdown("**Marcos interruptivos (CP art. 117):**")
        rows = []
        for m in marcos:
            data_str = m["data"][:10] if m.get("data") else "Nao localizada"
            if m.get("data"):
                try:
                    y, mo, d = data_str.split("-")
                    data_str = f"{d}/{mo}/{y}"
                except Exception:
                    pass
            rows.append({
                "Marco": m["label"],
                "Data": data_str,
                "fls.": m.get("pagina", "--"),
            })
        st.table(rows)

    # Tabela de intervalos
    if intervalos:
        st.markdown("**Intervalos calculados (Python puro):**")
        rows_iv = []
        for iv in intervalos:
            status = "❌ PRESCREVEU" if iv["prescreveu"] else "✅ OK"
            rows_iv.append({
                "De": iv["de_label"],
                "Ate": iv["ate_label"],
                "Anos decorridos": iv["anos"],
                "Prazo": f"{iv['prazo']} anos",
                "% do prazo": f"{iv['percentual']}%",
                "Status": status,
            })
        st.table(rows_iv)

    # Alertas
    for alerta in alertas:
        if risco == "CONSUMADA":
            st.error(f"\U0001f6a8 {alerta}")
        elif risco in ("ALTO", "MODERADO"):
            st.warning(f"⚠️ {alerta}")
        else:
            st.info(f"ℹ️ {alerta}")

    st.caption(
        "⚠️ Este calculo e estimativa baseada nos trechos recuperados pelo RAG e DEVE ser "
        "conferido pelo defensor nas folhas originais do processo. Verifique tambem causas "
        "suspensivas (CP art. 116) que possam alterar os prazos."
    )
    st.divider()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _friendly_error(e: Exception) -> str:
    msg = str(e)
    if "Invalid login credentials" in msg:
        return "E-mail ou senha incorretos."
    if "already registered" in msg:
        return "Este e-mail ja esta cadastrado."
    if "rate limit" in msg.lower():
        return "Muitas tentativas. Aguarde alguns minutos."
    return msg


# ---------------------------------------------------------------------------
# Roteamento
# ---------------------------------------------------------------------------

if not st.session_state.session:
    render_auth()
else:
    render_sidebar()
    if st.session_state.selected_process:
        render_chat()
    else:
        render_upload()

st.markdown(
    "<div style='text-align:center;font-size:11px;color:#94a3b8;margin-top:2rem'>"
    "As respostas sao geradas por IA e devem ser revisadas por um defensor humano."
    "</div>",
    unsafe_allow_html=True,
)
