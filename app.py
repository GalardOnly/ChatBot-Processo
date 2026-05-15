"""
Assistente Juridico para Defensoria - UI Streamlit.
Conforme LGPD (Lei 13.709/2018).

Rodar local:  streamlit run app.py
Deploy:       https://share.streamlit.io
"""

import streamlit as st

import config
import db
import lgpd
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
    st.session_state.setdefault("lgpd_accepted", False)
    st.session_state.setdefault("show_privacy", False)
    st.session_state.setdefault("confirm_delete_account", False)


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
                st.session_state.lgpd_accepted = False  # verifica novo a cada login
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
                    st.success("Conta criada. Faca login.")
                except Exception as e:
                    st.error(f"Falha no cadastro: {_friendly_error(e)}")

    # Rodape com link para aviso de privacidade
    st.divider()
    st.caption(
        "Ao usar este sistema, voce concorda com o tratamento de dados conforme a "
        "LGPD (Lei 13.709/2018). Seus dados sao usados exclusivamente para apoio "
        "ao Defensor Publico na analise de processos judiciais."
    )


# ---------------------------------------------------------------------------
# Tela de Consentimento LGPD (LGPD art. 7, I e art. 8)
# ---------------------------------------------------------------------------

def render_lgpd_consent():
    """
    Exibida uma vez por versao do termo, antes do primeiro uso.
    O aceite e registrado no banco (tabela lgpd_consents).
    """
    st.title("⚖️ Defensor IA")
    st.warning(
        "**Antes de continuar, leia e aceite os termos abaixo.**\n\n"
        "Em conformidade com a LGPD (Lei 13.709/2018), precisamos do seu "
        "consentimento informado para tratar dados pessoais e dados sensiveis "
        "de processos judiciais neste sistema."
    )

    with st.expander("Ler Termo de Consentimento completo", expanded=True):
        st.markdown(lgpd.get_termo_consentimento())

    with st.expander("Ler Aviso de Privacidade completo"):
        st.markdown(lgpd.get_aviso_privacidade())

    st.divider()
    col1, col2 = st.columns([3, 1])
    with col1:
        aceito = st.checkbox(
            "Li e aceito o Termo de Consentimento e o Aviso de Privacidade acima. "
            "Confirmo que possuo autorizacao para submeter autos de processos a este sistema."
        )
    with col2:
        if st.button("Continuar", type="primary", use_container_width=True, disabled=not aceito):
            db.record_lgpd_consent()
            st.session_state.lgpd_accepted = True
            st.rerun()

    if st.button("Sair sem aceitar", use_container_width=False):
        db.sign_out()
        st.session_state.session = None
        st.rerun()


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
                # Mostrar retencao LGPD
                created = proc.get("created_at", "")
                retencao = lgpd.formatar_expiracao(created) if created else ""
                st.caption(
                    f"&nbsp;&nbsp;{proc['total_pages']} pgs · {retencao}",
                    unsafe_allow_html=True,
                )

        st.divider()

        # SECAO LGPD: Seus Dados
        _render_sidebar_lgpd()

        if st.button("Sair", use_container_width=True):
            db.sign_out()
            st.session_state.session = None
            st.session_state.selected_process = None
            st.rerun()


def _render_sidebar_lgpd():
    """
    Secao de direitos LGPD na sidebar.
    Permite ao usuario exportar e excluir seus dados (LGPD art. 18).
    """
    with st.expander("🔒 Seus dados (LGPD)"):
        st.caption(
            "Em conformidade com a LGPD (Lei 13.709/2018), "
            "voce tem direito de acessar e excluir seus dados."
        )

        # Direito de acesso: exportar dados (art. 18, I e II)
        if st.button("📥 Exportar meus dados", use_container_width=True,
                     help="Baixa um JSON com todos os seus processos e historico de conversa."):
            with st.spinner("Preparando exportacao..."):
                json_str = db.export_user_data_json()
            st.download_button(
                label="⬇️ Baixar meus dados (JSON)",
                data=json_str,
                file_name="defensor_ia_meus_dados.json",
                mime="application/json",
                use_container_width=True,
            )
            db.log_action("export")
            st.success("Exportacao pronta.")

        st.divider()

        # Aviso de privacidade
        if st.button("📄 Ver Aviso de Privacidade", use_container_width=True):
            st.session_state.show_privacy = True
            st.rerun()

        st.divider()

        # Direito a eliminacao: excluir conta (art. 18, VI)
        st.caption("**Excluir conta e todos os dados**")
        st.caption(
            "Esta acao e irreversivel. Todos os processos, "
            "chunks e historico serao permanentemente deletados."
        )

        if not st.session_state.get("confirm_delete_account"):
            if st.button("🗑️ Excluir minha conta", use_container_width=True,
                         type="secondary"):
                st.session_state.confirm_delete_account = True
                st.rerun()
        else:
            st.error("Tem certeza? Esta acao e IRREVERSIVEL.")
            col1, col2 = st.columns(2)
            if col1.button("Sim, excluir tudo", use_container_width=True, type="primary"):
                with st.spinner("Excluindo seus dados..."):
                    ok = db.delete_all_user_data()
                if ok:
                    st.success("Dados excluidos. Fazendo logout...")
                    db.sign_out()
                    st.session_state.session = None
                    st.session_state.selected_process = None
                    st.session_state.confirm_delete_account = False
                    st.rerun()
                else:
                    st.error("Falha ao excluir dados. Tente novamente.")
                    st.session_state.confirm_delete_account = False
            if col2.button("Cancelar", use_container_width=True):
                st.session_state.confirm_delete_account = False
                st.rerun()


# ---------------------------------------------------------------------------
# Tela de Aviso de Privacidade
# ---------------------------------------------------------------------------

def render_privacy_notice():
    st.title("Aviso de Privacidade")
    st.markdown(lgpd.get_aviso_privacidade())
    if st.button("Fechar"):
        st.session_state.show_privacy = False
        st.rerun()


# ---------------------------------------------------------------------------
# Tela de Upload
# ---------------------------------------------------------------------------

def render_upload():
    st.title("Novo processo")
    st.write("Envie o PDF do processo para iniciar a conversa com o assistente.")

    # Aviso LGPD especifico para upload (dados de terceiros)
    st.info(
        "⚠️ **Aviso LGPD (art. 11):** Os autos do processo podem conter dados sensiveis de terceiros "
        "(reu, vitima, testemunhas). Ao enviar, voce confirma ter base legal para este tratamento "
        "no contexto de atuacao defensiva. Os dados sao retidos por "
        f"**{lgpd.PRAZO_RETENCAO_DIAS} dias** e eliminados automaticamente em seguida."
    )

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

    # Expiracao do processo (LGPD retencao)
    created = proc.get("created_at", "")
    retencao_str = lgpd.formatar_expiracao(created) if created else ""
    dias = lgpd.dias_ate_expiracao(created) if created else 999

    col_info, col_del = st.columns([4, 1])
    col_info.caption(
        f"{proc['total_pages']} paginas · {proc['total_chunks']} blocos indexados"
        + (f" · 🗓️ {retencao_str}" if retencao_str else "")
    )

    # Botao de deletar processo (LGPD art. 18, VI)
    if col_del.button("🗑️ Excluir processo", help="Remove este processo e todos os seus dados (LGPD art. 18, VI)"):
        db.delete_process(proc["id"])
        st.session_state.selected_process = None
        st.success("Processo excluido.")
        st.rerun()

    # Alerta se processo esta perto de expirar
    if 0 <= dias <= 30:
        st.warning(
            f"⚠️ **Retencao LGPD:** Este processo expira em **{dias} dias**. "
            "Apos essa data os dados serao eliminados automaticamente. "
            "Exporte o que precisar antes."
        )
    elif dias < 0:
        st.error(
            "🚨 **Retencao LGPD:** O prazo de retencao deste processo expirou. "
            "Os dados serao eliminados na proxima limpeza automatica."
        )

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
    db.save_message(process_id, "user", question, action_key=None)
    with st.spinner("Pensando..."):
        answer, sources = chat_mod.answer_question(process_id, question)
    db.save_message(process_id, "assistant", answer, sources=sources)


def _run_action_and_save(process_id: str, action_key: str):
    action = chat_mod.ACTIONS[action_key]
    user_message = f"**{action['icon']} {action['label']}**"
    db.save_message(process_id, "user", user_message, action_key=action_key)
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
    risco = meta.get("risco", "INDETERMINADO")
    icon, level, label = _RISCO_CONFIG.get(risco, _RISCO_CONFIG["INDETERMINADO"])

    msg_fn = getattr(st, level, st.info)
    msg_fn(f"{icon} **{label}**  ·  Calculado em Python puro com base nos trechos recuperados")

    pena = meta.get("pena_max")
    prazo = meta.get("prazo")
    marcos = meta.get("marcos", [])
    intervalos = meta.get("intervalos", [])
    alertas = meta.get("alertas", [])

    col1, col2, col3 = st.columns(3)
    col1.metric("Pena max. em abstrato", f"{pena} anos" if pena else "--")
    col2.metric("Prazo prescricional (art. 109)", f"{prazo} anos" if prazo else "--")
    col3.metric("Marcos identificados", len(marcos))

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
            rows.append({"Marco": m["label"], "Data": data_str, "fls.": m.get("pagina", "--")})
        st.table(rows)

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
# Roteamento principal
# ---------------------------------------------------------------------------

if not st.session_state.session:
    # Nao autenticado: tela de login/cadastro
    render_auth()

else:
    # Autenticado: verificar consentimento LGPD antes de qualquer tela
    if not st.session_state.lgpd_accepted:
        st.session_state.lgpd_accepted = db.has_accepted_lgpd()

    if not st.session_state.lgpd_accepted:
        # Primeira vez ou nova versao do termo: exige aceite
        render_lgpd_consent()

    elif st.session_state.get("show_privacy"):
        # Tela dedicada do Aviso de Privacidade
        render_sidebar()
        render_privacy_notice()

    else:
        # Fluxo normal do app
        render_sidebar()
        if st.session_state.selected_process:
            render_chat()
        else:
            render_upload()

# Rodape LGPD
st.markdown(
    "<div style='text-align:center;font-size:11px;color:#94a3b8;margin-top:2rem'>"
    "As respostas sao geradas por IA e devem ser revisadas por um defensor humano. "
    "Dados tratados em conformidade com a LGPD (Lei 13.709/2018)."
    "</div>",
    unsafe_allow_html=True,
)
