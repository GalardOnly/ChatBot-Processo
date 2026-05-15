"""
Motor deterministico de prescricao penal.

Aplica CP art. 109 (prazos) e art. 117 (marcos interruptivos) sem depender
da LLM para fazer os calculos. A LLM recebe os dados ja computados e apenas
contextualiza, corrige e recomenda.

Uso interno: importado por chat.py na acao "prescricao".
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# CP art. 109 - tabela pena maxima (anos) -> prazo prescricional (anos)
# ---------------------------------------------------------------------------

_TABELA_109: List[Tuple[float, int]] = [
    (12.0, 20),
    (8.0,  16),
    (4.0,  12),
    (2.0,   8),
    (1.0,   4),
    (0.0,   3),
]


def prazo_pela_pena(pena_max_anos: float) -> int:
    """Retorna o prazo prescricional em anos conforme CP art. 109."""
    for limite, prazo in _TABELA_109:
        if pena_max_anos > limite:
            return prazo
    return 3


# ---------------------------------------------------------------------------
# Normalizacao de texto (remove acentos, lowercase)
# Usado antes de comparar keywords para tolerar PDFs sem acentuacao
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Remove acentos e converte para lowercase."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


# ---------------------------------------------------------------------------
# Extracao de datas do texto
# ---------------------------------------------------------------------------

_MESES: Dict[str, int] = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

_RE_NUMERICO = re.compile(
    r"\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})\b"
)
_RE_EXTENSO = re.compile(
    r"\b(\d{1,2})\s+de\s+("
    + "|".join(_MESES.keys())
    + r")\s+de\s+(\d{4})\b",
    re.IGNORECASE,
)


def _parse_numerico(m: re.Match) -> Optional[date]:
    try:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_extenso(m: re.Match) -> Optional[date]:
    try:
        d = int(m.group(1))
        mo = _MESES.get(_norm(m.group(2)))
        y = int(m.group(3))
        if not mo:
            return None
        return date(y, mo, d)
    except ValueError:
        return None


def _find_dates_in_text(text: str) -> List[Tuple[date, int]]:
    """Retorna lista de (date, posicao) sem duplicatas."""
    found: Dict[date, int] = {}
    for m in _RE_NUMERICO.finditer(text):
        d = _parse_numerico(m)
        if d and d not in found:
            found[d] = m.start()
    for m in _RE_EXTENSO.finditer(text):
        d = _parse_extenso(m)
        if d and d not in found:
            found[d] = m.start()
    return sorted(found.items())


# ---------------------------------------------------------------------------
# Classificacao de marcos por contexto textual
# Keywords ja normalizadas (sem acento, lowercase)
# ---------------------------------------------------------------------------

_MARCO_LABELS: Dict[str, str] = {
    "data_fato":            "Data do fato (art. 117, I)",
    "recebimento_denuncia": "Recebimento da denuncia (art. 117, I)",
    "pronuncia":            "Decisao de pronuncia (art. 117, II)",
    "sentenca":             "Sentenca condenatoria (art. 117, IV)",
    "acordao":              "Acordao confirmatorio (art. 117, V/VI)",
}

# Keywords ja em forma normalizada (sem acento, lowercase)
_KEYWORDS: Dict[str, List[str]] = {
    "data_fato": [
        "data do fato", "data dos fatos", "fato ocorreu", "crime ocorreu",
        "delito ocorreu", "praticado em", "cometido em", "ocorrencia",
        "boletim de ocorrencia", "b.o.", "no dia dos fatos",
        "data da infracao", "infracao praticada", "ocorrido em",
        "no dia em que", "fatos ocorreram",
    ],
    "recebimento_denuncia": [
        "recebimento da denuncia", "recebi a denuncia", "recebo a denuncia",
        "denuncia recebida", "recebida a denuncia", "denuncia foi recebida",
        "recebeu a denuncia", "recebo a presente denuncia",
    ],
    "pronuncia": [
        "pronuncia", "pronunciado", "pronunciou o reu",
        "decisao de pronuncia", "decreto de pronuncia", "julgo pronunciado",
        "submeto o reu", "submeto ao tribunal do juri",
    ],
    "sentenca": [
        "sentenca condenatoria", "condeno o reu", "condeno o acusado",
        "julgo procedente a acao penal", "sentenca publicada",
        "prolatada a sentenca", "julgo procedente o pedido",
        "condeno o denunciado", "pelo que condeno",
        "condeno o ora reu", "fica condenado",
    ],
    "acordao": [
        "acordao", "apelacao improvida", "improvimento do recurso",
        "mantida a condenacao", "confirmada a sentenca", "desprovido",
        "negado provimento ao recurso", "recurso nao provido",
        "apelacao criminal nao provida", "tribunal negou",
    ],
}

_WINDOW = 350  # chars ao redor da data para analisar contexto


def _classify_date(text: str, pos: int) -> Optional[str]:
    """Classifica uma data em um tipo de marco pelo contexto ao redor (texto normalizado)."""
    start = max(0, pos - _WINDOW)
    end = min(len(text), pos + _WINDOW)
    snippet = _norm(text[start:end])
    for key, kws in _KEYWORDS.items():
        for kw in kws:
            if kw in snippet:
                return key
    return None


# ---------------------------------------------------------------------------
# Extracao de pena maxima
# ---------------------------------------------------------------------------

_RE_PENA_MAXIMA = re.compile(
    r"pena\s+(?:maxima|privativa)[^\d]{0,30}?(\d+)\s*(?:anos?|a\.)",
    re.IGNORECASE,
)
_RE_RECLUSAO = re.compile(
    r"(?:reclusao|reclusao)[^\d]{0,20}(\d+)\s+(?:a\s+\d+\s+)?anos?",
    re.IGNORECASE,
)
_RE_DETENCAO = re.compile(
    r"detencao[^\d]{0,20}(\d+)\s*anos?",
    re.IGNORECASE,
)
_RE_CONDENADO_ANOS = re.compile(
    r"(?:condeno|condenado)[^\d]{0,40}?(\d+)\s+(?:\(\w+\)\s+)?anos?",
    re.IGNORECASE,
)

# Tipos penais comuns com pena maxima em abstrato (referencia rapida)
_TIPOS_PENAIS: Dict[str, float] = {
    "homicidio simples":          20.0,
    "homicidio qualificado":      30.0,
    "lesao corporal grave":        5.0,
    "lesao corporal gravissima":   5.0,
    "lesao corporal seguida":     12.0,
    "lesao corporal":              1.0,
    "ameaca":                      1.0,
    "furto simples":               4.0,
    "furto qualificado":           8.0,
    "roubo":                      10.0,
    "roubo qualificado":          15.0,
    "extorsao":                   10.0,
    "estelionato":                 5.0,
    "trafico":                    15.0,
    "porte ilegal":                4.0,
    "estupro de vulneravel":      15.0,
    "estupro":                    10.0,
}


def _extract_pena(full_text: str) -> Optional[float]:
    """Tenta extrair pena maxima em anos a partir do texto consolidado (normalizado)."""
    norm_text = _norm(full_text)

    # 1. Padrao explicito "pena maxima X anos"
    for pat in (_RE_PENA_MAXIMA, _RE_RECLUSAO, _RE_DETENCAO, _RE_CONDENADO_ANOS):
        m = pat.search(norm_text)
        if m:
            try:
                val = float(m.group(1))
                if 1.0 <= val <= 40.0:
                    return val
            except ValueError:
                pass

    # 2. Tipo penal mencionado (mais especifico primeiro)
    for tipo, pena in sorted(_TIPOS_PENAIS.items(), key=lambda x: -len(x[0])):
        if tipo in norm_text:
            return pena

    return None


# ---------------------------------------------------------------------------
# Dataclasses de resultado
# ---------------------------------------------------------------------------

@dataclass
class Marco:
    tipo: str
    label: str
    data: Optional[date]
    pagina: int
    trecho: str = ""


@dataclass
class Intervalo:
    de_label: str
    ate_label: str
    anos: float
    prazo: int
    prescreveu: bool

    @property
    def percentual(self) -> float:
        return round(self.anos / self.prazo * 100, 1) if self.prazo else 0.0


@dataclass
class ResultadoPrescricao:
    marcos: List[Marco] = field(default_factory=list)
    pena_max: Optional[float] = None
    prazo: Optional[int] = None
    intervalos: List[Intervalo] = field(default_factory=list)
    risco: str = "INDETERMINADO"
    alertas: List[str] = field(default_factory=list)
    hoje: date = field(default_factory=date.today)


# ---------------------------------------------------------------------------
# Motor principal
# ---------------------------------------------------------------------------

def calcular(chunks: List[Dict]) -> ResultadoPrescricao:
    """
    Recebe a lista de chunks do processo (cada um com 'text' e 'page_num')
    e retorna um ResultadoPrescricao com todos os calculos feitos.
    """
    resultado = ResultadoPrescricao()
    hoje = resultado.hoje

    # Passo 1: identificar marcos interruptivos
    encontrados: Dict[str, Marco] = {}

    for chunk in chunks:
        text = chunk.get("text", "")
        pagina = chunk.get("page_num", 0)

        for d, pos in _find_dates_in_text(text):
            # Filtra datas absurdas
            if not (date(1940, 1, 1) <= d <= hoje):
                continue
            tipo = _classify_date(text, pos)
            if tipo and tipo not in encontrados:
                # Extrai trecho de contexto legivel
                s = max(0, pos - 130)
                e = min(len(text), pos + 60)
                trecho = text[s:e].strip().replace("\n", " ")
                encontrados[tipo] = Marco(
                    tipo=tipo,
                    label=_MARCO_LABELS[tipo],
                    data=d,
                    pagina=pagina,
                    trecho=trecho,
                )

    # Ordena conforme sequencia logica do CP
    for tipo in _MARCO_LABELS:
        if tipo in encontrados:
            resultado.marcos.append(encontrados[tipo])

    # Passo 2: pena maxima e prazo prescricional
    full_text = "\n".join(c.get("text", "") for c in chunks)
    resultado.pena_max = _extract_pena(full_text)
    if resultado.pena_max is not None:
        resultado.prazo = prazo_pela_pena(resultado.pena_max)

    # Passo 3: calcular intervalos entre marcos consecutivos
    marcos_datados = [m for m in resultado.marcos if m.data is not None]

    if len(marcos_datados) >= 1 and resultado.prazo:
        pares = list(zip(marcos_datados, marcos_datados[1:])) + [
            (marcos_datados[-1], None)  # ultimo marco -> hoje
        ]
        for a, b in pares:
            data_b = b.data if b else hoje
            label_b = b.label if b else f"Hoje ({hoje.strftime('%d/%m/%Y')})"
            delta_anos = round((data_b - a.data).days / 365.25, 2)
            prescreveu = delta_anos > resultado.prazo
            resultado.intervalos.append(
                Intervalo(
                    de_label=a.label,
                    ate_label=label_b,
                    anos=delta_anos,
                    prazo=resultado.prazo,
                    prescreveu=prescreveu,
                )
            )

    # Passo 4: classificar risco
    if resultado.intervalos:
        if any(iv.prescreveu for iv in resultado.intervalos):
            resultado.risco = "CONSUMADA"
            resultado.alertas.append(
                "PRESCRICAO POSSIVELMENTE CONSUMADA - pelo menos um intervalo supera o prazo do "
                "art. 109 CP. Argua em peca imediatamente e solicite extincao da punibilidade."
            )
        else:
            max_pct = max(iv.anos / iv.prazo for iv in resultado.intervalos if iv.prazo)
            if max_pct >= 0.80:
                resultado.risco = "ALTO"
                resultado.alertas.append(
                    f"Risco ALTO: pelo menos um intervalo atingiu {round(max_pct*100)}% do "
                    "prazo prescricional. Monitore semanalmente e acione arguicao preventiva "
                    "se houver paralisia do processo."
                )
            elif max_pct >= 0.50:
                resultado.risco = "MODERADO"
                resultado.alertas.append(
                    f"Risco MODERADO: intervalo mais longo com {round(max_pct*100)}% do prazo. "
                    "Acompanhe a movimentacao processual regularmente."
                )
            else:
                resultado.risco = "BAIXO"
    elif not resultado.marcos:
        resultado.alertas.append(
            "Nao foi possivel identificar datas de marcos interruptivos nos trechos recuperados. "
            "Revise manualmente as folhas do processo."
        )
    elif not resultado.prazo:
        resultado.alertas.append(
            "Pena maxima em abstrato nao identificada automaticamente. "
            "Informe o tipo penal e a pena para calcular o prazo prescricional."
        )

    return resultado


# ---------------------------------------------------------------------------
# Formatacao para o prompt do LLM
# ---------------------------------------------------------------------------

def formatar_para_prompt(r: ResultadoPrescricao) -> str:
    """
    Converte ResultadoPrescricao em texto estruturado para incluir no prompt da LLM.
    A LLM recebe dados ja calculados e apenas confirma, corrige ou enriquece.
    """
    linhas = ["=== RESULTADO DO MOTOR DETERMINISTICO DE PRESCRICAO ===\n"]

    linhas.append("Marcos interruptivos identificados (CP art. 117):")
    if r.marcos:
        linhas.append("| Marco | Data | fls. |")
        linhas.append("|---|---|---|")
        for m in r.marcos:
            data_str = m.data.strftime("%d/%m/%Y") if m.data else "Nao localizada"
            linhas.append(f"| {m.label} | {data_str} | {m.pagina} |")
    else:
        linhas.append("Nenhum marco identificado automaticamente.")

    linhas.append("")
    pena_str = f"{r.pena_max} anos" if r.pena_max else "Nao identificada"
    prazo_str = f"{r.prazo} anos" if r.prazo else "Nao calculado"
    linhas.append(f"Pena maxima em abstrato: {pena_str}")
    linhas.append(f"Prazo prescricional (art. 109 CP): {prazo_str}")

    if r.intervalos:
        linhas.append("")
        linhas.append("Intervalos calculados (Python puro - sem estimativa da LLM):")
        linhas.append("| De | Ate | Anos decorridos | Prazo | % do prazo | Status |")
        linhas.append("|---|---|---|---|---|---|")
        for iv in r.intervalos:
            status = "PRESCREVEU" if iv.prescreveu else "Dentro do prazo"
            linhas.append(
                f"| {iv.de_label} | {iv.ate_label} | {iv.anos} | {iv.prazo} anos "
                f"| {iv.percentual}% | {status} |"
            )

    linhas.append("")
    linhas.append(f"Nivel de risco calculado: {r.risco}")

    if r.alertas:
        linhas.append("")
        linhas.append("Alertas:")
        for alerta in r.alertas:
            linhas.append(f"- {alerta}")

    linhas.append("")
    linhas.append("=== FIM DO CALCULO AUTOMATICO ===")
    return "\n".join(linhas)


def serializar(r: ResultadoPrescricao) -> Dict:
    """Serializa o resultado para armazenar nos metadados dos sources."""
    return {
        "type": "prescricao_engine",
        "risco": r.risco,
        "pena_max": r.pena_max,
        "prazo": r.prazo,
        "hoje": r.hoje.isoformat(),
        "alertas": r.alertas,
        "marcos": [
            {
                "tipo": m.tipo,
                "label": m.label,
                "data": m.data.isoformat() if m.data else None,
                "pagina": m.pagina,
                "trecho": m.trecho,
            }
            for m in r.marcos
        ],
        "intervalos": [
            {
                "de_label": iv.de_label,
                "ate_label": iv.ate_label,
                "anos": iv.anos,
                "prazo": iv.prazo,
                "percentual": iv.percentual,
                "prescreveu": iv.prescreveu,
            }
            for iv in r.intervalos
        ],
    }
