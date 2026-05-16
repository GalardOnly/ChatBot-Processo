"""
Extracao de texto de PDF e chunking, preservando a pagina de origem.

Hardening (auditoria):
- A1: validacao de magic bytes antes do PyMuPDF
- P3.10: detecta PDF protegido por senha e levanta erro claro
- P3.13: scan de upload via security.scan_uploaded_file (plugavel)
- P1.4: MAX_PAGES e MAX_CHUNKS aplicados aqui
- P3.11: opcao de isolar PyMuPDF em subprocess (config.ISOLATE_PDF_PARSING)
"""

import io
import json
import re
import subprocess
import sys
from typing import List, Dict

import fitz  # pymupdf

import config
import security


# Magic bytes de PDFs validos (primeiros 4 bytes) - Seguranca A1
_PDF_MAGIC = b"%PDF"
_PDF_MIN_BYTES = 100


def _validate_pdf_bytes(file_bytes: bytes) -> None:
    """
    Valida o conteudo do arquivo antes de passar ao PyMuPDF.
    A1: impede que arquivos maliciosos renomeados para .pdf cheguem ao parser.
    P3.13: aciona scan_uploaded_file (stub plugavel).
    """
    if len(file_bytes) < _PDF_MIN_BYTES:
        raise ValueError(
            "Arquivo muito pequeno ou corrompido. Verifique se o PDF esta integro."
        )
    max_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise ValueError(
            f"Arquivo excede o limite de {config.MAX_FILE_SIZE_MB} MB."
        )
    if not file_bytes.startswith(_PDF_MAGIC):
        raise ValueError(
            "O arquivo enviado nao e um PDF valido. "
            "Certifique-se de enviar um PDF nao protegido por senha e nao corrompido."
        )

    # Scan antivirus (interface plugavel - hoje no-op se SCAN_ENGINE=none)
    scan = security.scan_uploaded_file(file_bytes)
    if not scan.clean:
        raise ValueError(
            "Arquivo bloqueado pelo scan de seguranca. "
            "Se voce confia neste documento, contate o administrador."
        )


# =========================================================================
# Extracao - dois caminhos: direto (default) ou subprocess isolado
# =========================================================================

def extract_pages(file_bytes: bytes) -> List[Dict]:
    """
    Extrai paginas do PDF. Sempre valida magic bytes + scan + senha.
    Se config.ISOLATE_PDF_PARSING = True, executa PyMuPDF em subprocess
    com timeout (P3.11) para conter CVEs do parser.
    """
    _validate_pdf_bytes(file_bytes)
    if config.ISOLATE_PDF_PARSING:
        return _extract_pages_subprocess(file_bytes)
    return _extract_pages_inproc(file_bytes)


def _extract_pages_inproc(file_bytes: bytes) -> List[Dict]:
    """Extracao no proprio processo (mais rapida, sem isolamento)."""
    try:
        doc = fitz.open(stream=io.BytesIO(file_bytes), filetype="pdf")
    except Exception as e:
        security.safe_log_warning("[pdf] fitz.open falhou", e)
        raise ValueError("Nao foi possivel abrir o PDF. O arquivo pode estar corrompido.")

    # P3.10: PDF protegido por senha
    if getattr(doc, "is_encrypted", False) or getattr(doc, "needs_pass", False):
        doc.close()
        raise ValueError(
            "Este PDF esta protegido por senha. Remova a protecao em um leitor "
            "(Adobe Reader, navegador) e tente novamente."
        )

    # P1.4: limite de paginas
    page_count = doc.page_count
    if page_count > config.MAX_PAGES:
        doc.close()
        raise ValueError(
            f"PDF tem {page_count} paginas, acima do limite de {config.MAX_PAGES}. "
            "Divida o processo em arquivos menores."
        )

    pages: List[Dict] = []
    try:
        for page_num, page in enumerate(doc, start=1):
            text = _clean_text(page.get_text("text"))
            if len(text.strip()) >= 30:
                pages.append({"page_num": page_num, "text": text})
    finally:
        doc.close()
    return pages


def _extract_pages_subprocess(file_bytes: bytes) -> List[Dict]:
    """
    Executa PyMuPDF em subprocess Python isolado, com timeout.
    Audit P3.11: contem CVEs do parser sem derrubar o app principal.
    """
    cmd = [sys.executable, "-c", _PDF_WORKER_SCRIPT]
    try:
        result = subprocess.run(
            cmd,
            input=file_bytes,
            capture_output=True,
            timeout=config.PDF_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"O processamento do PDF excedeu {config.PDF_SUBPROCESS_TIMEOUT_S}s. "
            "O arquivo pode ser muito complexo ou estar corrompido."
        )
    except Exception as e:
        security.safe_log_warning("[pdf] subprocess falhou", e)
        raise ValueError("Falha ao processar o PDF em ambiente isolado.")

    if result.returncode != 0:
        # stderr pode conter PII (path), sanitizar antes de logar
        security.safe_log_warning("[pdf] worker exit nao zero", result.stderr.decode("utf-8", "ignore"))
        raise ValueError("PDF nao pode ser processado. Verifique se nao esta corrompido ou criptografado.")

    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except Exception:
        raise ValueError("Resposta invalida do processador de PDF.")

    if payload.get("error"):
        raise ValueError(payload["error"])

    return payload.get("pages", [])


# Script Python executado em subprocess. Le bytes do stdin, escreve JSON no stdout.
_PDF_WORKER_SCRIPT = r"""
import sys, io, json, re, unicodedata
import fitz

def clean(t):
    t = re.sub(r"(?<=[a-zA-Z\xC0-\xFF,;:])\n(?=[a-zA-Z\xC0-\xFF])", " ", t)
    t = re.sub(r" {2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

data = sys.stdin.buffer.read()
try:
    doc = fitz.open(stream=io.BytesIO(data), filetype="pdf")
except Exception as e:
    print(json.dumps({"error": "PDF invalido."}))
    sys.exit(0)

if getattr(doc, "is_encrypted", False) or getattr(doc, "needs_pass", False):
    doc.close()
    print(json.dumps({"error": "PDF protegido por senha."}))
    sys.exit(0)

pages = []
for i, p in enumerate(doc, start=1):
    t = clean(p.get_text("text"))
    if len(t.strip()) >= 30:
        pages.append({"page_num": i, "text": t})
doc.close()
print(json.dumps({"pages": pages}, ensure_ascii=False))
"""


# =========================================================================
# Chunking (P1.4: MAX_CHUNKS)
# =========================================================================

def chunk_pages(pages: List[Dict]) -> List[Dict]:
    """Divide o texto em chunks de ~CHUNK_SIZE palavras com overlap."""
    chunks: List[Dict] = []
    chunk_index = 0

    for page in pages:
        paragraphs = _split_paragraphs(page["text"])
        buffer = ""
        buf_page = page["page_num"]

        for para in paragraphs:
            words = para.split()

            if len(words) > config.CHUNK_SIZE:
                if buffer:
                    chunks.append(_make(chunk_index, buffer, buf_page))
                    chunk_index += 1
                    buffer = ""
                step = max(1, config.CHUNK_SIZE - config.CHUNK_OVERLAP)
                for i in range(0, len(words), step):
                    piece = " ".join(words[i : i + config.CHUNK_SIZE])
                    chunks.append(_make(chunk_index, piece, page["page_num"]))
                    chunk_index += 1
                    # Aborto antecipado se exceder o limite
                    if chunk_index > config.MAX_CHUNKS:
                        raise ValueError(
                            f"Processo gerou mais de {config.MAX_CHUNKS} blocos. "
                            "Divida o PDF em arquivos menores ou aumente MAX_CHUNKS no config."
                        )
                continue

            candidate = (buffer + " " + para).strip()
            if len(candidate.split()) <= config.CHUNK_SIZE:
                buffer = candidate
            else:
                if buffer:
                    chunks.append(_make(chunk_index, buffer, buf_page))
                    chunk_index += 1
                buffer = para
                buf_page = page["page_num"]

        if buffer:
            chunks.append(_make(chunk_index, buffer, page["page_num"]))
            chunk_index += 1

        if chunk_index > config.MAX_CHUNKS:
            raise ValueError(
                f"Processo gerou mais de {config.MAX_CHUNKS} blocos. "
                "Divida o PDF em arquivos menores ou aumente MAX_CHUNKS no config."
            )

    return chunks


# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    text = re.sub(r"(?<=[a-zA-Z\xC0-\xFF,;:])\n(?=[a-zA-Z\xC0-\xFF])", " ", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_paragraphs(text: str) -> List[str]:
    parts = re.split(r"\n{2,}", text)
    return [p.strip() for p in parts if len(p.strip()) > 30]


def _make(index: int, text: str, page_num: int) -> Dict:
    text = text.strip()
    return {
        "chunk_index": index,
        "text": text,
        "page_num": page_num,
        "word_count": len(text.split()),
    }
