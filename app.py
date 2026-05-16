"""
Assistente Juridico para Defensoria - UI Streamlit.
Conforme LGPD (Lei 13.709/2018).

Rodar local:  streamlit run app.py
Deploy:       https://share.streamlit.io
"""

import time
import logging
import streamlit as st

import config
import db
import lgpd
import pdf as pdf_mod
import vector as vec
import chat as chat_mod
import security

# ---------------------------------------------------------------------------
# Rate Limiting (Seguranca A4)
# Limites por sessao para evitar exaustao de cotas das APIs externas (Groq/Voyage)
# ---------------------------------------------------------------------------
_RATE_LIMIT_CALLS   = 20   # maximo de chamadas LLM por janela de tempo
_RATE_LIMIT_WINDOW  = 600  # janela em segundos (10 minutos)
_RATE_LIMIT_UPLOADS = 5    # maximo de uploads de PDF por sessao


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
    # Biblioteca de jurisprudencia
    st.session_state.setdefault("view", "main")  # main | library | add_jurisprudence
    st.session_state.setdefault("juris_view_id", None)
    # Rate limiting: lista de timestamps de chamadas LLM na sessao atual (A4)
    st.session_state.setdefault("_rl_llm_calls", [])
    st.session_state.setdefault("_rl_upload_count", 0)
    st.session_state.setdefault("user_status", None)  # P2.9: 'approved'/'pending'/'rejected'


def _check_llm_rate_limit() -> bool:
    """
    P1.6: rate limit no BANCO (por user_id), resistente a refresh, nova
    aba ou nova sessao. Tambem mantem o backup por sessao (fail-closed
    extra) ja existente.
    """
    # Backup por sessao (defesa em profundidade)
    now = time.time()
    calls = [t for t in st.session_state._rl_llm_calls if now - t < _RATE_LIMIT_WINDOW]
    if len(calls) >= _RATE_LIMIT_CALLS:
        restante = int(_RATE_LIMIT_WINDOW - (now - min(calls)))
        st.warning(
            f"Limite por sessao atingido. "
            f"Aguarde {restante // 60}m{restante % 60:02d}s."
        )
        return False

    # Camada principal: banco
    result = db.check_rate_limit_db(
        "llm_call",
        config.RATE_LIMIT_CHAT_MAX,
        config.RATE_LIMIT_CHAT_WIN_S,
    )
    if not result.get("allowed", False):
        retry = int(result.get("retry_after_s", config.RATE_LIMIT_CHAT_WIN_S))
        st.warning(
            f"Limite de {result.get('max', config.RATE_LIMIT_CHAT_MAX)} consultas "
            f"por {config.RATE_LIMIT_CHAT_WIN_S // 60} minutos atingido. "
            f"Aguarde {retry // 60}m{retry % 60:02d}s."
        )
        return False

    calls.append(now)
    st.session_state._rl_llm_calls = calls
    return True


def _check_upload_rate_limit() -> bool:
    """P1.6: rate limit no BANCO + backup por sessao."""
    # Backup por sessao
    if st.session_state._rl_upload_count >= _RATE_LIMIT_UPLOADS:
        st.warning(
            f"Limite de uploads por sessao atingido. "
            "Faca logout e login novamente para continuar."
        )
        return False

    # Camada principal: banco
    result = db.check_rate_limit_db(
        "pdf_upload",
        config.RATE_LIMIT_UPLOAD_MAX,
        config.RATE_LIMIT_UPLOAD_WIN_S,
    )
    if not result.get("allowed", False):
        retry = int(result.get("retry_after_s", config.RATE_LIMIT_UPLOAD_WIN_S))
        st.warning(
            f"Limite de {result.get('max', config.RATE_LIMIT_UPLOAD_MAX)} uploads "
            f"por hora atingido. Aguarde {retry // 60} minutos."
        )
        return False
    return True


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
            password = st.text_input(
                "Senha", type="password", key="su_pw",
                help=(
                    f"Minimo {config.MIN_PASSWORD_LEN} caracteres. "
                    "A senha sera checada contra vazamentos conhecidos."
                ),
            )
            password2 = st.text_input("Confirme a senha", type="password", key="su_pw2")
            allowed_dom = config.ALLOWED_EMAIL_DOMAINS
            if allowed_dom:
                st.caption(
                    "Cadastro automatico apenas para dominios: "
                    + ", ".join(allowed_dom)
                    + ". Outros e-mails ficam aguardando aprovacao."
                )
            elif config.REQUIRE_ADMIN_APPROVAL:
                st.caption("Apos o cadastro, sua conta ficara pendente de aprovacao.")
            submitted = st.form_submit_button("Criar conta", type="primary", use_container_width=True)
        if submitted:
            if password != password2:
                st.error("As senhas nao conferem.")
            elif len(password) < config.MIN_PASSWORD_LEN:
                # P2.7: senha minima 12 chars
                st.error(
                    f"A senha precisa ter pelo menos {config.MIN_PASSWORD_LEN} caracteres."
                )
            elif config.HIBP_CHECK_ENABLED and security.is_password_pwned(password):
                # P2.8: HIBP k-anonymity SHA-1
                st.error(
                    "Esta senha consta em vazamentos publicos conhecidos. "
                    "Por seguranca, escolha outra senha que voce nunca usou em "
                    "outros servicos."
                )
            else:
                try:
                    db.sign_up(email, password)
                    # A criacao de user_status acontece no proximo login
                    # via get_or_create_user_status() (P2.9)
                    st.success(
                        "Conta criada. Faca login. "
                        "Se seu dominio nao esta na whitelist, voce ficara "
                        "aguardando aprovacao de um administrador."
                    )
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
# Tela de aprovacao pendente (P2.9 - whitelist de dominio / admin approval)
# ---------------------------------------------------------------------------

def render_pending_approval():
    """
    Mostrada para usuarios que se cadastraram mas ainda nao foram aprovados.
    O acesso a analise de processos fica bloqueado ate aprovacao manual.
    """
    st.title("\u23f3 Aguardando aprovacao")
    user_email = st.session_state.session.user.email
    st.warning(
        f"Sua conta (**{security.safe_text(user_email)}**) esta pendente de aprovacao "
        "por um administrador.\n\n"
        "O acesso a analise de processos jurídicos esta restrito a Defensores "
        "Publicos identificados. Voce sera notificado por e-mail quando sua "
        "conta for aprovada."
    )

    allowed = config.ALLOWED_EMAIL_DOMAINS
    if allowed:
        st.info(
            "Cadastros automaticos sao liberados apenas para os dominios: "
            + ", ".join(f"`{d}`" for d in allowed)
        )

    st.divider()
    if st.button("Sair", use_container_width=False):
        db.sign_out()
        st.session_state.session = None
        st.session_state.user_status = None
        st.rerun()


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
            st.session_state.view = "main"
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
                    st.session_state.view = "main"
                    st.rerun()
                # Mostrar retencao LGPD (Seguranca M1: sem unsafe_allow_html)
                created = proc.get("created_at", "")
                retencao = lgpd.formatar_expiracao(created) if created else ""
                st.caption(f"  {proc['total_pages']} pgs · {retencao}")

        st.divider()

        # Biblioteca de jurisprudencia
        if st.button("\U0001f4da Biblioteca de jurisprudencia",
                     use_container_width=True,
                     help="Sua colecao de acordaos, sumulas e precedentes anexados"):
            st.session_state.view = "library"
            st.session_state.selected_process = None
            st.session_state.juris_view_id = None
            st.rerun()

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

    # Seguranca M5: verificar tamanho via seek/tell ANTES de criar copia em bytes na RAM
    # Nota: maxUploadSize=50 no config.toml ja rejeita no nivel do Streamlit (1a camada).
    # Esta e a 2a camada, no Python, sem duplicar o arquivo em memoria desnecessariamente.
    uploaded.seek(0, 2)           # vai para o fim do buffer
    size_bytes = uploaded.tell()  # le a posicao = tamanho em bytes
    uploaded.seek(0)              # volta ao inicio para leitura posterior
    size_mb = size_bytes / (1024 * 1024)

    col1, col2, _ = st.columns([2, 1, 1])
    col1.metric("Arquivo", uploaded.name)
    col2.metric("Tamanho", f"{size_mb:.1f} MB")

    if size_mb > config.MAX_FILE_SIZE_MB:
        st.error(f"Arquivo acima do limite de {config.MAX_FILE_SIZE_MB} MB.")
        return

    # Carrega em memoria apenas apos validar o tamanho
    file_bytes = uploaded.getvalue()

    if not st.button("Analisar processo", type="primary"):
        return

    # Seguranca A4: verificar rate limit de uploads antes de processar
    if not _check_upload_rate_limit():
        return

    st.session_state._rl_upload_count += 1
    _process_pdf(_sanitize_filename(uploaded.name), file_bytes)


def _process_pdf(filename: str, file_bytes: bytes):
    """Pipeline completo: extrair -> chunkar -> embedding -> salvar."""
    with st.status("Processando o PDF...", expanded=True) as status:
        st.write("\U0001f50d Extraindo texto pagina a pagina...")
        # Seguranca A1: validacao de magic bytes feita dentro de extract_pages()
        try:
            pages = pdf_mod.extract_pages(file_bytes)
        except ValueError as e:
            status.update(label="Arquivo invalido", state="error")
            st.error(str(e))
            return
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

            if msg["role"] == "assistant":
                if msg.get("sources"):
                    review_meta = _get_review_meta(msg["sources"])
                    if review_meta:
                        _render_review_badge(review_meta)
                    chunk_sources = [
                        s for s in msg["sources"]
                        if s.get("type") not in ("prescricao_engine", "reviewer_meta")
                    ]
                    if chunk_sources:
                        _render_sources(chunk_sources)
                _render_feedback_buttons(msg)

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
    # Seguranca A4: verificar rate limit antes de chamar o LLM
    if not _check_llm_rate_limit():
        return
    db.save_message(process_id, "user", question, action_key=None)
    with st.spinner("Pensando..."):
        answer, sources = chat_mod.answer_question(process_id, question)
    db.save_message(process_id, "assistant", answer, sources=sources)


def _run_action_and_save(process_id: str, action_key: str):
    # Seguranca A4: verificar rate limit antes de chamar o LLM
    if not _check_llm_rate_limit():
        return
    action = chat_mod.ACTIONS[action_key]
    user_message = f"**{action['icon']} {action['label']}**"
    db.save_message(process_id, "user", user_message, action_key=action_key)
    with st.spinner(f"Executando: {action['label']}..."):
        answer, sources = chat_mod.run_action(process_id, action_key)
    # Persiste action_key tambem na resposta para suportar few-shot futuro
    db.save_message(process_id, "assistant", answer, sources=sources, action_key=action_key)


def _render_sources(sources):
    with st.expander(f"Fontes utilizadas ({len(sources)})"):
        for s in sources:
            score = s.get("score", 0)
            st.markdown(
                f"**fls. {s['page_num']}** · similaridade {score:.2f}\n\n"
                f"> {security.safe_text(s['excerpt'])}"
            )


# ---------------------------------------------------------------------------
# Badge de revisao automatica (LLM-as-judge)
# ---------------------------------------------------------------------------

def _get_review_meta(sources: list) -> dict | None:
    for s in sources:
        if isinstance(s, dict) and s.get("type") == "reviewer_meta":
            return s
    return None


_REVIEW_LEVELS = {
    "low":    ("\u2705", "success", "Revisada por IA auditora · risco baixo"),
    "medium": ("\u26a0\ufe0f", "warning", "Revisada por IA auditora · risco medio"),
    "high":   ("\U0001f6a8", "error",   "Resposta bloqueada pela revisao automatica"),
}


def _render_review_badge(meta: dict):
    """
    Exibe um pequeno indicador do resultado da revisao. Nao mostra a resposta
    bruta - corrected_answer ja substituiu a original no banco.
    """
    risk = (meta.get("risk_level") or "high").lower()
    approved = bool(meta.get("approved"))
    icon, level, default_label = _REVIEW_LEVELS.get(risk, _REVIEW_LEVELS["high"])

    if approved:
        label = default_label
        msg_fn = getattr(st, level, st.info)
    else:
        label = "\U0001f6a8 Resposta bloqueada pela revisao automatica"
        msg_fn = st.error

    issues = meta.get("issues") or []
    if issues:
        issue_str = ", ".join(f"`{i}`" for i in issues[:6])
        msg_fn(f"{label}  ·  Issues: {issue_str}")
    else:
        msg_fn(label)


# ---------------------------------------------------------------------------
# Feedback do usuario (👍/👎 nas respostas)
# ---------------------------------------------------------------------------

def _render_feedback_buttons(msg: dict):
    """
    Renderiza botoes de avaliacao para uma mensagem do assistente.
    Estado vem de msg["my_feedback"] = {"rating": ..., "comment": ...} ou None.
    """
    msg_id = msg.get("id")
    if msg_id is None:
        return

    current = msg.get("my_feedback")
    current_rating = current["rating"] if current else None
    ss_key_form = f"fb_form_{msg_id}"

    cols = st.columns([1, 1, 6])

    up_label = "👍 Útil" if current_rating != "positive" else "✅ Avaliado útil"
    if cols[0].button(up_label, key=f"fb_up_{msg_id}", use_container_width=True):
        db.save_feedback(msg_id, "positive", comment=None)
        st.session_state.pop(ss_key_form, None)
        st.toast("Obrigado pelo feedback.", icon="👍")
        st.rerun()

    down_label = "👎 Ruim" if current_rating != "negative" else "❌ Avaliado ruim"
    if cols[1].button(down_label, key=f"fb_down_{msg_id}", use_container_width=True):
        # Abre formulario de comentario opcional
        st.session_state[ss_key_form] = True
        st.rerun()

    if current_rating:
        if cols[2].button("Remover voto", key=f"fb_remove_{msg_id}"):
            db.delete_feedback(msg_id)
            st.session_state.pop(ss_key_form, None)
            st.rerun()

    # Formulario de comentario do voto negativo
    if st.session_state.get(ss_key_form):
        with st.form(f"fb_neg_form_{msg_id}"):
            st.caption("O que tava ruim? (opcional, mas ajuda a melhorar os prompts)")
            comment = st.text_area(
                "Comentario",
                key=f"fb_neg_comment_{msg_id}",
                placeholder="Ex: inventou jurisprudencia / errou a pagina / faltou citar fls. X",
                max_chars=1000,
                label_visibility="collapsed",
            )
            csubmit, ccancel = st.columns(2)
            if csubmit.form_submit_button("Enviar 👎", type="primary", use_container_width=True):
                db.save_feedback(msg_id, "negative", comment=comment.strip() or None)
                st.session_state.pop(ss_key_form, None)
                st.toast("Feedback registrado.", icon="📝")
                st.rerun()
            if ccancel.form_submit_button("Cancelar", use_container_width=True):
                st.session_state.pop(ss_key_form, None)
                st.rerun()

    # Mostra comentario antigo se existir
    if current and current.get("comment"):
        with st.expander("Seu comentario", expanded=False):
            st.caption(security.safe_text(current["comment"]))


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
# Telas da Biblioteca de Jurisprudencia
# ---------------------------------------------------------------------------

def render_library():
    """Lista a biblioteca de jurisprudencia (pessoal + global) do usuario."""
    col1, col2 = st.columns([3, 1])
    col1.title("\U0001f4da Biblioteca de jurisprudencia")
    if col2.button("+ Adicionar peca", use_container_width=True, type="primary"):
        st.session_state.view = "add_jurisprudence"
        st.rerun()

    st.caption(
        "Pecas anexadas (acordaos, sumulas, REsp, HC, etc.) sao usadas pelo "
        "RAG para fundamentar respostas. Sem peca anexada, o assistente NAO "
        "cita jurisprudencia (anti-alucinacao)."
    )

    pecas = db.list_jurisprudence()
    if not pecas:
        st.info(
            "Sua biblioteca esta vazia. Clique em **+ Adicionar peca** para colar "
            "um acordao ou subir o PDF da peca."
        )
        return

    # Filtro simples
    q = st.text_input("Filtrar por titulo / tribunal / numero", "")
    if q:
        ql = q.lower()
        pecas = [p for p in pecas if
                 ql in (p.get("title") or "").lower()
                 or ql in (p.get("court") or "").lower()
                 or ql in (p.get("case_number") or "").lower()]

    user_id = db.current_user_id()
    for p in pecas:
        is_global = p.get("user_id") is None
        owner_label = "\U0001f310 Global" if is_global else "\U0001f464 Sua"
        court = p.get("court") or ""
        case = p.get("case_number") or ""
        date = p.get("judgment_date") or ""
        rap = p.get("rapporteur") or ""
        ref_line = " · ".join(filter(None, [court, case, rap, str(date) if date else ""]))

        with st.container(border=True):
            top_left, top_right = st.columns([5, 1])
            top_left.markdown(f"**{security.safe_text(p['title'])}**")
            top_right.caption(owner_label)
            if ref_line:
                st.caption(ref_line)
            tags = p.get("tags") or []
            if tags:
                st.caption("Tags: " + ", ".join(f"`{t}`" for t in tags))
            st.caption(f"{p.get('total_chunks', 0)} blocos indexados")

            cols = st.columns([1, 1, 4])
            if cols[0].button("Ver", key=f"juris_view_{p['id']}", use_container_width=True):
                st.session_state.juris_view_id = p["id"]
                st.session_state.view = "view_jurisprudence"
                st.rerun()
            # Globais nao podem ser deletadas pelo usuario comum
            if not is_global and p.get("user_id") == user_id:
                if cols[1].button("Excluir", key=f"juris_del_{p['id']}", use_container_width=True):
                    db.delete_jurisprudence(p["id"])
                    st.success("Peca removida da biblioteca.")
                    st.rerun()


def render_add_jurisprudence():
    """Formulario para adicionar nova peca a biblioteca (texto colado ou PDF)."""
    col1, col2 = st.columns([4, 1])
    col1.title("+ Adicionar jurisprudencia")
    if col2.button("Voltar", use_container_width=True):
        st.session_state.view = "library"
        st.rerun()

    st.caption(
        "Cole o texto integral do acordao OU faca upload do PDF. O texto sera "
        "indexado e ficara disponivel para o RAG fundamentar respostas."
    )

    metodo = st.radio(
        "Como deseja adicionar?",
        ["Colar texto", "Upload de PDF"],
        horizontal=True,
        key="juris_metodo",
    )

    with st.form("add_juris_form"):
        title = st.text_input(
            "Titulo *",
            placeholder="Ex: STF, HC 126.292/SP - presuncao de inocencia",
            max_chars=300,
        )
        c1, c2 = st.columns(2)
        court = c1.text_input("Tribunal", placeholder="STF / STJ / TJSP / ...")
        case_number = c2.text_input("Numero do processo", placeholder="HC 126.292/SP")

        c3, c4 = st.columns(2)
        rapporteur = c3.text_input("Relator(a)", placeholder="Min. Teori Zavascki")
        judgment_date = c4.date_input(
            "Data de julgamento",
            value=None,
            format="DD/MM/YYYY",
        )

        tags_str = st.text_input(
            "Tags (separadas por virgula)",
            placeholder="execucao penal, presuncao de inocencia, recurso",
        )
        source_url = st.text_input(
            "Link da fonte (opcional)",
            placeholder="https://...",
        )

        full_text = ""
        uploaded_pdf = None
        if metodo == "Colar texto":
            full_text = st.text_area(
                "Texto integral do acordao *",
                height=400,
                placeholder="Cole aqui o texto completo do acordao...",
                max_chars=500000,
            )
        else:
            uploaded_pdf = st.file_uploader(
                "PDF do acordao *",
                type=["pdf"],
                accept_multiple_files=False,
            )

        submitted = st.form_submit_button(
            "Indexar peca na biblioteca",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return

    # Validacao
    if not title.strip():
        st.error("Titulo e obrigatorio.")
        return

    # Carregar texto (texto direto ou extraido do PDF)
    if metodo == "Colar texto":
        if not full_text.strip() or len(full_text.strip()) < 100:
            st.error("Texto integral muito curto. Cole o acordao completo.")
            return
        text_to_index = full_text
    else:
        if not uploaded_pdf:
            st.error("Selecione um PDF.")
            return
        # Rate limit como upload tambem
        if not _check_upload_rate_limit():
            return
        st.session_state._rl_upload_count += 1
        try:
            pdf_bytes = uploaded_pdf.getvalue()
            pages = pdf_mod.extract_pages(pdf_bytes)
        except ValueError as e:
            st.error(str(e))
            return
        if not pages:
            st.error("Nao foi possivel extrair texto do PDF (pode ser escaneado).")
            return
        text_to_index = "\n\n".join(p["text"] for p in pages)

    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None

    _index_jurisprudence(
        title=title.strip(),
        full_text=text_to_index,
        court=court.strip() or None,
        case_number=case_number.strip() or None,
        rapporteur=rapporteur.strip() or None,
        judgment_date=judgment_date.isoformat() if judgment_date else None,
        tags=tags,
        source_url=source_url.strip() or None,
    )


def _index_jurisprudence(**meta):
    """Pipeline: chunkar texto -> criar peca -> embed chunks via Voyage."""
    text = meta.pop("full_text")
    # Reutiliza chunk_pages tratando o texto como uma pseudo-pagina (page_num=1)
    pseudo_pages = [{"page_num": 1, "text": text}]
    chunks = pdf_mod.chunk_pages(pseudo_pages)
    if not chunks:
        st.error("Texto resultou em zero blocos. Verifique se nao esta vazio.")
        return

    with st.status("Indexando jurisprudencia...", expanded=True) as status:
        st.write(f"\u2702\ufe0f {len(chunks)} blocos preparados.")
        st.write("\U0001f4be Registrando peca...")
        juris_id = db.create_jurisprudence(
            full_text=text,
            total_chunks=len(chunks),
            **meta,
        )

        st.write(f"\U0001f9e0 Gerando embeddings (voyage-law-2)...")
        progress = st.progress(0.0)
        def on_progress(done, total):
            progress.progress(done / total if total else 1.0)
        vec.embed_and_store_jurisprudence(juris_id, chunks, progress_cb=on_progress)
        progress.progress(1.0)

        status.update(label=f"Pronto! {len(chunks)} blocos indexados.", state="complete")

    st.success("Peca adicionada a biblioteca.")
    st.session_state.view = "library"
    st.rerun()


def render_view_jurisprudence():
    """Visualiza o texto integral de uma peca da biblioteca."""
    juris_id = st.session_state.get("juris_view_id")
    if not juris_id:
        st.session_state.view = "library"
        st.rerun()
        return

    peca = db.get_jurisprudence(juris_id)
    if not peca:
        st.error("Peca nao encontrada.")
        if st.button("Voltar"):
            st.session_state.view = "library"
            st.rerun()
        return

    col1, col2 = st.columns([4, 1])
    col1.title(security.safe_text(peca["title"]))
    if col2.button("Voltar", use_container_width=True):
        st.session_state.view = "library"
        st.session_state.juris_view_id = None
        st.rerun()

    court = peca.get("court") or ""
    case = peca.get("case_number") or ""
    rap = peca.get("rapporteur") or ""
    date = peca.get("judgment_date") or ""
    ref_line = " · ".join(filter(None, [court, case, rap, str(date) if date else ""]))
    if ref_line:
        st.caption(ref_line)

    tags = peca.get("tags") or []
    if tags:
        st.caption("Tags: " + ", ".join(f"`{t}`" for t in tags))

    url = peca.get("source_url")
    if url:
        st.caption(f"Fonte: {url}")

    st.divider()
    st.text_area(
        "Texto integral",
        value=peca.get("full_text", ""),
        height=600,
        disabled=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """
    Sanitiza o nome do arquivo antes de armazenar no banco.
    Seguranca B1: previne path traversal e caracteres especiais
    que poderiam ser perigosos se o filename fosse usado em operacoes de arquivo.
    - Mantem: letras, numeros, espacos, hifens, underscores, pontos e parenteses
    - Remove: barras, contrabarra, null bytes e qualquer outro caractere especial
    - Limita a 200 caracteres para evitar overflow em campos de texto
    """
    import re as _re
    # Remover null bytes e caracteres de controle
    name = name.replace("\x00", "").strip()
    # Manter apenas caracteres seguros
    name = _re.sub(r"[^\w\s\-\.\(\)\[\]#]", "_", name)
    # Normalizar espacos multiplos
    name = _re.sub(r"\s+", " ", name).strip()
    # Garantir que tem extensao .pdf
    if not name.lower().endswith(".pdf"):
        name = name + ".pdf"
    # Truncar se necessario
    return name[:200] if len(name) > 200 else name


def _friendly_error(e: Exception) -> str:
    """
    Converte excecoes de auth em mensagens amigaveis.
    Seguranca M4: nunca expoe stack trace ou detalhes de infraestrutura ao usuario.
    """
    msg = str(e)
    if "Invalid login credentials" in msg:
        return "E-mail ou senha incorretos."
    if "already registered" in msg:
        # M4 + B2: mensagem neutra evita enumeracao de e-mails
        return "Nao foi possivel criar a conta com este e-mail. Tente fazer login."
    if "rate limit" in msg.lower():
        return "Muitas tentativas. Aguarde alguns minutos e tente novamente."
    if "email" in msg.lower() and "invalid" in msg.lower():
        return "Endereco de e-mail invalido."
    # Fallback generico: loga internamente sem expor ao usuario (M4)
    security.safe_log_warning("[Defensor IA] Auth error", msg)
    return "Ocorreu um erro inesperado. Tente novamente ou entre em contato com o suporte."


# ---------------------------------------------------------------------------
# Roteamento principal
# ---------------------------------------------------------------------------

if not st.session_state.session:
    # Nao autenticado: tela de login/cadastro
    render_auth()

else:
    # P2.9: verifica status de aprovacao do usuario
    if st.session_state.get("user_status") is None:
        st.session_state.user_status = db.get_or_create_user_status(
            allowed_domains=config.ALLOWED_EMAIL_DOMAINS,
        )

    if st.session_state.user_status != "approved":
        render_pending_approval()

    # Autenticado e aprovado: verificar consentimento LGPD antes de qualquer tela
    elif not st.session_state.lgpd_accepted:
        st.session_state.lgpd_accepted = db.has_accepted_lgpd()
        if not st.session_state.lgpd_accepted:
            render_lgpd_consent()
        else:
            st.rerun()

    elif st.session_state.get("show_privacy"):
        # Tela dedicada do Aviso de Privacidade
        render_sidebar()
        render_privacy_notice()

    else:
        # Fluxo normal do app
        render_sidebar()
        view = st.session_state.get("view", "main")
        if view == "library":
            render_library()
        elif view == "add_jurisprudence":
            render_add_jurisprudence()
        elif view == "view_jurisprudence":
            render_view_jurisprudence()
        elif st.session_state.selected_process:
            render_chat()
        else:
            render_upload()

# Rodape LGPD (estatico - sem unsafe_allow_html com dados externos)
st.markdown(
    "<div style='text-align:center;font-size:11px;color:#94a3b8;margin-top:2rem'>"
    "As respostas sao geradas por IA e devem ser revisadas por um defensor humano. "
    "Dados tratados em conformidade com a LGPD (Lei 13.709/2018)."
    "</div>",
    unsafe_allow_html=True,
)
