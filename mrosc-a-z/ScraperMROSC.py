"""
ScraperMROSC.py  (v2 -- extracao com consciencia de secao)
===========================================================
Extrai texto pagina a pagina de MROSC_final.pdf usando pdfplumber,
classificando cada pagina segundo a secao do documento e separando
titulos de conteudo com base na familia tipografica de cada caractere.

Secoes detectadas automaticamente pelas paginas divisorias:
  Secao 1 -- institucional  (capa, ficha tecnica, sumario, introducao)
  Secao 2 -- indice         (tabela Onde Encontrar o Termo)
  Secao 3 -- glossario      (definicoes A-Z)
  Secao 4 -- lei            (texto da Lei nr 13.019/2014)

Regras de extracao por secao:
  institucional : titulos=[]  conteudo=todo nao-ruido
  indice        : titulos=[]  conteudo=todo nao-ruido
  glossario     : titulos=BricolageGrotesque-Bold ~17.6pt e ~12.9pt
                  discard=BricolageGrotesque-Bold ~48.9pt (letras A B C)
                  conteudo=tudo o mais (incl. boxes 15.4pt)
  lei           : titulos=Lato-Black ~7.5pt (capitulos/secoes da lei)
                  conteudo=tudo o mais

Ruido descartado em todas as secoes:
  - BricolageGrotesque24ptSemiCondensed-SemiBold (rodape)
  - Lato-Bold ~8pt com apenas digitos (numero de pagina)

Saida: indice-documentos.novo.json (NUNCA sobrescreve indice-documentos.json)

Formato de cada registro:
  {
    "documento": "MROSC de A a Z",
    "url": "...",
    "pagina": 255,
    "secao": "glossario",
    "titulos": ["Termo A", "Termo B"],
    "conteudo": "texto corrido limpo"
  }

Uso:
    python ScraperMROSC.py                    # gera indice-documentos.novo.json
    python ScraperMROSC.py --preview          # mostra amostras e pede confirmacao
    python ScraperMROSC.py -o saida.json      # saida personalizada

Dependencias:
    pip install pdfplumber
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuracao padrao embutida
# ---------------------------------------------------------------------------

DEFAULT_DOCUMENTS: list[dict] = [
    {
        "nome": "MROSC de A a Z",
        "arquivo": "MROSC_final.pdf",
        "url": (
            "https://hom-portaldeparcerias.prefeitura.sp.gov.br"
            "/wp-content/uploads/2026/09/MROSC_final.pdf"
        ),
    },
]

DEFAULT_MIN_CHARS: int = 30
DEFAULT_OUTPUT: str = "indice-documentos.novo.json"

# Arquivo que NUNCA deve ser sobrescrito por este script.
_PROTECTED_OUTPUT = "indice-documentos.json"

# ---------------------------------------------------------------------------
# Deteccao de secoes
# ---------------------------------------------------------------------------

# Paginas divisorias sao identificadas por terem pouquissimos caracteres
# (so o numero de pagina duplicado + o titulo da secao).
DIVIDER_MAX_CHARS: int = 60

# ---------------------------------------------------------------------------
# Classificacao de fontes
# ---------------------------------------------------------------------------

# Rodape: BricolageGrotesque24ptSemiCondensed-SemiBold em qualquer tamanho
FOOTER_FONT_SUFFIX = "BricolageGrotesque24ptSemiCondensed-SemiBold"

# Numero de pagina: Lato-Bold ~8pt com 1-4 digitos
PAGE_NUM_FONT_SUFFIX = "Lato-Bold"
PAGE_NUM_SIZE        = 8.0
PAGE_NUM_SIZE_TOL    = 0.6   # aceita 7.4pt a 8.6pt
PAGE_NUM_MAX_CHARS   = 4

# Fonte dos titulos/separadores do glossario
GLOSSARIO_BOLD = "BricolageGrotesque36ptSemiCondensed-Bold"

# Tamanhos que viram TITULO no glossario: (centro_pt, tolerancia_pt)
GLOSSARIO_TITULO_RANGES = [
    (17.6, 1.5),   # nome do verbete
    (12.9, 0.5),   # sub-titulo dentro do verbete
]

# Tamanho descartado no glossario: letra-separador A, B, C...
GLOSSARIO_DISCARD_RANGE = (48.9, 5.0)

# Na secao da lei: Lato-Black ~7.5pt = titulo de capitulo/secao
LEI_TITULO_FONT = "Lato-Black"
LEI_TITULO_SIZE = 7.5
LEI_TITULO_TOL  = 0.5

# Tolerancia Y (em pontos) para considerar chars na mesma linha
Y_SAME_LINE_TOL = 5.0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tipos de dados
# ---------------------------------------------------------------------------


@dataclass
class DocumentConfig:
    """Configuracao de um documento PDF a processar."""
    nome: str
    arquivo: Path
    url: str


@dataclass
class PageRecord:
    """Registro de uma pagina extraida com classificacao de secao."""
    documento: str
    url: str
    pagina: int       # 1-based
    secao: str        # institucional | indice | glossario | lei
    titulos: list     # titulos/verbetes da pagina (lista de strings)
    conteudo: str     # texto corrido limpo


@dataclass
class RunSummary:
    """Acumulador de metricas."""
    documentos_processados: int = 0
    documentos_com_erro: int = 0
    total_paginas: int = 0
    total_chars_conteudo: int = 0
    paginas_suspeitas: list = field(default_factory=list)
    secao_contagem: dict = field(default_factory=dict)

# ---------------------------------------------------------------------------
# Limpeza de texto (aplicada ao conteudo)
# ---------------------------------------------------------------------------

_HYPHEN_BREAK = re.compile(r"-\n(?=[a-z\u00e0-\u00fc])", re.IGNORECASE)
_MULTI_SPACE  = re.compile(r"[ \t]{2,}")
_MULTI_NL     = re.compile(r"\n{3,}")
_CTRL_CHARS   = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(raw: str) -> str:
    """Limpa artefatos tipograficos do texto extraido."""
    text = _CTRL_CHARS.sub("", raw)
    text = _HYPHEN_BREAK.sub("", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()

# ---------------------------------------------------------------------------
# Utilitarios de fonte
# ---------------------------------------------------------------------------


def _font_base(fontname: str) -> str:
    """Remove prefixo de subconjunto (ex.: ONITTQ+Lato-Regular -> Lato-Regular)."""
    return fontname.split("+", 1)[-1] if "+" in fontname else fontname


def _is_noise(fontname_raw: str, size: float, text: str) -> bool:
    """Retorna True para rodape e numero de pagina."""
    base = _font_base(fontname_raw)
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


def _classify_char(font_base: str, size: float, section: str) -> str:
    """Classifica um char como titulo, conteudo ou discard."""
    if section == "glossario":
        if font_base == GLOSSARIO_BOLD:
            dc, dt = GLOSSARIO_DISCARD_RANGE
            if abs(size - dc) <= dt:
                return "discard"
            for tc, tt in GLOSSARIO_TITULO_RANGES:
                if abs(size - tc) <= tt:
                    return "titulo"
            return "conteudo"
        return "conteudo"

    if section == "lei":
        if font_base == LEI_TITULO_FONT and abs(size - LEI_TITULO_SIZE) <= LEI_TITULO_TOL:
            return "titulo"
        return "conteudo"

    # institucional e indice: tudo vai para conteudo, sem titulos
    return "conteudo"

# ---------------------------------------------------------------------------
# Agrupamento de chars em strings
# ---------------------------------------------------------------------------


def _build_lines(chars: list) -> list:
    """Agrupa chars (com categoria em ch['_cat']) em linhas, em ordem de leitura."""
    if not chars:
        return []

    sorted_chars = sorted(chars, key=lambda c: (c.get("top", 0), c.get("x0", 0)))

    lines: list = []
    current: list = []
    line_y = None

    for ch in sorted_chars:
        y = ch.get("top", 0)
        if line_y is None or abs(y - line_y) <= Y_SAME_LINE_TOL:
            current.append(ch)
            if line_y is None:
                line_y = y
        else:
            lines.append(current)
            current = [ch]
            line_y = y

    if current:
        lines.append(current)

    for line in lines:
        line.sort(key=lambda c: c.get("x0", 0))

    return lines


def _lines_to_titles_and_conteudo(lines: list) -> tuple:
    """
    Percorre as linhas da pagina em ordem de leitura e separa titulos de
    conteudo com base na categoria de cada trecho (titulo/conteudo).

    Um titulo que continua na fonte de titulo em linhas seguintes (sem
    conteudo entre elas) e tratado como o MESMO titulo, quebrado em varias
    linhas, e as partes sao juntadas com espaco. Quando a fonte muda para
    conteudo, o titulo atual e fechado; se a fonte voltar a ser titulo mais
    adiante, isso inicia um NOVO titulo.
    """
    titulos: list = []
    conteudo_parts: list = []
    current_title_parts: list = []
    in_title = False

    for line in lines:
        runs: list = []
        run_cat = None
        run_chars: list = []
        for ch in line:
            cat = ch["_cat"]
            if run_cat is None or cat == run_cat:
                run_chars.append(ch)
                run_cat = cat
            else:
                runs.append((run_cat, run_chars))
                run_chars = [ch]
                run_cat = cat
        if run_chars:
            runs.append((run_cat, run_chars))

        for run_cat, run_chars in runs:
            text = "".join(c.get("text", "") for c in run_chars).strip()
            if not text:
                continue

            if run_cat == "titulo":
                current_title_parts.append(text)
                in_title = True
            else:
                if in_title:
                    titulos.append(" ".join(current_title_parts))
                    current_title_parts = []
                    in_title = False
                conteudo_parts.append(text)

    if in_title and current_title_parts:
        titulos.append(" ".join(current_title_parts))

    conteudo = clean_text("\n".join(conteudo_parts))
    return titulos, conteudo

# ---------------------------------------------------------------------------
# Carregamento de configuracao
# ---------------------------------------------------------------------------


def load_config(config_path: Path | None) -> list:
    """Carrega a lista de documentos a processar."""
    if config_path is not None:
        logger.info("Carregando configuracao de: %s", config_path)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        base_dir = config_path.parent
    else:
        logger.info("Usando configuracao embutida (DEFAULT_DOCUMENTS).")
        raw = DEFAULT_DOCUMENTS
        base_dir = Path(__file__).parent

    documents = []
    for entry in raw:
        for key in ("nome", "arquivo", "url"):
            if key not in entry:
                raise ValueError(f"Entrada sem chave obrigatoria '{key}'.: {entry!r}")
        arquivo = Path(entry["arquivo"])
        if not arquivo.is_absolute():
            arquivo = (base_dir / arquivo).resolve()
        documents.append(DocumentConfig(nome=entry["nome"], arquivo=arquivo, url=entry["url"]))

    logger.info("Total de documentos configurados: %d", len(documents))
    return documents

# ---------------------------------------------------------------------------
# Deteccao de secoes
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _detect_sections(pdf_path: Path):
    """
    Varre o PDF e detecta as paginas divisorias de cada secao.

    Retorna (page_section_fn, dividers, total_pages) onde:
      page_section_fn(page_num) -> str  ("institucional"|"indice"|"glossario"|"lei")
      dividers: dict com as primeiras paginas de cada secao
      total_pages: int
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError(
            "pdfplumber nao esta instalado. Execute: pip install pdfplumber"
        ) from exc

    dividers: dict = {}

    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        logger.info("Detectando secoes em %d paginas...", total)

        for page_index in range(total):
            page = pdf.pages[page_index]
            page_num = page_index + 1
            raw = page.extract_text() or ""
            chars_count = len(raw.strip())

            if chars_count >= DIVIDER_MAX_CHARS:
                continue

            norm = _normalize(raw)

            if "indice" not in dividers and "onde encontrar o termo" in norm:
                dividers["indice"] = page_num
                logger.info("  Divisoria indice    : pagina %d", page_num)

            elif (
                "glossario" not in dividers
                and "indice" in dividers
                and page_num > dividers["indice"]
                and "gloss" in norm
            ):
                dividers["glossario"] = page_num
                logger.info("  Divisoria glossario : pagina %d", page_num)

            elif "lei" not in dividers and "texto da lei" in norm:
                dividers["lei"] = page_num
                logger.info("  Divisoria lei       : pagina %d", page_num)

    for key in ("indice", "glossario", "lei"):
        if key not in dividers:
            logger.warning("Divisoria '%s' NAO encontrada!", key)

    si = dividers.get("indice")
    sg = dividers.get("glossario")
    sl = dividers.get("lei")

    def page_section(p: int) -> str:
        if sl and p >= sl:
            return "lei"
        if sg and p >= sg:
            return "glossario"
        if si and p >= si:
            return "indice"
        return "institucional"

    return page_section, dividers, total

# ---------------------------------------------------------------------------
# Extracao de uma pagina
# ---------------------------------------------------------------------------


def _extract_page(page, section: str):
    """
    Extrai (titulos, conteudo, noise_count) de uma pagina pdfplumber.

    titulos      : lista de strings com os titulos encontrados
    conteudo     : texto corrido limpo
    noise_count  : numero de chars descartados como ruido
    """
    chars = page.chars
    kept: list = []
    noise_count = 0

    for ch in chars:
        fn_raw = ch.get("fontname", "") or ""
        fn     = _font_base(fn_raw)
        size   = round(float(ch.get("size", 0) or 0), 1)
        text   = ch.get("text", "") or ""

        if _is_noise(fn_raw, size, text):
            noise_count += 1
            continue

        cat = _classify_char(fn, size, section)

        if cat == "discard":
            noise_count += 1
            continue

        ch2 = dict(ch)
        ch2["_cat"] = cat
        kept.append(ch2)

    lines = _build_lines(kept)
    titulos, conteudo = _lines_to_titles_and_conteudo(lines)
    return titulos, conteudo, noise_count

# ---------------------------------------------------------------------------
# Montagem dos registros
# ---------------------------------------------------------------------------


def build_records(
    config: DocumentConfig,
    min_chars: int,
    summary: RunSummary,
) -> tuple:
    """
    Processa todas as paginas de um PDF e retorna (records, preview_data).

    preview_data: dict {secao: [lista de amostras {pagina, raw, titulos, conteudo, noise}]}
    """
    records: list = []
    preview_data: dict = {"indice": [], "glossario": [], "lei": []}
    PREVIEW_SAMPLES_PER_SECTION = 3   # coletamos 3 e exibimos os 2 melhores

    try:
        import pdfplumber
    except ImportError as exc:
        logger.error("pdfplumber nao instalado: %s", exc)
        summary.documentos_com_erro += 1
        return records, preview_data

    logger.info("Processando: %s -> %s", config.nome, config.arquivo)

    try:
        page_section_fn, dividers, total_pages = _detect_sections(config.arquivo)
    except Exception as exc:
        logger.error("Erro ao detectar secoes em '%s'.: %s", config.nome, exc)
        summary.documentos_com_erro += 1
        return records, preview_data

    logger.info(
        "Secoes: institucional=1-%s | indice=%s-%s | glossario=%s-%s | lei=%s-%d",
        (dividers.get("indice", "?") or 1) - 1,
        dividers.get("indice", "?"),
        (dividers.get("glossario", "?") or 1) - 1,
        dividers.get("glossario", "?"),
        (dividers.get("lei", "?") or 1) - 1,
        dividers.get("lei", "?"),
        total_pages,
    )

    try:
        with pdfplumber.open(str(config.arquivo)) as pdf:
            for page_index in range(total_pages):
                page_num = page_index + 1
                section  = page_section_fn(page_num)
                page     = pdf.pages[page_index]

                titulos, conteudo, noise_count = _extract_page(page, section)

                # Coleta amostras para preview (ate PREVIEW_SAMPLES_PER_SECTION por secao)
                if section in preview_data and len(preview_data[section]) < PREVIEW_SAMPLES_PER_SECTION:
                    if conteudo or titulos:
                        raw_text = page.extract_text() or ""
                        preview_data[section].append({
                            "pagina":      page_num,
                            "raw":         raw_text,
                            "titulos":     titulos,
                            "conteudo":    conteudo,
                            "noise_count": noise_count,
                        })

                chars_total = len(conteudo) + sum(len(t) for t in titulos)
                summary.total_chars_conteudo += chars_total
                summary.total_paginas += 1
                summary.secao_contagem[section] = summary.secao_contagem.get(section, 0) + 1

                if chars_total < min_chars:
                    msg = {"documento": config.nome, "pagina": page_num, "caracteres": chars_total}
                    summary.paginas_suspeitas.append(msg)
                    logger.warning(
                        "Pagina suspeita (pouco texto): %s p.%d -- %d char(s).",
                        config.nome, page_num, chars_total,
                    )

                records.append(PageRecord(
                    documento=config.nome,
                    url=config.url,
                    pagina=page_num,
                    secao=section,
                    titulos=titulos,
                    conteudo=conteudo,
                ))

    except Exception as exc:
        logger.error("Erro durante extracao de '%s'.: %s", config.nome, exc)
        summary.documentos_com_erro += 1
        return [], preview_data

    summary.documentos_processados += 1
    logger.info("  OK  %d paginas extraidas de '%s'.", len(records), config.nome)
    return records, preview_data

# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def print_preview(preview_data: dict) -> None:
    """Imprime antes/depois de paginas de amostra de cada secao."""
    sep_major = "=" * 72
    sep_minor = "-" * 72

    SECAO_LABELS = {
        "indice":    "SECAO 2 -- Indice Remissivo",
        "glossario": "SECAO 3 -- Glossario",
        "lei":       "SECAO 4 -- Texto da Lei",
    }

    print(f"\n{sep_major}")
    print("  PREVIEW -- Extracao com classificacao de secao")
    print(sep_major)

    for secao, label in SECAO_LABELS.items():
        amostras = preview_data.get(secao, [])
        if not amostras:
            print(f"\n[{label}] -- nenhuma amostra coletada.")
            continue

        for i, sample in enumerate(amostras[:2]):  # mostra no maximo 2
            pnum       = sample["pagina"]
            raw        = sample["raw"]
            titulos    = sample["titulos"]
            conteudo   = sample["conteudo"]
            noise      = sample["noise_count"]

            print(f"\n{sep_major}")
            print(f"  {label}  |  Pagina {pnum}  (amostra {i+1})")
            print(sep_major)

            print("\n  [ANTES -- texto bruto da pagina (primeiros 400 chars)]")
            print(sep_minor)
            snippet_raw = (raw[:400] + "...") if len(raw) > 400 else raw
            for line in snippet_raw.splitlines():
                print(f"    {line}")

            print("\n  [DEPOIS -- titulos extraidos]")
            print(sep_minor)
            if titulos:
                for t in titulos:
                    print(f"    * {t!r}")
            else:
                print("    (nenhum -- secao nao tem titulos)")

            print("\n  [DEPOIS -- conteudo (primeiros 400 chars)]")
            print(sep_minor)
            snippet_con = (conteudo[:400] + "...") if len(conteudo) > 400 else conteudo
            for line in snippet_con.splitlines():
                print(f"    {line}")

            print(f"\n  [DESCARTADO como ruido: {noise} chars/tokens (rodape + nr pagina + separadores)]")

    print(f"\n{sep_major}\n")

# ---------------------------------------------------------------------------
# Escrita do JSON de saida
# ---------------------------------------------------------------------------


def write_index(records: list, output_path: Path) -> None:
    """Serializa PageRecord em JSON (novo formato com secao/titulos/conteudo)."""
    # Protecao: nunca sobrescrever o indice publicado
    if output_path.name == _PROTECTED_OUTPUT:
        raise ValueError(
            f"ERRO DE SEGURANCA: tentativa de sobrescrever o arquivo protegido '{_PROTECTED_OUTPUT}'. "
            "Use -o para especificar outro nome de saida."
        )

    payload = [
        {
            "documento": r.documento,
            "url":        r.url,
            "pagina":     r.pagina,
            "secao":      r.secao,
            "titulos":    r.titulos,
            "conteudo":   r.conteudo,
        }
        for r in records
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Indice gravado em: %s  (%d registros)", output_path, len(payload))

# ---------------------------------------------------------------------------
# Resumo final
# ---------------------------------------------------------------------------


def print_summary(summary: RunSummary, output_path: Path | None = None) -> None:
    """Imprime resumo legivel da execucao."""
    sep = "=" * 60
    print(f"\n{sep}")
    print("  RESUMO DA EXECUCAO")
    print(sep)
    print(f"  Documentos processados com sucesso : {summary.documentos_processados}")
    print(f"  Documentos com erro                : {summary.documentos_com_erro}")
    print(f"  Total de paginas indexadas         : {summary.total_paginas}")
    print(f"  Total de chars (conteudo+titulos)  : {summary.total_chars_conteudo:,}")
    print(f"  Paginas suspeitas (pouco texto)    : {len(summary.paginas_suspeitas)}")
    if summary.secao_contagem:
        print("\n  Paginas por secao:")
        for sec, count in sorted(summary.secao_contagem.items()):
            print(f"    {sec:<16} : {count}")
    if output_path:
        print(f"\n  Arquivo de saida: {output_path}")
    if summary.paginas_suspeitas:
        print("\n  Paginas sinalizadas:")
        for item in summary.paginas_suspeitas[:20]:
            print(f"    - {item['documento']}  p.{item['pagina']}  ({item['caracteres']} chars)")
    print(sep)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    """Define e interpreta os argumentos de linha de comando."""
    parser = argparse.ArgumentParser(
        description=(
            "Extrai texto com classificacao de secao de PDFs do Portal de Parcerias "
            "e gera indice-documentos.novo.json para a busca client-side."
        ),
        epilog=(
            "Exemplos:\n"
            "  python ScraperMROSC.py\n"
            "  python ScraperMROSC.py --preview\n"
            "  python ScraperMROSC.py -o meu-indice.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c", "--config",
        metavar="ARQUIVO_CONFIG",
        type=Path,
        default=None,
        help="JSON externo com lista de documentos. Omitido = usa DEFAULT_DOCUMENTS.",
    )
    parser.add_argument(
        "-o", "--output",
        metavar="ARQUIVO_SAIDA",
        type=Path,
        default=None,
        help=f"Arquivo de saida JSON. Padrao: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--min-chars",
        metavar="N",
        type=int,
        default=DEFAULT_MIN_CHARS,
        help=f"Paginas com menos de N chars sao sinalizadas (padrao: {DEFAULT_MIN_CHARS}).",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        default=False,
        help=(
            "Exibe amostras antes/depois por secao e aguarda confirmacao "
            "antes de gravar o indice."),
    )
    return parser.parse_args(argv)

# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    """Orquestra o pipeline completo."""
    args = parse_args(argv)
    summary = RunSummary()

    output_path = args.output if args.output is not None else Path(DEFAULT_OUTPUT)

    # Resolve relativo ao diretorio do script (nao ao CWD)
    if not output_path.is_absolute():
        output_path = Path(__file__).parent / output_path

    # Protecao antecipada
    if output_path.name == _PROTECTED_OUTPUT:
        logger.error(
            "PROTECAO: o arquivo de saida nao pode ser '%s'. "
            "Use -o para especificar outro nome.",
            _PROTECTED_OUTPUT,
        )
        return 1

    try:
        documents = load_config(args.config)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        logger.error("Falha ao carregar configuracao: %s", exc)
        return 1

    if not documents:
        logger.warning("Nenhum documento configurado. Nada a processar.")
        print_summary(summary)
        return 0

    all_records: list = []
    all_preview: dict = {"indice": [], "glossario": [], "lei": []}

    for doc_config in documents:
        records, preview_data = build_records(doc_config, args.min_chars, summary)
        all_records.extend(records)
        for sec, samples in preview_data.items():
            all_preview[sec].extend(samples)

    if args.preview:
        print_preview(all_preview)
        print(f"Arquivo de saida: {output_path}")
        print("Confirma a geracao do indice? [s/N] ", end="", flush=True)
        resposta = input().strip().lower()
        if resposta not in ("s", "sim", "y", "yes"):
            print("Operacao cancelada. Nenhum arquivo foi alterado.")
            return 0

    try:
        write_index(all_records, output_path)
    except (OSError, ValueError) as exc:
        logger.error("Falha ao gravar o indice: %s", exc)
        return 1

    print_summary(summary, output_path)
    return 1 if summary.documentos_com_erro > 0 else 0


if __name__ == "__main__":
    sys.exit(main())