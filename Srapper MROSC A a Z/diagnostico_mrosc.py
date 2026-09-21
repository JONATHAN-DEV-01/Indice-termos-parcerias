"""
diagnostico_mrosc.py  (v3 - definitivo)
========================================
Script de DIAGNOSTICO -- nao altera nenhum arquivo.

Estrategia de deteccao de secoes (revisada):
  - As paginas divisorias sao MUITO CURTAS (< 60 chars) e contem o titulo.
  - "indice"    -> contem "onde encontrar o termo"
  - "glossario" -> contem "gloss" E pagina > divisoria do indice (evita capa)
  - "lei"       -> contem "texto da lei"
  - "institucional" -> tudo antes do indice (pag 1 em diante)

Dependencias:
    pip install pdfplumber
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Configuracao de ruido
# ---------------------------------------------------------------------------
FOOTER_FONT_SUFFIX   = "BricolageGrotesque24ptSemiCondensed-SemiBold"
PAGE_NUM_FONT_SUFFIX = "Lato-Bold"
PAGE_NUM_SIZE        = 8.0
PAGE_NUM_SIZE_TOL    = 0.6
PAGE_NUM_MAX_CHARS   = 4
DIVIDER_MAX_CHARS    = 60   # paginas com menos chars sao candidatas a divisoria

def _font_base(fontname: str) -> str:
    """Remove prefixo de subconjunto (ex.: 'ONITTQ+Lato-Regular' -> 'Lato-Regular')."""
    return fontname.split("+", 1)[-1] if "+" in fontname else fontname

def _is_noise(fontname: str, size: float, text: str) -> bool:
    base = _font_base(fontname)
    if base == FOOTER_FONT_SUFFIX:
        return True
    stripped = text.strip()
    if (
        base == PAGE_NUM_FONT_SUFFIX
        and abs(size - PAGE_NUM_SIZE) <= PAGE_NUM_SIZE_TOL
        and stripped.isdigit()
        and len(stripped) <= PAGE_NUM_MAX_CHARS
    ):
        return True
    return False

# ---------------------------------------------------------------------------
# Tipos auxiliares
# ---------------------------------------------------------------------------

class FontKey(NamedTuple):
    fontname: str
    size: float

class FontStats:
    MAX_SAMPLES    = 4
    MAX_SAMPLE_LEN = 120

    def __init__(self) -> None:
        self.pages: set[int] = set()
        self.samples: list[str] = []

    def add(self, page_num: int, text: str) -> None:
        self.pages.add(page_num)
        if len(self.samples) < self.MAX_SAMPLES and text.strip():
            snippet = text.strip()[:self.MAX_SAMPLE_LEN]
            if snippet not in self.samples:
                self.samples.append(snippet)

# ---------------------------------------------------------------------------
# Deteccao de secoes
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return " ".join(text.lower().split())

def detect_sections(pdf_path: Path):
    """
    Retorna (page_section_fn, dividers, total_pages).

    Logica:
      1. Varre paginas em ordem.
      2. Para cada pagina curta (< DIVIDER_MAX_CHARS):
         - Se contem "onde encontrar o termo" -> divisoria do INDICE
         - Se contem "gloss" E pagina > divisoria_do_indice -> divisoria do GLOSSARIO
         - Se contem "texto da lei" -> divisoria da LEI
      3. A secao INSTITUCIONAL vai de 1 ate pagina_antes_do_indice.
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("pdfplumber nao esta instalado. Execute: pip install pdfplumber") from exc

    print(f"\nAbrindo PDF: {pdf_path}")
    print("Detectando paginas divisorias...\n")

    dividers: dict[str, int] = {}

    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        print(f"  Total de paginas: {total}\n")

        for page_index in range(total):
            page = pdf.pages[page_index]
            page_num = page_index + 1
            raw = page.extract_text() or ""
            chars_count = len(raw.strip())

            if chars_count >= DIVIDER_MAX_CHARS:
                continue

            norm = _normalize(raw)

            # Indice
            if "indice" not in dividers and "onde encontrar o termo" in norm:
                dividers["indice"] = page_num
                print(f"  [OK] Divisoria 'indice'    : pagina {page_num:3d} ({chars_count} chars) | {raw.strip()!r}")

            # Glossario: so detecta APOS o indice para evitar a capa
            elif (
                "glossario" not in dividers
                and "indice" in dividers
                and page_num > dividers["indice"]
                and "gloss" in norm
            ):
                dividers["glossario"] = page_num
                print(f"  [OK] Divisoria 'glossario' : pagina {page_num:3d} ({chars_count} chars) | {raw.strip()!r}")

            # Lei
            elif "lei" not in dividers and "texto da lei" in norm:
                dividers["lei"] = page_num
                print(f"  [OK] Divisoria 'lei'       : pagina {page_num:3d} ({chars_count} chars) | {raw.strip()!r}")

    print()

    si = dividers.get("indice")
    sg = dividers.get("glossario")
    sl = dividers.get("lei")

    def page_section(p: int) -> str:
        # Ordem de verificacao: mais restritiva primeiro
        if sl and p >= sl:
            return "lei"
        if sg and p >= sg:
            return "glossario"
        if si and p >= si:
            return "indice"
        return "institucional"

    return page_section, dividers, total

# ---------------------------------------------------------------------------
# Varredura de combinacoes de fontes por secao
# ---------------------------------------------------------------------------

def scan_fonts_by_section(pdf_path: Path, page_section_fn, total_pages: int):
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("pdfplumber nao esta instalado.") from exc

    result: dict[str, dict] = {
        "institucional": defaultdict(FontStats),
        "indice":        defaultdict(FontStats),
        "glossario":     defaultdict(FontStats),
        "lei":           defaultdict(FontStats),
    }

    print("Coletando combinacoes (fontname, size) por secao...")
    print("(pode levar alguns minutos para 400+ paginas)\n")

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_index in range(total_pages):
            page_num = page_index + 1
            section  = page_section_fn(page_num)
            page     = pdf.pages[page_index]

            chars = page.chars
            if not chars:
                continue

            group_texts: dict[FontKey, list[str]] = defaultdict(list)
            for ch in chars:
                fn_raw = ch.get("fontname", "") or ""
                fn     = _font_base(fn_raw)
                size   = round(float(ch.get("size", 0) or 0), 1)
                text   = ch.get("text", "") or ""
                if _is_noise(fn_raw, size, text):
                    continue
                key = FontKey(fontname=fn, size=size)
                group_texts[key].append(text)

            for fkey, chars_list in group_texts.items():
                snippet = "".join(chars_list)
                result[section][fkey].add(page_num, snippet)

            if page_num % 50 == 0:
                print(f"  ... pagina {page_num}/{total_pages}")

    return result

# ---------------------------------------------------------------------------
# Impressao do relatorio
# ---------------------------------------------------------------------------

SECTION_LABELS = {
    "institucional": "SECAO 1 -- Institucional (capa, ficha, sumario, introducao)",
    "indice":        "SECAO 2 -- Indice Remissivo ('Onde encontrar o termo')",
    "glossario":     "SECAO 3 -- Glossario (definicoes de A a Z)",
    "lei":           "SECAO 4 -- Texto da Lei nr 13.019/2014",
}
SECTION_ORDER = ["institucional", "indice", "glossario", "lei"]

def print_report(dividers, total_pages, font_data):
    sep  = "=" * 78
    sep2 = "-" * 78

    print(f"\n{sep}")
    print("  RELATORIO DE DIAGNOSTICO -- MROSC_final.pdf  (v3)")
    print(sep)

    si = dividers.get("indice")
    sg = dividers.get("glossario")
    sl = dividers.get("lei")

    inst_end = (si - 1) if si else "?"
    ind_end  = (sg - 1) if sg else ((sl - 1) if sl else "?")
    glo_end  = (sl - 1) if sl else "?"

    print("\n  LIMITES DE SECAO DETECTADOS:")
    print(sep2)
    print(f"  Secao 1 -- Institucional : pags.   1 - {inst_end}")
    print(f"  Secao 2 -- Indice        : pags. {si or '?':>3} - {ind_end}")
    print(f"  Secao 3 -- Glossario     : pags. {sg or '?':>3} - {glo_end}")
    print(f"  Secao 4 -- Lei           : pags. {sl or '?':>3} - {total_pages}")
    print(f"\n  (Referencia esperada:    pags.   1-32,  33-123,  124-356,  357-403)")
    print(f"  Total de paginas no PDF  : {total_pages}")

    for sec_key in SECTION_ORDER:
        label = SECTION_LABELS[sec_key]
        data  = font_data.get(sec_key, {})

        print(f"\n{sep}")
        print(f"  {label}")
        print(sep)

        if not data:
            print("  (nenhum caractere encontrado nesta secao)")
            continue

        sorted_items = sorted(
            data.items(),
            key=lambda kv: (-len(kv[1].pages), -kv[0].size, kv[0].fontname),
        )

        print(f"  {'FONTE (sem prefixo)':<55} {'TAMANHO':>7}  {'Nx PAGS':>7}")
        print(sep2)

        for fkey, stats in sorted_items:
            print(f"  {fkey.fontname:<55} {fkey.size:>7.1f}  {len(stats.pages):>7}")
            for sample in stats.samples:
                display = sample.replace("\n", " ").strip()
                if len(display) > 100:
                    display = display[:97] + "..."
                print(f"      >>> {display!r}")

    print(f"\n{sep}")
    print("  FIM DO DIAGNOSTICO")
    print(f"  Verifique se os limites batem com: 1-32, 33-123, 124-356, 357-403.")
    print(sep)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Diagnostico de secoes e fontes do MROSC_final.pdf (somente leitura).",
    )
    parser.add_argument(
        "--pdf", metavar="ARQUIVO", type=Path,
        default=Path(__file__).parent / "MROSC_final.pdf",
        help="Caminho do PDF (padrao: MROSC_final.pdf ao lado deste script).",
    )
    return parser.parse_args(argv)

def main(argv=None):
    args = parse_args(argv)
    pdf_path: Path = args.pdf
    if not pdf_path.exists():
        print(f"ERRO: arquivo nao encontrado: {pdf_path}", file=sys.stderr)
        return 1

    try:
        page_section_fn, dividers, total_pages = detect_sections(pdf_path)
    except ImportError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    missing = [k for k in ("indice", "glossario", "lei") if k not in dividers]
    if missing:
        print(f"\n[ATENCAO] Divisorias NAO encontradas: {missing}")
        print("  Ajuste os marcadores ou o limiar DIVIDER_MAX_CHARS.\n")

    try:
        font_data = scan_fonts_by_section(pdf_path, page_section_fn, total_pages)
    except ImportError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    print_report(dividers, total_pages, font_data)
    return 0

if __name__ == "__main__":
    sys.exit(main())
