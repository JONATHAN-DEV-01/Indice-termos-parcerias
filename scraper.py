#!/usr/bin/env python3
"""
scraper.py — Crawler para o Portal de Parcerias
Gera o arquivo indice-busca.json com título, URL e conteúdo limpo de cada página.

Uso:
    python scraper.py
    python scraper.py --url https://hom-portaldeparcerias.prefeitura.sp.gov.br/
    python scraper.py --url https://portaldeparcerias.prefeitura.sp.gov.br/ --delay 1.0 --output meu-indice.json

Dependências:
    pip install requests beautifulsoup4 lxml
"""

import argparse
import json
import logging
import re
import sys
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# Configuração de logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("scraper")

# ─────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────
DEFAULT_URL    = "https://hom-portaldeparcerias.prefeitura.sp.gov.br/"
DEFAULT_OUTPUT = "indice-busca.json"
DEFAULT_DELAY  = 0.7  # segundos entre requisições
USER_AGENT     = (
    "Mozilla/5.0 (compatible; PortalParceirasScraper/1.0; "
    "+https://portaldeparcerias.prefeitura.sp.gov.br/)"
)
REQUEST_TIMEOUT = 15  # segundos

# Seletores de elementos a REMOVER antes de extrair o texto
TAGS_TO_REMOVE = [
    "header", "footer", "nav", "script", "style", "noscript",
    "svg", "iframe", "img", "figure", "picture",
]
CLASSES_TO_REMOVE = [
    # Elementor / Royal Addons / HFE navigation
    "wpr-menu-item", "wpr-nav-menu", "wpr-mobile-nav-items",
    "hfe-search-button-wrapper", "hfe-nav-menu",
    "elementor-social-icon", "elementor-social-icons-wrapper",
    # Widgets de busca e breadcrumb
    "elementor-widget-search-form", "elementor-widget-breadcrumb",
    # Barras laterais
    "elementor-widget-sidebar",
    # Botão "Voltar ao topo"
    "elementor-button-back-to-top",
    # Rodapé do portal (contém links de menu, emails repetidos em todas as páginas)
    "elementor-location-footer",
    "hfe-footer",
    "site-footer",
    # Cabeçalho
    "elementor-location-header",
    "hfe-header",
    "site-header",
]

# Tamanho máximo do conteúdo por página (evita JSON muito grande)
MAX_CONTEUDO_CHARS = 15000

# Caminhos a ignorar mesmo sendo HTML (uploads renomeados, páginas internas do WP)
IGNORE_PATH_PREFIXES = [
    "/wp-admin",
    "/wp-content/uploads",
    "/feed",
    "/wp-json",
    "/xmlrpc",
    "/trackback",
    "/embed",
    "/wp-login",
]

# Extensões de arquivo a ignorar durante o crawl
IGNORE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".tar", ".gz",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".mp4", ".mp3", ".avi", ".mov",
    ".css", ".js", ".json", ".xml", ".txt",
    ".woff", ".woff2", ".ttf", ".eot",
}


# ─────────────────────────────────────────────
# Funções auxiliares
# ─────────────────────────────────────────────

def normalizar_url(url: str) -> str:
    """Remove fragmento (#âncora) e query strings de rastreamento."""
    url, _ = urldefrag(url)
    return url.rstrip("/") + "/"


def mesmo_dominio(url: str, base_domain: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc == base_domain


def extensao_ignorada(url: str) -> bool:
    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in IGNORE_EXTENSIONS):
        return True
    # Ignorar caminhos internos do WordPress que não são páginas de conteúdo
    if any(path.startswith(prefix) for prefix in IGNORE_PATH_PREFIXES):
        return True
    return False


def carregar_robots(base_url: str, session: requests.Session) -> RobotFileParser:
    rp = RobotFileParser()
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        resp = session.get(robots_url, timeout=REQUEST_TIMEOUT)
        rp.parse(resp.text.splitlines())
        log.info("robots.txt carregado de %s", robots_url)
    except Exception as exc:
        log.warning("Não foi possível carregar robots.txt: %s", exc)
    return rp


def limpar_shortcodes(texto: str) -> str:
    """Remove shortcodes residuais do WordPress/Elementor como [elementor-template id='123']."""
    return re.sub(r"\[.*?\]", "", texto)


def extrair_conteudo_accordions(container) -> str:
    """
    Extrai explicitamente o texto dos accordions <details>/<summary> do Elementor
    (widget e-n-accordion). Inclui o título da sanfona + o corpo do painel.
    Retorna string com todos os pares "TÍTULO: conteúdo" separados por espaço.
    """
    partes = []
    for details in container.find_all("details", class_=re.compile(r"e-n-accordion-item")):
        # Título da sanfona (dentro do <summary>)
        summary = details.find("summary")
        titulo_sanfona = summary.get_text(separator=" ", strip=True) if summary else ""

        # Clonar o details e remover o summary para pegar só o corpo
        details_clone = BeautifulSoup(str(details), "lxml").find("details")
        if details_clone:
            s = details_clone.find("summary")
            if s:
                s.decompose()
            corpo = details_clone.get_text(separator=" ", strip=True)
        else:
            corpo = ""

        if titulo_sanfona or corpo:
            parte = (titulo_sanfona + ": " + corpo).strip(": ")
            partes.append(parte)

    return " ".join(partes)


def deduplicar_paragrafos(texto: str, min_len: int = 80) -> str:
    """
    Remove parágrafos duplicados do texto (problema comum em páginas Elementor
    onde placeholders são repetidos em cada seção/sanfona).
    Mantém apenas a primeira ocorrência de cada bloco com mais de min_len chars.
    """
    # Dividir em sentenças longas (separadas por ponto + espaço ou fim de texto)
    # Usamos uma abordagem simples: dividir por blocos de pelo menos min_len chars
    vistos = set()
    resultado = []

    # Tokenizar em "frases" curtas para detectar duplicatas
    # Divide por '. ' mantendo o ponto, depois junta em parágrafos
    sentencas = re.split(r'(?<=\.) ', texto)
    i = 0
    while i < len(sentencas):
        # Juntar até ter um bloco grande o suficiente para comparar
        bloco = sentencas[i].strip()
        if len(bloco) >= min_len:
            # Normalizar para comparação (lowercase, sem espaços extras)
            chave = re.sub(r'\s+', ' ', bloco.lower())[:200]  # primeiros 200 chars como chave
            if chave not in vistos:
                vistos.add(chave)
                resultado.append(bloco)
        else:
            resultado.append(bloco)
        i += 1

    return ' '.join(resultado)


def extrair_conteudo(soup: BeautifulSoup) -> str:
    """
    Extrai o texto limpo do conteúdo principal da página.
    Remove header, footer, nav, scripts, estilos e widgets de suporte.
    Captura explicitamente o conteúdo de accordions <details>/<summary>.
    """
    # Clonar para não modificar o soup original
    page = BeautifulSoup(str(soup), "lxml")

    # 1. Remover tags indesejadas por nome
    for tag_name in TAGS_TO_REMOVE:
        for tag in page.find_all(tag_name):
            tag.decompose()

    # 2. Remover elementos por classe CSS
    for cls in CLASSES_TO_REMOVE:
        for el in page.find_all(class_=re.compile(re.escape(cls))):
            el.decompose()

    # 3. Tentar extrair área de conteúdo principal
    #    Prioridade: <main> → #content → .elementor → body
    conteudo = (
        page.find("main")
        or page.find(id="content")
        or page.find(id="primary")
        or page.find(class_=re.compile(r"elementor-page"))
        or page.find(class_=re.compile(r"elementor"))
        or page.body
    )

    if not conteudo:
        return ""

    # 4. Extrair texto dos accordions explicitamente (preserva título + corpo de cada painel)
    texto_accordions = extrair_conteudo_accordions(conteudo)

    # 5. Obter o texto bruto do container principal
    texto = conteudo.get_text(separator=" ", strip=True)

    # 6. Se há accordions, concatenar seu texto extraído explicitamente ao final
    #    (o get_text já pega o conteúdo dos <details>, mas o título do accordion
    #     às vezes fica colado ao corpo sem separação; a extração explícita garante
    #     que cada sanfona fique legível no índice)
    if texto_accordions:
        texto = texto + " " + texto_accordions

    # 7. Limpeza adicional
    texto = limpar_shortcodes(texto)
    texto = re.sub(r"\s{2,}", " ", texto)   # colapsar espaços múltiplos
    texto = re.sub(r"\n{2,}", "\n", texto)  # colapsar quebras de linha
    texto = texto.strip()

    # 8. Remover parágrafos duplicados (problema de placeholders repetidos)
    texto = deduplicar_paragrafos(texto)
    texto = re.sub(r"\s{2,}", " ", texto).strip()

    # 9. Truncar para manter JSON compacto (preserva início do conteúdo)
    if len(texto) > MAX_CONTEUDO_CHARS:
        texto = texto[:MAX_CONTEUDO_CHARS].rsplit(" ", 1)[0] + "…"

    return texto


def extrair_titulo(soup: BeautifulSoup, url: str) -> str:
    """Extrai o título da página."""
    # Tentar <title>
    if soup.title and soup.title.string:
        titulo = soup.title.string.strip()
        # WordPress costuma usar "Página – Site"; pegar a parte antes do separador
        for sep in [" \u2013 ", " - ", " | ", " \u00b7 "]:
            if sep in titulo:
                partes = titulo.split(sep)
                # Retorna a primeira parte (nome da página), que costuma ser mais específica
                return partes[0].strip()
        return titulo

    # Fallback: primeiro <h1>
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)

    # Último recurso: path da URL
    return urlparse(url).path.strip("/").replace("-", " ").replace("/", " > ").title()


def extrair_url_canonica(soup: BeautifulSoup, url_fallback: str) -> str:
    """Tenta obter a URL canônica via <link rel='canonical'>."""
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        return canonical["href"].strip()
    og_url = soup.find("meta", property="og:url")
    if og_url and og_url.get("content"):
        return og_url["content"].strip()
    return url_fallback


def extrair_links(soup: BeautifulSoup, pagina_url: str, base_domain: str) -> list:
    """Coleta todos os links internos da página."""
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        href_abs = urljoin(pagina_url, href)
        href_norm = normalizar_url(href_abs)
        parsed = urlparse(href_norm)

        # Filtros
        if parsed.scheme not in ("http", "https"):
            continue
        if not mesmo_dominio(href_norm, base_domain):
            continue
        if extensao_ignorada(href_norm):
            continue

        links.append(href_norm)

    return links


# ─────────────────────────────────────────────
# Crawler principal
# ─────────────────────────────────────────────

def crawl(base_url: str, delay: float, output: str) -> None:
    base_url_norm = normalizar_url(base_url)
    base_domain   = urlparse(base_url_norm).netloc

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    robots = carregar_robots(base_url_norm, session)

    visitadas       = set()
    urls_canonicas  = set()   # para deduplicar por URL canônica
    fila            = deque([base_url_norm])
    indice          = []

    total_encontradas = 0
    total_sucesso     = 0
    total_erro        = 0
    erros             = []

    log.info("Iniciando crawl em: %s", base_url_norm)
    log.info("Domínio alvo: %s", base_domain)

    while fila:
        url = fila.popleft()

        if url in visitadas:
            continue

        visitadas.add(url)
        total_encontradas += 1

        # Verificar robots.txt
        if not robots.can_fetch(USER_AGENT, url):
            log.warning("Bloqueado por robots.txt: %s", url)
            continue

        # Requisição HTTP
        try:
            log.info("[%4d] Processando: %s", total_encontradas, url)
            resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)

            # Seguir redirecionamentos mas verificar domínio final
            url_final = normalizar_url(resp.url)
            if not mesmo_dominio(url_final, base_domain):
                log.warning("Redirecionou para fora do domínio: %s → %s", url, url_final)
                continue

            if resp.status_code == 404:
                msg = "404 Not Found: {}".format(url)
                log.warning(msg)
                erros.append(msg)
                total_erro += 1
                continue

            if resp.status_code >= 400:
                msg = "HTTP {}: {}".format(resp.status_code, url)
                log.warning(msg)
                erros.append(msg)
                total_erro += 1
                continue

            # Processar apenas HTML
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                log.debug("Tipo de conteúdo ignorado (%s): %s", content_type, url)
                continue

            soup = BeautifulSoup(resp.text, "lxml")

            # Extrair dados
            titulo   = extrair_titulo(soup, url_final)
            url_can  = extrair_url_canonica(soup, url_final)
            conteudo = extrair_conteudo(soup)

            # Adicionar ao índice somente se houver conteúdo útil E URL canônica nova
            if conteudo and len(conteudo) > 30:
                if url_can not in urls_canonicas:
                    urls_canonicas.add(url_can)
                    indice.append({
                        "titulo":   titulo,
                        "url":      url_can,
                        "conteudo": conteudo,
                    })
                    total_sucesso += 1
                else:
                    log.debug("URL canônica duplicada, ignorada: %s (de %s)", url_can, url)
            else:
                log.debug("Conteúdo vazio/curto ignorado: %s", url)

            # Descobrir novos links
            novos_links = extrair_links(soup, url_final, base_domain)
            for link in novos_links:
                if link not in visitadas:
                    fila.append(link)

        except requests.exceptions.Timeout:
            msg = "Timeout: {}".format(url)
            log.error(msg)
            erros.append(msg)
            total_erro += 1
        except requests.exceptions.ConnectionError as exc:
            msg = "Erro de conexão em {}: {}".format(url, exc)
            log.error(msg)
            erros.append(msg)
            total_erro += 1
        except Exception as exc:
            msg = "Erro inesperado em {}: {}".format(url, exc)
            log.error(msg, exc_info=True)
            erros.append(msg)
            total_erro += 1

        # Delay para não sobrecarregar o servidor
        time.sleep(delay)

    # ─────────────────────────────────────────────
    # Relatório final
    # ─────────────────────────────────────────────
    log.info("=" * 60)
    log.info("CRAWL CONCLUÍDO")
    log.info("  Total de URLs encontradas: %d", total_encontradas)
    log.info("  Processadas com sucesso:   %d", total_sucesso)
    log.info("  Com erro:                  %d", total_erro)
    log.info("=" * 60)

    if erros:
        log.warning("Páginas com erro:")
        for e in erros:
            log.warning("  • %s", e)

    # Salvar JSON
    with open(output, "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=2)

    log.info("Índice salvo em: %s (%d páginas)", output, len(indice))


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scraper do Portal de Parcerias — gera indice-busca.json"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="URL raiz para iniciar o crawl (padrão: {})".format(DEFAULT_URL),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Delay em segundos entre requisições (padrão: {})".format(DEFAULT_DELAY),
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Caminho do arquivo JSON de saída (padrão: {})".format(DEFAULT_OUTPUT),
    )
    args = parser.parse_args()

    crawl(base_url=args.url, delay=args.delay, output=args.output)


if __name__ == "__main__":
    main()
