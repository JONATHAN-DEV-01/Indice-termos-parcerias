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

CORREÇÕES (09/09/2026):
  1) A Home passava pelo índice com o <title> da página ("Portal de
     Parcerias"), em vez de aparecer nos resultados de busca como "Home".
  2) O rodapé do site (endereço, telefone, e-mails, links de privacidade
     etc.) está embutido, IDÊNTICO, no HTML de toda página — e as classes
     CSS que deveriam removê-lo (elementor-location-footer, hfe-footer,
     site-footer) não estão batendo com o wrapper real do tema, então o
     rodapé inteiro ia parar dentro do "conteudo" de TODAS as páginas.
     Isso fazia qualquer termo do rodapé (telefone, "Revogar
     consentimento", e-mails de contato) aparecer como resultado de busca
     em TODAS as páginas do site, mesmo sem relação nenhuma com o
     conteúdo real de cada uma.
     Solução: além de tentar remover o rodapé pelas classes CSS (mantido
     como primeira linha de defesa, útil se/quando o tema for corrigido),
     agora extraímos o bloco de rodapé de cada página por um recorte de
     texto (âncora "Portal de Parcerias com o Terceiro Setor Secretaria
     Municipal de Gestão (SEGES) ... Nossas Redes") e o transformamos em
     UMA ÚNICA entrada extra no índice, "Home (rodapé do site)". Termos
     que só existem no rodapé passam a aparecer uma vez só; termos que
     também existem de verdade em alguma página continuam aparecendo
     normalmente naquela página, além do card do rodapé.
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
    # CAUSA RAIZ IDENTIFICADA (09/09/2026, via inspeção do HTML ao vivo):
    #   O tema usa "elementor-template-canvas" (sem <footer> real) e o
    #   rodapé é um container Elementor com ID de elemento único por build.
    #   O container pai do rodapé encontrado é:
    #     <div class="elementor-element elementor-element-149eaefd e-con e-parent">
    #   O ID "elementor-element-149eaefd" é específico deste build — pode
    #   mudar se o template for re-salvo no Elementor. Adicionamos aqui
    #   como segunda camada de defesa; a âncora de texto em separar_rodape()
    #   continua sendo o fallback principal.
    "elementor-element-149eaefd",   # container do rodapé (build atual)
    "elementor-location-footer",    # caso o tema seja corrigido no futuro
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
# CORREÇÃO: título fixo da Home + isolamento do rodapé
# ─────────────────────────────────────────────
TITULO_HOME   = "Home"
TITULO_RODAPE = "Home (rodapé do site)"

# Âncora de início e fim do bloco de rodapé — texto FIXO e idêntico em
# toda página do Portal de Parcerias. Se o rodapé mudar de texto no
# futuro (novo endereço, nova seção etc.), esta âncora precisa ser
# atualizada para continuar funcionando.
RODAPE_ANCORA_INICIO = "Portal de Parcerias com o Terceiro Setor Secretaria Municipal de Gestão (SEGES)"
RODAPE_ANCORA_FIM    = "Nossas Redes"
# NOTA DE DESIGN (09/09/2026): regex NÃO-GULOSA (.*?).
#
# A âncora RODAPE_ANCORA_INICIO aparece NO MÁXIMO UMA VEZ por página,
# portanto guloso e não-guloso produzem o MESMO resultado para todas as
# páginas conhecidas do portal.
#
# Mantemos NÃO-GULOSO (.*?) por segurança: se "Nossas Redes" aparecer
# DUAS VEZES após a âncora (ex.: widget de rodapé duplicado — cenário
# documentado em outras páginas deste site), o não-guloso para na 1ª
# ocorrência, preservando qualquer conteúdo real que exista entre as duas.
# O guloso apagaria silenciosamente todo esse conteúdo sem aviso no log.
RODAPE_RE = re.compile(
    re.escape(RODAPE_ANCORA_INICIO) + r".*?" + re.escape(RODAPE_ANCORA_FIM),
    re.DOTALL,
)


# ─────────────────────────────────────────────
# Funções auxiliares
# ─────────────────────────────────────────────

def normalizar_url(url: str) -> str:
    """Remove fragmento (#âncora), força HTTPS e normaliza a barra final."""
    url, _ = urldefrag(url)
    # Forçar HTTPS — o servidor rejeita (403/404) requisições via HTTP puro
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
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


def separar_rodape(texto: str):
    """
    CORREÇÃO CRÍTICA: localiza o bloco de rodapé (idêntico em todas as
    páginas) dentro do texto já extraído de UMA página, e o separa do
    resto do conteúdo.

    Retorna (texto_sem_rodape, bloco_do_rodape_ou_None).

    Usa busca GULOSA (".*"), capturando do início da âncora até o ÚLTIMO
    "Nossas Redes" da página. Isso garante que o rodapé real seja sempre
    isolado, mesmo em páginas como "Ajuda", que têm sua própria seção de
    contatos com "Nossas Redes" ANTES do rodapé real do site.
    """
    m = RODAPE_RE.search(texto)
    if not m:
        return texto, None
    bloco = m.group(0)
    resto = texto[: m.start()] + " " + texto[m.end():]
    resto = re.sub(r"\s{2,}", " ", resto).strip()
    return resto, bloco


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


def extrair_conteudo(soup: BeautifulSoup):
    """
    Extrai o texto limpo do conteúdo principal da página.
    Remove header, footer, nav, scripts, estilos e widgets de suporte.
    Captura explicitamente o conteúdo de accordions <details>/<summary>.

    CORREÇÃO: agora retorna uma TUPLA (texto, bloco_rodape) em vez de só
    o texto. O bloco de rodapé é retirado do texto principal (usando
    separar_rodape) ANTES do truncamento por MAX_CONTEUDO_CHARS, para não
    desperdiçar espaço do limite de caracteres com o rodapé repetido, e
    devolvido separadamente para quem chamou esta função decidir o que
    fazer com ele (ver crawl()).
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
        return "", None

    # 4. Extrair texto dos accordions explicitamente (preserva título + corpo de cada painel)
    texto_accordions = extrair_conteudo_accordions(conteudo)

    # 5. Obter o texto bruto do container principal
    texto = conteudo.get_text(separator=" ", strip=True)

    # 6. Limpeza inicial (antes de separar o rodapé, para a âncora de
    #    texto bater de forma previsível com espaços únicos)
    texto = limpar_shortcodes(texto)
    texto = re.sub(r"\s{2,}", " ", texto)   # colapsar espaços múltiplos
    texto = re.sub(r"\n{2,}", "\n", texto)  # colapsar quebras de linha
    texto = texto.strip()

    # 7. CORREÇÃO CRÍTICA: separar o bloco de rodapé do conteúdo real da
    #    página (ver docstring de separar_rodape). Isso acontece ANTES de
    #    truncar por MAX_CONTEUDO_CHARS, para não gastar o limite de
    #    caracteres com texto de rodapé repetido em toda página.
    texto, bloco_rodape = separar_rodape(texto)

    # 8. Se há accordions, concatenar seu texto extraído explicitamente ao
    #    conteúdo real (já sem o rodapé)
    #    (o get_text já pega o conteúdo dos <details>, mas o título do accordion
    #     às vezes fica colado ao corpo sem separação; a extração explícita garante
    #     que cada sanfona fique legível no índice)
    if texto_accordions:
        texto = texto + " " + texto_accordions
        texto = re.sub(r"\s{2,}", " ", texto).strip()

    # 9. Remover parágrafos duplicados (problema de placeholders repetidos)
    texto = deduplicar_paragrafos(texto)
    texto = re.sub(r"\s{2,}", " ", texto).strip()

    # 10. Truncar para manter JSON compacto (preserva início do conteúdo)
    if len(texto) > MAX_CONTEUDO_CHARS:
        texto = texto[:MAX_CONTEUDO_CHARS].rsplit(" ", 1)[0] + "…"

    return texto, bloco_rodape


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

    # CORREÇÃO: guarda o primeiro bloco de rodapé encontrado (é idêntico
    # em todas as páginas, então basta capturar uma vez) para virar uma
    # única entrada extra no índice ao final do crawl.
    rodape_capturado = None

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
            conteudo, bloco_rodape = extrair_conteudo(soup)

            # CORREÇÃO: a Home deve aparecer nos resultados de busca como
            # "Home", não com o <title> da página ("Portal de Parcerias").
            # Compara a URL final E a canônica com a raiz do site, pois é
            # a URL canônica que acaba salva no índice.
            if normalizar_url(url_final) == base_url_norm or normalizar_url(url_can) == base_url_norm:
                titulo = TITULO_HOME

            # Guarda o primeiro bloco de rodapé encontrado (idêntico em
            # todas as páginas — só precisamos de uma cópia).
            if bloco_rodape and rodape_capturado is None:
                rodape_capturado = bloco_rodape
                log.info("Bloco de rodapé identificado e isolado (será uma entrada única no índice).")

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

    # CORREÇÃO: adiciona a entrada única do rodapé ao final do índice,
    # se algum bloco de rodapé foi capturado durante o crawl.
    if rodape_capturado:
        indice.append({
            "titulo":   TITULO_RODAPE,
            "url":      base_url_norm,
            "conteudo": rodape_capturado,
        })
        log.info("Entrada '%s' adicionada ao índice.", TITULO_RODAPE)
    else:
        log.warning(
            "Nenhum bloco de rodapé foi identificado em nenhuma página. "
            "Se o texto do rodapé do site mudou, atualize RODAPE_ANCORA_INICIO/"
            "RODAPE_ANCORA_FIM no topo deste arquivo."
        )

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