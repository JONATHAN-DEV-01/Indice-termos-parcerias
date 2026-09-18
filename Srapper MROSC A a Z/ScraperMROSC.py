"""
scraper-documentos.py  (entrypoint: ScraperMROSC.py)
=====================================================
Extrai texto página a página de documentos PDF listados em um arquivo de
configuração JSON e gera um índice (indice-documentos.json) para alimentar
a busca client-side do Portal de Parcerias da Prefeitura de São Paulo.

Cada registro de saída contém:
  - documento : nome de exibição
  - url       : URL pública do PDF (usada no link de resultado de busca)
  - pagina    : número da página (1-based) dentro do PDF
  - conteudo  : texto limpo extraído dessa página

Uso:
    python ScraperMROSC.py                              # usa config e saída padrão
    python ScraperMROSC.py -c documentos.json           # config personalizada
    python ScraperMROSC.py -o meu-indice.json           # saída personalizada
    python ScraperMROSC.py -c docs.json -o idx.json     # ambos
    python ScraperMROSC.py --min-chars 100              # limiar de suspeita
    python ScraperMROSC.py --preview                    # mostra antes/depois sem gravar

Dependências:
    pip install pymupdf
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Configuração padrão embutida
# ---------------------------------------------------------------------------
# Edite este bloco para adicionar novos documentos SEM alterar a lógica do
# script.  Você também pode apontar para um JSON externo com -c / --config.
# ---------------------------------------------------------------------------

DEFAULT_DOCUMENTS: list[dict] = [
    {
        "nome": "MROSC de A a Z",
        "arquivo": "MROSC_final.pdf",   # caminho relativo ao diretório do script
                                        # ou absoluto; ajuste conforme o servidor
        "url": (
            "https://hom-portaldeparcerias.prefeitura.sp.gov.br"
            "/wp-content/uploads/2026/09/MROSC_final.pdf"
        ),
    },
    # Adicione outros documentos aqui seguindo o mesmo formato:
    # {
    #     "nome": "Nome de Exibição",
    #     "arquivo": "caminho/para/arquivo.pdf",
    #     "url": "https://...",
    # },
]

# Limiar padrão de caracteres por página abaixo do qual a página é sinalizada
# como suspeita (possível scan sem camada de texto).
DEFAULT_MIN_CHARS: int = 80

# Caminho padrão de saída (relativo ao diretório de trabalho atual).
DEFAULT_OUTPUT: str = "indice-documentos.json"

# ---------------------------------------------------------------------------
# Remoção de boilerplate (cabeçalhos/rodapés repetidos)
# ---------------------------------------------------------------------------
#
# Limiar de frequência para considerar uma linha como boilerplate.
# Uma linha (normalizada) que aparece em >= BOILERPLATE_THRESHOLD das páginas
# de um documento é candidata a âncora de corte.
BOILERPLATE_THRESHOLD: float = 0.70

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
    """Configuração de um único documento PDF a ser processado."""

    nome: str
    arquivo: Path
    url: str


@dataclass
class PageRecord:
    """Registro de uma página extraída, pronto para serialização JSON."""

    documento: str
    url: str
    pagina: int       # 1-based
    conteudo: str


@dataclass
class RunSummary:
    """Acumulador de métricas para o resumo final da execução."""

    documentos_processados: int = 0
    documentos_com_erro: int = 0
    total_paginas: int = 0
    total_caracteres_antes: int = 0
    total_caracteres_depois: int = 0
    paginas_suspeitas: list[dict] = field(default_factory=list)

# ---------------------------------------------------------------------------
# Carregamento de configuração
# ---------------------------------------------------------------------------


def load_config(config_path: Path | None) -> list[DocumentConfig]:
    """Carrega a lista de documentos a processar.

    Se *config_path* for fornecido, lê um JSON externo com a lista.
    Caso contrário, utiliza DEFAULT_DOCUMENTS definido no topo do script.

    O JSON externo deve ser uma lista de objetos com as chaves:
        "nome"    – nome de exibição
        "arquivo" – caminho do PDF (absoluto ou relativo ao JSON)
        "url"     – URL pública do PDF

    Levanta ValueError se o JSON estiver malformado.
    """
    if config_path is not None:
        logger.info("Carregando configuração de: %s", config_path)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        base_dir = config_path.parent
    else:
        logger.info("Usando configuração embutida (DEFAULT_DOCUMENTS).")
        raw = DEFAULT_DOCUMENTS
        base_dir = Path(__file__).parent   # relativo ao próprio script

    documents: list[DocumentConfig] = []
    for entry in raw:
        for key in ("nome", "arquivo", "url"):
            if key not in entry:
                raise ValueError(
                    "Entrada de configuração sem a chave obrigatória "
                    f"'{key}': {entry!r}"
                )
        arquivo = Path(entry["arquivo"])
        if not arquivo.is_absolute():
            arquivo = (base_dir / arquivo).resolve()
        documents.append(DocumentConfig(
            nome=entry["nome"],
            arquivo=arquivo,
            url=entry["url"],
        ))

    logger.info("Total de documentos configurados: %d", len(documents))
    return documents

# ---------------------------------------------------------------------------
# Extração de texto via PyMuPDF
# ---------------------------------------------------------------------------


def open_pdf(path: Path):
    """Abre um documento PDF com PyMuPDF e retorna o objeto Document.

    Levanta ImportError se pymupdf não estiver instalado, e
    RuntimeError se o arquivo não puder ser aberto (corrompido, criptografado,
    formato inválido, etc.).
    """
    try:
        import pymupdf as fitz  # pymupdf >= 1.24 expõe o namespace pymupdf diretamente
    except ImportError:
        try:
            import fitz  # fallback para versões mais antigas
        except ImportError as exc:
            raise ImportError(
                "PyMuPDF não está instalado. Execute: pip install pymupdf"
            ) from exc

    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        raise RuntimeError(
            f"Não foi possível abrir o PDF '{path}': {exc}"
        ) from exc

    return doc


def extract_pages_text(path: Path) -> Iterator[tuple[int, str]]:
    """Itera pelas páginas do PDF e gera tuplas (numero_pagina_1based, texto_bruto).

    Utiliza PyMuPDF (fitz) para extração.  O texto retornado ainda não foi
    submetido à limpeza — use clean_text() em seguida.

    IMPORTANTE: abre o documento *antes* de entrar no loop de geração para
    que erros de abertura (arquivo inexistente, corrompido, etc.) sejam
    levantados imediatamente — não de forma lazy na primeira iteração, o que
    dificultaria a captura correta em build_records().

    Levanta RuntimeError (via open_pdf) se o arquivo não puder ser lido.
    """
    # Abre aqui (fora do bloco de yield) para propagar o erro de imediato.
    doc = open_pdf(path)

    def _page_generator() -> Iterator[tuple[int, str]]:
        try:
            for page_index in range(doc.page_count):
                page = doc.load_page(page_index)
                raw_text: str = page.get_text("text")
                yield (page_index + 1, raw_text)
        finally:
            doc.close()

    return _page_generator()

# ---------------------------------------------------------------------------
# Limpeza de texto
# ---------------------------------------------------------------------------

# Padrão para hifenização quebrada no final de linha:
#   "configu-\nração"  →  "configuração"
_HYPHEN_BREAK = re.compile(r"-\n(?=[a-záàâãéèêíïóôõöúüçA-ZÁÀÂÃÉÈÊÍÏÓÔÕÖÚÜÇ])")

# Múltiplos espaços e tabulações → espaço único
_MULTI_SPACE = re.compile(r"[ \t]{2,}")

# Quebras de linha múltiplas (mais de duas consecutivas) → parágrafo duplo
_MULTI_NEWLINE = re.compile(r"\n{3,}")

# Caracteres de controle (exceto \n e \t que já são tratados acima)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(raw: str) -> str:
    """Aplica limpeza básica ao texto extraído de uma página PDF.

    Operações realizadas (por ordem):
    1. Remove caracteres de controle invisíveis.
    2. Recompõe palavras hifenizadas que foram quebradas no fim da linha.
    3. Colapsa múltiplos espaços/tabulações em um único espaço.
    4. Reduz sequências de mais de duas quebras de linha a exatamente duas.
    5. Remove espaços no início e fim do texto.

    Intencionalmente NÃO realiza:
    - Conversão para minúsculas.
    - Remoção ou normalização de acentos.
    - Qualquer outra transformação semântica — essas ficam para o motor JS.
    """
    text = _CONTROL_CHARS.sub("", raw)
    text = _HYPHEN_BREAK.sub("", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()

# ---------------------------------------------------------------------------
# Detecção e remoção de boilerplate (rodapés/cabeçalhos repetidos)
# ---------------------------------------------------------------------------

# Regex para normalizar números isolados em linhas — usada ao comparar padrões
# de linhas de rodapé sem que o número de página variável quebre a detecção.
_DIGITS_ONLY = re.compile(r"^\d+$")


def _normalize_line(line: str) -> str:
    """Normaliza uma linha para comparação de frequência.

    Substitui linhas compostas apenas por dígitos por um placeholder neutro,
    preservando o conteúdo textual real para fins de comparação estrutural.
    """
    stripped = line.strip()
    return "<NUM>" if _DIGITS_ONLY.match(stripped) else stripped


def detect_boilerplate_anchors(
    pages_text: list[str],
    tail_lines: int = 6,
    threshold: float = BOILERPLATE_THRESHOLD,
) -> list[str]:
    """Identifica linhas de rodapé que se repetem em alta proporção das páginas.

    Estratégia:
    - Coleta as últimas *tail_lines* linhas não-vazias de cada página.
    - Normaliza dígitos isolados (número de página variável) para um placeholder.
    - Conta quantas páginas contêm cada linha normalizada.
    - Retorna as linhas que aparecem em >= *threshold* das páginas, ordenadas
      pela posição média em que aparecem (as mais ao final primeiro).

    Essa abordagem é genérica: funciona mesmo que o rodapé mude em PDFs
    futuros — basta ajustar *tail_lines* e *threshold* se necessário.

    Retorna lista vazia se nenhuma linha atingir o limiar.
    """
    total = len(pages_text)
    if total == 0:
        return []

    line_counts: Counter[str] = Counter()
    for text in pages_text:
        non_empty = [l for l in text.split("\n") if l.strip()]
        tail = non_empty[-tail_lines:] if len(non_empty) >= tail_lines else non_empty
        seen_in_page: set[str] = set()
        for line in tail:
            norm = _normalize_line(line)
            if norm and norm not in seen_in_page:
                line_counts[norm] += 1
                seen_in_page.add(norm)

    anchors = [
        line for line, count in line_counts.items()
        if line != "<NUM>" and count / total >= threshold
    ]

    logger.info(
        "Detecção de boilerplate: %d linha(s) acima de %.0f%% de frequência: %s",
        len(anchors),
        threshold * 100,
        anchors,
    )
    return anchors


def strip_boilerplate(text: str, anchors: list[str]) -> str:
    """Remove blocos de rodapé/cabeçalho do texto de uma página.

    Para cada âncora em *anchors*, localiza a primeira ocorrência no texto
    e descarta tudo a partir daí até o final da página.  A âncora deve ser
    uma linha completa — a comparação é feita linha a linha (case-sensitive)
    para evitar cortes acidentais em conteúdo legítimo que contenha o mesmo
    trecho no meio de uma frase.

    Se nenhuma âncora for encontrada na página, o texto é retornado intacto.
    """
    if not anchors:
        return text

    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() in anchors:
            # Remove a linha âncora e tudo que vem depois; descarta espaços
            # em branco que possam ter ficado no final do conteúdo anterior.
            return "\n".join(lines[:i]).rstrip()

    return text


def preview_boilerplate_removal(
    pages: list[tuple[int, str]],
    anchors: list[str],
    n_samples: int = 5,
) -> None:
    """Imprime no console um comparativo antes/depois para revisão visual.

    Seleciona *n_samples* páginas distribuídas ao longo do documento
    (excluindo páginas sem conteúdo), imprime o texto original e o texto
    após remoção do boilerplate, para que o operador verifique que nenhum
    conteúdo legítimo está sendo cortado.
    """
    non_empty = [(pnum, text) for pnum, text in pages if text.strip()]
    if not non_empty:
        print("  (nenhuma página com conteúdo para amostrar)")
        return

    # Seleciona índices uniformemente espaçados
    step = max(1, len(non_empty) // n_samples)
    samples = non_empty[::step][:n_samples]

    sep = "-" * 70
    print(f"\n{'=' * 70}")
    print(f"  PREVIEW — antes/depois da remoção de boilerplate ({n_samples} amostras)")
    print(f"{'=' * 70}")

    for pnum, before in samples:
        after = strip_boilerplate(before, anchors)
        chars_before = len(before)
        chars_after = len(after)
        removed = chars_before - chars_after
        print(f"\n{sep}")
        print(f"  Pagina {pnum}  |  antes: {chars_before} chars -> depois: {chars_after} chars  |  removidos: {removed}")
        print(sep)
        print("  [ANTES — últimas 6 linhas]")
        for line in before.split("\n")[-6:]:
            print("    " + repr(line))
        print("  [DEPOIS — últimas 6 linhas]")
        for line in after.split("\n")[-6:]:
            print("    " + repr(line))

    print(f"\n{'=' * 70}\n")

# ---------------------------------------------------------------------------
# Montagem dos registros de índice
# ---------------------------------------------------------------------------


def build_records(
    config: DocumentConfig,
    min_chars: int,
    summary: RunSummary,
    boilerplate_anchors: list[str] | None = None,
    preview: bool = False,
    n_preview_samples: int = 5,
) -> list[PageRecord]:
    """Extrai e limpa o texto de todas as páginas de um documento PDF.

    Pipeline por página:
    1. Extração bruta via PyMuPDF.
    2. Limpeza de artefatos tipográficos (clean_text).
    3. Detecção de boilerplate por frequência nas páginas limpas do lote.
    4. Remoção do boilerplate de cada página (strip_boilerplate).
    5. Sinalização de páginas suspeitas (poucos caracteres após limpeza).

    Se *preview* for True, exibe comparativo antes/depois sem gravar nada e
    retorna lista vazia (o chamador deve re-invocar com preview=False para
    gerar o índice de verdade).

    Em caso de falha ao abrir o documento, registra o erro em *summary* e
    retorna lista vazia — permitindo que os outros documentos sejam
    processados normalmente.
    """
    records: list[PageRecord] = []

    logger.info("Processando: %s -> %s", config.nome, config.arquivo)

    try:
        page_iter = extract_pages_text(config.arquivo)
    except (RuntimeError, ImportError, OSError) as exc:
        logger.error("ERRO ao abrir '%s': %s", config.nome, exc)
        summary.documentos_com_erro += 1
        return records

    # --- Passo 1 e 2: extração bruta + limpeza tipográfica ---
    raw_pages: list[tuple[int, str]] = []
    try:
        for page_num, raw_text in page_iter:
            raw_pages.append((page_num, clean_text(raw_text)))
    except Exception as exc:
        logger.error(
            "ERRO durante extração de '%s' (a partir da página %d): %s",
            config.nome,
            len(raw_pages) + 1,
            exc,
        )
        summary.documentos_com_erro += 1
        return records

    # --- Passo 3: detectar âncoras de boilerplate (se não fornecidas) ---
    if boilerplate_anchors is None:
        anchors = detect_boilerplate_anchors(
            [text for _, text in raw_pages],
            threshold=BOILERPLATE_THRESHOLD,
        )
    else:
        anchors = boilerplate_anchors

    # --- Passo 4 (preview): mostrar antes/depois sem gravar ---
    if preview:
        preview_boilerplate_removal(raw_pages, anchors, n_samples=n_preview_samples)
        return []

    # --- Passo 4 (produção): remover boilerplate e montar registros ---
    mrosc_before = 0
    mrosc_after = 0

    for page_num, clean in raw_pages:
        chars_before = len(clean)
        final_text = strip_boilerplate(clean, anchors)
        chars_after = len(final_text)

        summary.total_paginas += 1
        summary.total_caracteres_antes += chars_before
        summary.total_caracteres_depois += chars_after

        # Rastrear "MROSC" para comparativo no resumo final
        if "MROSC" in clean:
            mrosc_before += 1
        if "MROSC" in final_text:
            mrosc_after += 1

        if chars_after < min_chars:
            msg = {
                "documento": config.nome,
                "pagina": page_num,
                "caracteres": chars_after,
            }
            summary.paginas_suspeitas.append(msg)
            logger.warning(
                "Página suspeita (possível scan sem texto): %s p.%d — "
                "apenas %d caractere(s) após limpeza.",
                config.nome,
                page_num,
                chars_after,
            )

        records.append(PageRecord(
            documento=config.nome,
            url=config.url,
            pagina=page_num,
            conteudo=final_text,
        ))

    summary.documentos_processados += 1
    logger.info(
        "  OK  %d página(s) extraída(s) de '%s'.",
        len(records),
        config.nome,
    )
    logger.info(
        "  Comparativo 'MROSC': %d páginas antes → %d após remoção de boilerplate.",
        mrosc_before,
        mrosc_after,
    )

    # Guarda para o resumo final (injetado no summary via atributo dinâmico)
    if not hasattr(summary, "_mrosc_stats"):
        summary._mrosc_stats: list[dict] = []  # type: ignore[attr-defined]
    summary._mrosc_stats.append(  # type: ignore[attr-defined]
        {"documento": config.nome, "mrosc_antes": mrosc_before, "mrosc_depois": mrosc_after}
    )

    return records

# ---------------------------------------------------------------------------
# Escrita do JSON de saída
# ---------------------------------------------------------------------------


def write_index(records: list[PageRecord], output_path: Path) -> None:
    """Serializa a lista de PageRecord em um arquivo JSON (UTF-8, indentado).

    Sobrescreve o arquivo caso já exista, garantindo idempotência: o script
    pode ser re-executado a qualquer momento sem limpeza manual prévia.
    """
    payload = [
        {
            "documento": r.documento,
            "url": r.url,
            "pagina": r.pagina,
            "conteudo": r.conteudo,
        }
        for r in records
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Índice gravado em: %s  (%d registro(s))", output_path, len(payload))

# ---------------------------------------------------------------------------
# Resumo final
# ---------------------------------------------------------------------------


def print_summary(summary: RunSummary) -> None:
    """Imprime no stdout um resumo legível da execução do script."""
    sep = "=" * 60
    total_antes = summary.total_caracteres_antes
    total_depois = summary.total_caracteres_depois
    reducao = total_antes - total_depois
    reducao_media = (
        reducao / summary.total_paginas if summary.total_paginas > 0 else 0
    )

    print(f"\n{sep}")
    print("  RESUMO DA EXECUÇÃO")
    print(sep)
    print(f"  Documentos processados com sucesso : {summary.documentos_processados}")
    print(f"  Documentos com erro                : {summary.documentos_com_erro}")
    print(f"  Total de páginas indexadas         : {summary.total_paginas}")
    print(f"  Caracteres antes da limpeza        : {total_antes:,}")
    print(f"  Caracteres após a limpeza          : {total_depois:,}")
    print(f"  Caracteres removidos (boilerplate) : {reducao:,}")
    print(f"  Redução média por página           : {reducao_media:.0f} chars")
    print(f"  Páginas suspeitas (baixo texto)    : {len(summary.paginas_suspeitas)}")

    mrosc_stats = getattr(summary, "_mrosc_stats", [])
    if mrosc_stats:
        print(f"\n  Comparativo de páginas com 'MROSC':")
        for item in mrosc_stats:
            print(
                f"    {item['documento']}: "
                f"{item['mrosc_antes']} paginas antes -> "
                f"{item['mrosc_depois']} apos remocao de boilerplate"
            )

    if summary.paginas_suspeitas:
        print("\n  Páginas sinalizadas como suspeitas:")
        for item in summary.paginas_suspeitas:
            print(
                f"    - {item['documento']}  —  p.{item['pagina']}"
                f"  ({item['caracteres']} caractere(s))"
            )

    print(sep)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define e interpreta os argumentos de linha de comando."""
    parser = argparse.ArgumentParser(
        description=(
            "Extrai texto página a página de PDFs do Portal de Parcerias "
            "e gera indice-documentos.json para a busca client-side."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  python ScraperMROSC.py\n"
            "  python ScraperMROSC.py -c documentos.json -o indice-documentos.json\n"
            "  python ScraperMROSC.py --min-chars 100\n"
            "  python ScraperMROSC.py --preview\n"
        ),
    )
    parser.add_argument(
        "-c", "--config",
        metavar="ARQUIVO_CONFIG",
        type=Path,
        default=None,
        help=(
            "Caminho para um JSON externo com a lista de documentos. "
            "Se omitido, usa DEFAULT_DOCUMENTS definido no topo do script."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        metavar="ARQUIVO_SAIDA",
        type=Path,
        default=None,
        help=(
            "Caminho do JSON de saída. "
            "Padrão sem --preview: indice-documentos.json. "
            "Padrão com --preview (ao confirmar): indice-documentos.novo.json. "
            "O arquivo é sempre sobrescrito."
        ),
    )
    parser.add_argument(
        "--min-chars",
        metavar="N",
        type=int,
        default=DEFAULT_MIN_CHARS,
        help=(
            f"Páginas com menos de N caracteres extraídos são sinalizadas "
            f"como suspeitas no log (padrão: {DEFAULT_MIN_CHARS})."
        ),
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        default=False,
        help=(
            "Imprime antes/depois de 5 páginas de amostra e aguarda "
            "confirmação antes de gerar o índice. "
            "O arquivo de saída padrão neste modo é indice-documentos.novo.json."
        ),
    )
    parser.add_argument(
        "--preview-samples",
        metavar="N",
        type=int,
        default=5,
        help="Número de páginas de amostra exibidas no preview (padrão: 5).",
    )
    return parser.parse_args(argv)

# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Orquestra o pipeline completo de extração e geração do índice.

    Fluxo normal:
      carregar config → extrair páginas → detectar boilerplate →
      preview (se --preview) → remover boilerplate → gravar JSON → resumo

    Retorna 0 em sucesso total, 1 se algum documento falhou (permitindo
    uso em pipelines CI/CD ou agendamentos com verificação de exit code).
    """
    args = parse_args(argv)
    summary = RunSummary()

    # Definir caminho de saída com base no modo
    if args.output is not None:
        output_path = args.output
    elif args.preview:
        output_path = Path("indice-documentos.novo.json")
    else:
        output_path = Path(DEFAULT_OUTPUT)

    # 1. Carregar configuração
    try:
        documents = load_config(args.config)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        logger.error("Falha ao carregar configuração: %s", exc)
        return 1

    if not documents:
        logger.warning("Nenhum documento configurado. Nada a processar.")
        print_summary(summary)
        return 0

    # 2. Modo preview: mostrar antes/depois e aguardar confirmação
    if args.preview:
        print("\n[MODO PREVIEW] Rodando extração e detecção de boilerplate...")
        for doc_config in documents:
            build_records(
                doc_config,
                args.min_chars,
                summary,
                preview=True,
                n_preview_samples=args.preview_samples,
            )

        print(
            f"Arquivo de saída após confirmação: {output_path}\n"
            "Confirma a geração do índice com remoção de boilerplate? [s/N] ",
            end="",
            flush=True,
        )
        resposta = input().strip().lower()
        if resposta not in ("s", "sim", "y", "yes"):
            print("Operação cancelada pelo usuário. Nenhum arquivo foi alterado.")
            return 0

        # Reinicia o summary para a passagem real
        summary = RunSummary()

    # 3. Processar cada documento (passagem real)
    all_records: list[PageRecord] = []
    for doc_config in documents:
        records = build_records(
            doc_config,
            args.min_chars,
            summary,
            preview=False,
        )
        all_records.extend(records)

    # 4. Gravar o índice (idempotente — sempre sobrescreve)
    try:
        write_index(all_records, output_path)
    except OSError as exc:
        logger.error("Falha ao gravar o índice: %s", exc)
        return 1

    # 5. Exibir resumo
    print_summary(summary)

    return 1 if summary.documentos_com_erro > 0 else 0


if __name__ == "__main__":
    sys.exit(main())