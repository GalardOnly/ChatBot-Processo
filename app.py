"""
Assistente Juridico para Defensoria - UI Streamlit.

Rodar local:  streamlit run app.py
Deploy:       https://share.streamlit.io  (conecte o repo do GitHub)
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


_init_state()


# ---------------------------------------------------------------------------
# Tela de Auth (login / cadastro)
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
# Sidebar (lista de processos + acoes)
# ---------------------------------------------------------------------------

def render_sidebar():
    with st.sidebar:
        st.markdown("### ⚖️ Defensor IA")

        user_email = st.session_state.session.user.email
        st.caption(f"Logado: **{user_email}**")

        if st.button("➕ Novo processo", use_container_width=True, type="primary"):
            st.session_state.selected_process = None
            st.session_state.pending_question = None
            st.rerun()

        st.divider()
        st.markdown("**Processos**")

        processes = db.list_processes()
        if not processes:
            st.caption("Nenhum processo ainda. Suba um PDF para comecar.")
        else:
            for proc in processes:
                is_active = st.session_state.selected_process and st.session_state.selected_process["id"] == proc["id"]
                label = f"{'📂' if is_active else '📄'} {proc['filename']}"
                if st.button(label, key=f"proc_{proc['id']}", use_container_width=True):
                    st.session_state.selected_process = proc
                    st.session_state.pending_question = None
                    st.rerun()
                st.caption(f"&nbsp;&nbsp;{proc['total_pages']} pgs · {proc['total_chunks']} blocos",
                           unsafe_allow_html=True)

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
                "3. Voce conversa para extrair fatos, prazos e fundamentos"
            )
        return

    file_bytes = uploaded.getvalue()
    size_mb = len(file_bytes) / (1024 * 1024)

    col1, col2, col3 = st.columns([2, 1, 1])
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
        st.write("🔍 Extraindo texto pagina a pagina...")
        pages = pdf_mod.extract_pages(file_bytes)
        if not pages:
            status.update(label="Falha", state="error")
            st.error("Nao foi possivel extrair texto. O PDF pode ser uma imagem escaneada (precisa OCR).")
            return
        st.write(f"✅ {len(pages)} paginas com texto util.")

        st.write("✂️ Dividindo em blocos...")
        chunks = pdf_mod.chunk_pages(pages)
        st.write(f"✅ {len(chunks)} blocos preparados.")

        st.write("💾 Registrando processo...")
        process_id = db.create_process(filename, len(pages), len(chunks))

        st.write(f"🧠 Gerando embeddings (voyage-law-2) em {(len(chunks)+63)//64} batches...")
        progress = st.progress(0.0)
        def on_progress(done, total):
            progress.progress(done / total if total else 1.0)
        vec.embed_and_store(process_id, chunks, progress_cb=on_progress)
        progress.progress(1.0)

        status.update(label=f"Pronto! {len(pages)} paginas, {len(chunks)} blocos.", state="complete")

    # Seleciona o processo recem-criado e pula pro chat
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

SUGGESTIONS = [
    "Resumir o processo",
    "Quais sao os principais fatos?",
    "Existem prazos pendentes?",
    "Sugerir teses de defesa",
]


def render_chat():
    proc = st.session_state.selected_process
    st.title(proc["filename"])
    st.caption(f"{proc['total_pages']} paginas · {proc['total_chunks']} blocos indexados")

    history = db.list_messages(proc["id"])

    if not history:
        with st.chat_message("assistant"):
            st.markdown(
                "Recebi o processo e estou pronto. Voce pode pedir um resumo, "
                "perguntar sobre fatos, prazos ou possiveis teses de defesa."
            )
        cols = st.columns(len(SUGGESTIONS))
        for i, s in enumerate(SUGGESTIONS):
            if cols[i].button(s, key=f"sug_{i}", use_container_width=True):
                st.session_state.pending_question = s
                st.rerun()

    for msg in history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant" and msg.get("sources"):
                _render_sources(msg["sources"])

    # Pergunta vinda de botao de sugestao
    pending = st.session_state.pop("pending_question", None)
    user_input = st.chat_input("Pergunte algo sobre o processo...")
    question = pending or user_input

    if question:
        _answer_and_save(proc["id"], question)
        st.rerun()


def _answer_and_save(process_id: str, question: str):
    db.save_message(process_id, "user", question)
    with st.spinner("Pensando..."):
        answer, sources = chat_mod.answer_question(process_id, question)
    db.save_message(process_id, "assistant", answer, sources=sources)


def _render_sources(sources):
    with st.expander(f"Fontes utilizadas ({len(sources)})"):
        for s in sources:
            score = s.get("score", 0)
            st.markdown(
                f"**Pagina {s['page_num']}** · similaridade {score:.2f}\n\n"
                f"> {s['excerpt']}"
            )


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
