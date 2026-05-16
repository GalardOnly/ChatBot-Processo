"""
Conformidade com a Lei Geral de Protecao de Dados (LGPD - Lei 13.709/2018).

Este modulo centraliza:
- Base legal e finalidade do tratamento
- Textos do Aviso de Privacidade e Termo de Consentimento
- Prazo de retencao de dados
- Helpers para exportacao de dados (direito de acesso, art. 18 LGPD)

Dados de processos judiciais sao DADOS SENSIVEIS (art. 11 LGPD) pois
podem revelar a situacao juridica de pessoas naturais. A base legal
aplicavel e o exercicio regular de direitos em processo judicial
(art. 7, VI e art. 11, II, d, LGPD).
"""

from datetime import date, timedelta
from typing import Dict


# Configuracoes de retencao e finalidade

PRAZO_RETENCAO_DIAS: int = 730  # 2 anos (prazo razoavel para prescricao de delitos menores)

FINALIDADE = (
    "Apoio tecnico ao Defensor Publico na analise de processos judiciais, "
    "por meio de recuperacao inteligente de informacoes dos autos e geracao "
    "de resumos, analises probatorias, calculos de prescricao e roteiros de "
    "perguntas para audiencias, exclusivamente para uso defensivo."
)

BASE_LEGAL = (
    "Exercicio regular de direitos em processo judicial ou administrativo "
    "(LGPD art. 7, inciso VI, e art. 11, inciso II, alinea d)."
)

CONTROLADOR = "Defensor Publico responsavel pela conta cadastrada neste sistema."

OPERADOR = (
    "Sistema Defensor IA - assistente de analise de processos. "
    "Infraestrutura: Supabase (banco de dados, autenticacao), "
    "Voyage AI (geracao de embeddings), Groq (geracao de texto via LLM)."
)

DADOS_TRATADOS = [
    "E-mail do Defensor Publico (cadastro e autenticacao)",
    "Conteudo textual dos autos do processo judicial enviado (PDF)",
    "Perguntas e respostas do historico de conversa sobre o processo",
    "Registro de data e hora de acesso e operacoes realizadas (log de auditoria)",
    "Aceite do Termo de Consentimento (data e hora)",
    "Conteudo e metadados das pecas de jurisprudencia adicionadas a sua biblioteca pessoal",
]

DADOS_SENSIVEIS_TERCEIROS = (
    "Os autos do processo podem conter dados pessoais de terceiros (reu, vitima, "
    "testemunhas), incluindo dados sensiveis nos termos do art. 11 da LGPD "
    "(saude, vida sexual, biometria, origem racial, crenca religiosa etc.). "
    "O Defensor Publico e responsavel por garantir que o envio desses dados "
    "possui base legal adequada para o tratamento."
)

DIREITOS_TITULAR = [
    "Confirmacao da existencia de tratamento (art. 18, I)",
    "Acesso aos dados tratados - botao 'Exportar meus dados' neste sistema (art. 18, II)",
    "Correcao de dados incompletos ou desatualizados (art. 18, III)",
    "Anonimizacao ou eliminacao de dados desnecessarios (art. 18, IV)",
    "Portabilidade dos dados (art. 18, V)",
    "Eliminacao dos dados tratados com consentimento - botao 'Excluir minha conta' (art. 18, VI)",
    "Revogacao do consentimento a qualquer momento (art. 18, IX)",
]

CONTATO_DPO = "Utilize o botao 'Excluir minha conta' ou entre em contato com o administrador do sistema."



# Textos para a UI

def get_aviso_privacidade() -> str:
    dados_lista = "\n".join(f"  - {d}" for d in DADOS_TRATADOS)
    direitos_lista = "\n".join(f"  - {d}" for d in DIREITOS_TITULAR)
    return f"""
**AVISO DE PRIVACIDADE - DEFENSOR IA**
*Conforme exigido pela Lei Geral de Protecao de Dados (LGPD - Lei 13.709/2018)*

**1. Quem somos (Controlador)**
{CONTROLADOR}

**2. O que fazemos com seus dados (Finalidade)**
{FINALIDADE}

**3. Base legal para o tratamento**
{BASE_LEGAL}

**4. Quais dados sao tratados**
{dados_lista}

**5. Dados de terceiros nos autos**
{DADOS_SENSIVEIS_TERCEIROS}

**6. Retencao dos dados**
Os dados de cada processo sao retidos por ate **{PRAZO_RETENCAO_DIAS} dias** (2 anos) a partir
do envio do PDF. Apos esse prazo, os dados sao eliminados automaticamente do banco de dados.
Voce pode excluir um processo a qualquer momento.

**7. Compartilhamento**
Os dados sao processados pelos seguintes operadores para viabilizar o servico:
- **Supabase** (banco de dados e autenticacao) - supabase.com
- **Voyage AI** (geracao de embeddings de texto) - voyageai.com
- **Groq** (geracao de analises por LLM) - groq.com

Nenhum dado e compartilhado com terceiros para fins comerciais ou publicitarios.

**8. Seus direitos como titular**
{direitos_lista}

**9. Contato**
{CONTATO_DPO}
""".strip()


def get_termo_consentimento() -> str:
    return f"""
**TERMO DE CONSENTIMENTO E USO - DEFENSOR IA**

Antes de continuar, leia e aceite os termos abaixo:

**O que este sistema faz**
O Defensor IA e um assistente de inteligencia artificial que analisa processos
judiciais enviados por voce (em formato PDF) e responde perguntas sobre os autos,
gera resumos, analises probatorias e calcula prazos de prescricao.

**Dados que serao tratados**
- Seu e-mail de cadastro
- O conteudo dos PDFs dos processos que voce enviar
- O historico de perguntas e respostas de cada processo

**Aviso importante sobre dados de terceiros**
Os PDFs dos processos contem dados pessoais e possivelmente dados sensiveis
de terceiros (reu, vitima, testemunhas). Ao enviar um processo, voce declara
que possui base legal para o tratamento desses dados no contexto de atuacao
defensiva (LGPD art. 7, VI e art. 11, II, d).

**Base legal**
{BASE_LEGAL}

**Retencao**
Seus dados sao mantidos por ate {PRAZO_RETENCAO_DIAS} dias por processo.
Voce pode excluir qualquer processo ou toda a sua conta a qualquer momento.

**Seus direitos**
Voce pode exportar ou excluir todos os seus dados pelo menu lateral do sistema.

**Ao clicar em "Li e aceito", voce confirma que:**
1. Leu e compreendeu este termo e o Aviso de Privacidade
2. Concorda com o tratamento dos dados conforme descrito
3. Possui autorizacao para submeter os autos de processos a este sistema
4. Tem ciencia de que as analises geradas por IA sao apoio tecnico e devem
   ser revisadas por um profissional habilitado
""".strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def data_expiracao(created_at_iso: str) -> date:
    """
    Calcula a data de expiracao de um processo a partir da data de criacao.
    created_at_iso: string ISO 8601 (ex: '2025-01-15T10:30:00+00:00')
    """
    try:
        # Aceita tanto datetime completo quanto so data
        dt_str = created_at_iso[:10]  # pega YYYY-MM-DD
        y, m, d = dt_str.split("-")
        criado = date(int(y), int(m), int(d))
        return criado + timedelta(days=PRAZO_RETENCAO_DIAS)
    except Exception:
        return date.today() + timedelta(days=PRAZO_RETENCAO_DIAS)


def dias_ate_expiracao(created_at_iso: str) -> int:
    """Retorna quantos dias faltam para o processo expirar (negativo = ja expirou)."""
    return (data_expiracao(created_at_iso) - date.today()).days


def formatar_expiracao(created_at_iso: str) -> str:
    """Retorna string legivel para mostrar na UI."""
    dias = dias_ate_expiracao(created_at_iso)
    exp = data_expiracao(created_at_iso)
    exp_str = exp.strftime("%d/%m/%Y")
    if dias < 0:
        return f"Expirado em {exp_str}"
    elif dias == 0:
        return "Expira hoje"
    elif dias <= 30:
        return f"Expira em {dias} dias ({exp_str})"
    else:
        return f"Retido ate {exp_str}"


def resumo_tratamento() -> Dict:
    """Dicionario estruturado com os dados do tratamento (para exportacao/log)."""
    return {
        "finalidade": FINALIDADE,
        "base_legal": BASE_LEGAL,
        "controlador": CONTROLADOR,
        "operador": OPERADOR,
        "dados_tratados": DADOS_TRATADOS,
        "prazo_retencao_dias": PRAZO_RETENCAO_DIAS,
        "direitos_titular": DIREITOS_TITULAR,
    }
