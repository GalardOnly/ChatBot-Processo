"""
Extracao de texto de PDF e chunking, preservando a pagina de origem.
"""

import io
import re
from typing import List, Dict

import fitz  # pymupdf

import config

# Magic bytes de PDFs validos (primeiros 4 bytes) - Seguranca A1
_PDF_MAGIC = b'%PDF'
# Tamanho minimo razoavel para um PDF nao corrompido
_PDF_MIN_BYTES = 100


def _validate_pdf_bytes(file_bytes: bytes) -> None:
    """
    Valida o conteudo do arquivo antes de passar ao PyMuPDF.
    Seguranca A1: impede que arquivos maliciosos renomeados para .pdf
    cheguem ao parser (que tem historico de CVEs).
    Levanta ValueError com mensagem amigavel se o arquivo for invalido.
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


def extract_pages(file_bytes: bytes) -> List[Dict]:
    """
    Recebe o conteudo bruto do PDF e retorna [{"page_num", "text"}].
    Descarta paginas em branco / com menos de 30 chars uteis.
    Valida magic bytes antes de abrir com PyMuPDF (seguranca A1).
    """
    _validate_pdf_bytes(file_bytes)
    doc = fitz.open(stream=io.BytesIO(file_bytes), filetype="pdf")
    pages: List[Dict] = []
    for page_num, page in enumerate(doc, start=1):
        text = _clean_text(page.get_text("text"))
        if len(text.strip()) >= 30:
            pages.append({"page_num": page_num, "text": text})
    doc.close()
    return pages


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
