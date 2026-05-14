"""
Extracao de texto de PDF e chunking, preservando a pagina de origem.
"""

import io
import re
from typing import List, Dict

import fitz  # pymupdf

import config


def extract_pages(file_bytes: bytes) -> List[Dict]:
    """
    Recebe o conteudo bruto do PDF e retorna [{"page_num", "text"}].
    Descarta paginas em branco / com menos de 30 chars uteis.
    """
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
    text = re.sub(r"(?<=[a-zA-ZÀ-ſ,;:])\n(?=[a-zA-ZÀ-ſ])", " ", text)
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
