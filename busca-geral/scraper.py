#!/usr/bin/env python3
"""
scraper.py — Crawler para o Portal de Parcerias
Gera o índice da busca geral (título, URL, subtítulos e conteúdo de cada página)
e, se ele passar nas validações, substitui o arquivo publicado no Supabase Storage.

Uso:
    python busca-geral/scraper.py                # gera, valida e publica
    python busca-geral/scraper.py --sem-upload   # só gera e valida; não altera o Supabase
    python busca-geral/scraper.py --publicar-arquivo busca-geral/backups/indice-busca.X.json
    python busca-geral/scraper.py --url https://portaldeparcerias.prefeitura.sp.gov.br/ --forcar

--forcar publica mesmo com validações reprovadas. É necessário, por exemplo, na
troca de homologação para produção, porque o domínio das URLs muda de propósito.

Dependências:
    pip install requests beautifulsoup4 lxml playwright
    playwright install chromium

Credencial (só para publicar): SUPABASE_SECRET_KEY no .env da raiz do projeto (ver .env.example).

ATUALIZAÇÃO (06/11/2026):
  1) As páginas agora são renderizadas em um navegador. O HTML cru não traz os
     cards montados por JavaScript (Editais, Capacitação, carrosséis da Home).
  2) Ficam de fora páginas protegidas por senha, páginas com texto provisório
     (Lorem Ipsum) e as listadas em URLS_EXCLUIDAS.
  3) O texto injetado pelo JavaScript que não é conteúdo (widget de Libras,
     filtros, clones de carrossel) é removido antes da extração.
  4) O índice só é publicado depois de validado; o arquivo anterior é salvo em
     backups/ antes da troca.

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
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# Configuração de logging
# ─────────────────────────────────────────────
# O console do Windows usa cp1252 e quebraria com caracteres fora dessa tabela.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
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
DEFAULT_URL    = "https://portaldeparcerias.prefeitura.sp.gov.br/"
DEFAULT_OUTPUT = str(Path(__file__).resolve().parent / "indice-busca.novo.json")
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
    # Controles de tela: filtros, "Limpar filtros", "Mostrar mais" etc.
    "button", "select",
]
CLASSES_TO_REMOVE = [
    # Widget de Libras (Hand Talk), injetado em todas as páginas
    "ht-addons-exception",
    # Texto só para leitor de tela: repete o nome do item + "(abre em nova aba)"
    "capacitacoes-sr-only", "manuais-sr-only",
    # Barras de filtro, contadores e mensagens de lista vazia
    "editais-toolbar", "editais-contador", "editais-vazio", "card-edital-btn",
    "capacitacoes-toolbar", "capacitacoes-sem-resultado",
    "manuais-toolbar", "manuais-sem-resultado",
    # Controles e mensagens de erro do visualizador de PDF do MROSC de A a Z
    "mz-viewer",
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
    # O tema não tem <footer> real: quem isola o rodapé é a âncora de texto em
    # separar_rodape(). Não use IDs "elementor-element-*" aqui: eles mudam quando
    # o template é salvo de novo. O antigo 149eaefd passou a ser a seção de
    # notícias da Home e estava apagando esse conteúdo.
    "elementor-location-footer",    # caso o tema seja corrigido no futuro
    "hfe-footer",
    "site-footer",
    # Cabeçalho
    "elementor-location-header",
    "hfe-header",
    "site-header",
]

# Tamanho máximo do conteúdo por página (evita JSON muito grande).
# Editais já passa de 70 mil caracteres com os 189 cards; o limite anterior
# (60 mil) cortava os últimos editais sem nenhum aviso além do log.
MAX_CONTEUDO_CHARS = 150000

# ─────────────────────────────────────────────
# Renderização em navegador
# ─────────────────────────────────────────────
RENDER_TIMEOUT_MS   = 60000
RENDER_IDLE_MS      = 15000   # espera pela rede ociosa; não é erro se estourar
RENDER_SELETOR_MS   = 20000
RENDER_ASSENTAR_MS  = 1000    # folga para os scripts terminarem de montar a tela

# Elementos que precisam existir depois da renderização. Se não aparecerem, a
# página é tratada como erro: indexá-la assim apagaria os cards da busca.
SELETORES_OBRIGATORIOS = {
    "/":           [".editais-carrossel-item", ".nc-card"],
    "/editais/":   ["#editais-lista .card-edital"],
    "/capacitacao/": ["#capacitacoes-lista .card-capacitacao"],
}

# Carrosséis em loop duplicam os slides no DOM. Só a primeira cópia de cada
# texto é mantida.
SELETORES_CARROSSEL = [".pp-slide", ".editais-carrossel-item", ".nc-card"]

# Recursos que não trazem texto: bloqueá-los acelera o crawl e evita registrar
# visitas do scraper nos serviços de estatística.
TIPOS_BLOQUEADOS = {"image", "media", "font"}
HOSTS_BLOQUEADOS = (
    "handtalk.me", "cloudflareinsights.com", "challenges.cloudflare.com",
    "powerbi.com", "analysis.windows.net", "youtube.com", "ytimg.com",
    "googlevideo.com",
)

MIN_OCORRENCIAS_LOREM = 3

# Páginas alcançáveis por link que não devem aparecer na busca.
URLS_EXCLUIDAS = {
    "/home-teste-26-10/",   # versão de teste da Home; /home/ redireciona para ela
}

# ─────────────────────────────────────────────
# Publicação no Supabase Storage
# ─────────────────────────────────────────────
SUPABASE_URL_PADRAO = "https://syjyktusqgfsrsbuxgrj.supabase.co"
SUPABASE_BUCKET     = "site-busca"
SUPABASE_OBJETO     = "indice-busca.json"   # o JS 9818 lê este nome fixo
SUPABASE_CACHE      = "max-age=300"
SUPABASE_LIMITE_BYTES = 5 * 1024 * 1024     # limite configurado no bucket
PASTA_BACKUPS       = Path(__file__).resolve().parent / "backups"
ARQUIVO_ENV         = Path(__file__).resolve().parent.parent / ".env"

# ─────────────────────────────────────────────
# Validação antes de publicar
# ─────────────────────────────────────────────
MIN_PAGINAS = 15
# Tamanho mínimo por caminho: pega cards que deixaram de ser renderizados.
MIN_CHARS_POR_CAMINHO = {"/editais/": 5000, "/capacitacao/": 2000}
# Em relação ao índice publicado: páginas com pelo menos este tamanho...
COMPARAR_A_PARTIR_DE_CHARS = 1000
# ...não podem sumir nem encolher abaixo desta fração.
FRACAO_MINIMA_DO_PUBLICADO = 0.5

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


def extrair_subtitulos(container) -> list:
    """Extrai subtítulos (h2, h3, h4) e o <summary> de accordions, mantendo a ordem e deduplicando."""
    subtitulos = []
    vistos = set()
    for tag in container.find_all(["h2", "h3", "h4", "summary"]):
        if tag.name == "summary":
            # Verificar se está dentro do widget de accordion do Elementor
            if not tag.find_parent("details", class_=re.compile(r"e-n-accordion-item")):
                continue
        
        texto = tag.get_text(separator=" ", strip=True)
        # Limpar texto
        texto = re.sub(r"\s{2,}", " ", texto).strip()
        
        # Ignorar vazio ou só símbolos (precisa de pelo menos 1 caractere alfanumérico)
        if not re.search(r"\w", texto):
            continue
            
        chave = texto.lower()
        if chave not in vistos:
            vistos.add(chave)
            subtitulos.append(texto)
            
    return subtitulos


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


def remover_clones_de_carrossel(page: BeautifulSoup) -> None:
    """Mantém só a primeira ocorrência de cada slide, comparando pelo texto."""
    for seletor in SELETORES_CARROSSEL:
        vistos = set()
        for slide in page.select(seletor):
            chave = " ".join(slide.get_text(separator=" ", strip=True).split()).lower()
            if chave in vistos:
                slide.decompose()
            else:
                vistos.add(chave)


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

    remover_clones_de_carrossel(page)

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
        return "", None, [], 0

    subtitulos = extrair_subtitulos(conteudo)

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

    tamanho_original = len(texto)

    # 10. Truncar para manter JSON compacto (preserva início do conteúdo)
    if tamanho_original > MAX_CONTEUDO_CHARS:
        texto = texto[:MAX_CONTEUDO_CHARS].rsplit(" ", 1)[0] + "…"

    return texto, bloco_rodape, subtitulos, tamanho_original


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


def pagina_protegida_por_senha(soup: BeautifulSoup) -> bool:
    """O WordPress troca o conteúdo da página protegida por este formulário."""
    return soup.find("form", class_="post-password-form") is not None


def tem_texto_provisorio(conteudo: str) -> bool:
    """Página de rascunho repete o Lorem Ipsum; uma ocorrência solta é só um resto em página real."""
    return conteudo.lower().count("lorem ipsum") >= MIN_OCORRENCIAS_LOREM


# ─────────────────────────────────────────────
# Renderização
# ─────────────────────────────────────────────

class Renderizador:
    """Abre as páginas em um Chromium sem janela e devolve o HTML já montado."""

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SystemExit(
                "Playwright não instalado. Execute:\n"
                "    pip install playwright\n"
                "    playwright install chromium"
            ) from exc
        self._playwright = sync_playwright().start()
        self._navegador = self._playwright.chromium.launch(headless=True)
        self._contexto = self._navegador.new_context(
            user_agent=USER_AGENT, locale="pt-BR",
            viewport={"width": 1366, "height": 900},
        )
        self._contexto.route("**/*", self._filtrar_requisicao)
        return self

    def __exit__(self, *_):
        self._navegador.close()
        self._playwright.stop()

    @staticmethod
    def _filtrar_requisicao(route):
        req = route.request
        parsed = urlparse(req.url)
        host = parsed.hostname or ""
        bloquear = (
            req.resource_type in TIPOS_BLOQUEADOS
            or parsed.path.lower().endswith(".pdf")
            or any(host == h or host.endswith("." + h) for h in HOSTS_BLOQUEADOS)
        )
        route.abort() if bloquear else route.continue_()

    def renderizar(self, url: str) -> str:
        """Retorna o HTML renderizado. Falha se um elemento obrigatório não surgir."""
        pagina = self._contexto.new_page()
        try:
            pagina.goto(url, wait_until="load", timeout=RENDER_TIMEOUT_MS)
            try:
                pagina.wait_for_load_state("networkidle", timeout=RENDER_IDLE_MS)
            except Exception:
                log.debug("Rede não ficou ociosa a tempo: %s", url)
            for seletor in SELETORES_OBRIGATORIOS.get(urlparse(url).path, []):
                # "attached": cards além do limite "Mostrar mais" ficam ocultos, mas contam.
                pagina.wait_for_selector(seletor, state="attached", timeout=RENDER_SELETOR_MS)
            pagina.wait_for_timeout(RENDER_ASSENTAR_MS)
            return pagina.content()
        finally:
            pagina.close()


# ─────────────────────────────────────────────
# Crawler principal
# ─────────────────────────────────────────────

def crawl(base_url: str, delay: float, output: str) -> list:
    """Percorre o site, grava o índice em `output` e retorna a lista de erros."""
    with Renderizador() as renderizador:
        return _crawl(base_url, delay, output, renderizador)


def _crawl(base_url: str, delay: float, output: str, renderizador: Renderizador) -> list:
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
    links_quebrados   = []
    origem_do_link    = {}

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

            # Link quebrado é problema do site, não do crawl: não bloqueia a
            # publicação. Se a página tinha conteúdo, a validação acusa a perda.
            if resp.status_code == 404:
                log.warning("404 Not Found: %s", url)
                links_quebrados.append(url)
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

            # Um redirecionamento pode cair em página já processada ou excluída.
            if url_final != url and url_final in visitadas:
                continue
            visitadas.add(url_final)

            if urlparse(url_final).path in URLS_EXCLUIDAS:
                log.info("  Ignorada (URLS_EXCLUIDAS): %s", url_final)
                continue

            soup = BeautifulSoup(renderizador.renderizar(url_final), "lxml")

            if pagina_protegida_por_senha(soup):
                log.warning("  Ignorada (protegida por senha): %s", url_final)
                continue

            # Extrair dados
            titulo   = extrair_titulo(soup, url_final)
            url_can  = extrair_url_canonica(soup, url_final)
            conteudo, bloco_rodape, subtitulos, tam_orig = extrair_conteudo(soup)

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
            if tem_texto_provisorio(conteudo):
                log.warning("  Fora do índice (texto provisório Lorem Ipsum): %s", url_final)
            elif conteudo and len(conteudo) > 30:
                if "lorem ipsum" in conteudo.lower():
                    log.warning("  Página real com resto de Lorem Ipsum, corrigir no site: %s", url_final)
                if url_can not in urls_canonicas:
                    urls_canonicas.add(url_can)
                    log.info("  Tamanho do conteúdo extraído: %d caracteres", tam_orig)
                    if tam_orig > MAX_CONTEUDO_CHARS:
                        log.warning("  ATENÇÃO: Conteúdo excedeu o limite de %d caracteres (tamanho: %d) e foi truncado!", MAX_CONTEUDO_CHARS, tam_orig)
                    indice.append({
                        "titulo":     titulo,
                        "url":        url_can,
                        "subtitulos": subtitulos,
                        "conteudo":   conteudo,
                    })
                    total_sucesso += 1
                else:
                    log.debug("URL canônica duplicada, ignorada: %s (de %s)", url_can, url)
            else:
                log.debug("Conteúdo vazio/curto ignorado: %s", url)

            # Descobrir novos links
            novos_links = extrair_links(soup, url_final, base_domain)
            for link in novos_links:
                origem_do_link.setdefault(link, url_final)
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
            "titulo":     TITULO_RODAPE,
            "url":        base_url_norm,
            "subtitulos": [],
            "conteudo":   rodape_capturado,
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

    if links_quebrados:
        log.warning("Links internos quebrados (corrigir no site):")
        for quebrado in links_quebrados:
            log.warning("  • %s (encontrado em %s)", quebrado, origem_do_link.get(quebrado, "?"))

    # Salvar JSON
    with open(output, "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=2)

    log.info("Índice salvo em: %s (%d páginas)", output, len(indice))

    # Exibir exemplos
    log.info("\n=== 3 EXEMPLOS COM SUBTÍTULOS ===")
    exemplos = [p for p in indice if p.get("subtitulos")]
    for ex in exemplos[:3]:
        log.info("TÍTULO: %s", ex["titulo"])
        log.info("URL: %s", ex["url"])
        log.info("SUBTÍTULOS:\n  - " + "\n  - ".join(ex["subtitulos"]))
        log.info("-" * 40)

    return erros


# ─────────────────────────────────────────────
# Validação do índice
# ─────────────────────────────────────────────

def caminho_da_url(url: str) -> str:
    return urlparse(url).path


def validar_indice(indice, publicado=None) -> list:
    """Retorna a lista de problemas que impedem a publicação (vazia = aprovado)."""
    if not isinstance(indice, list) or not indice:
        return ["O índice não é uma lista de páginas, ou está vazio."]

    problemas = []
    for i, item in enumerate(indice):
        if not (
            isinstance(item, dict)
            and isinstance(item.get("titulo"), str) and item["titulo"]
            and isinstance(item.get("url"), str) and item["url"]
            and isinstance(item.get("subtitulos"), list)
            and isinstance(item.get("conteudo"), str)
        ):
            problemas.append("Registro %d fora do formato titulo/url/subtitulos/conteudo." % i)
    if problemas:
        return problemas

    if len(indice) < MIN_PAGINAS:
        problemas.append("Só %d páginas (mínimo: %d)." % (len(indice), MIN_PAGINAS))

    # O JS 9818 reduz a pontuação desta entrada comparando pelo título exato.
    if not any(p["titulo"] == TITULO_RODAPE for p in indice):
        problemas.append("Falta a entrada '%s'." % TITULO_RODAPE)

    dominios = {urlparse(p["url"]).netloc for p in indice}
    if len(dominios) > 1:
        problemas.append("URLs com mais de um domínio: %s." % sorted(dominios))

    por_caminho = {caminho_da_url(p["url"]): p for p in indice if p["titulo"] != TITULO_RODAPE}
    por_titulo = {p["titulo"]: p for p in indice}
    for caminho, minimo in MIN_CHARS_POR_CAMINHO.items():
        tamanho = len(por_caminho[caminho]["conteudo"]) if caminho in por_caminho else 0
        if tamanho < minimo:
            problemas.append(
                "%s tem %d caracteres (mínimo: %d); os cards podem não ter sido montados."
                % (caminho, tamanho, minimo)
            )

    for p in indice:
        if len(p["conteudo"]) >= MAX_CONTEUDO_CHARS and p["conteudo"].endswith("…"):
            problemas.append("'%s' foi cortada no limite de %d caracteres." % (p["titulo"], MAX_CONTEUDO_CHARS))

    if publicado:
        dominios_publicados = {urlparse(p["url"]).netloc for p in publicado}
        if dominios != dominios_publicados:
            problemas.append(
                "O domínio das URLs mudaria de %s para %s. Homologação e produção leem "
                "o mesmo arquivo; use --forcar só se a troca for intencional."
                % (sorted(dominios_publicados), sorted(dominios))
            )
        else:
            for p in publicado:
                if p["titulo"] == TITULO_RODAPE or len(p["conteudo"]) < COMPARAR_A_PARTIR_DE_CHARS:
                    continue
                if caminho_da_url(p["url"]) in URLS_EXCLUIDAS:   # saída intencional
                    continue
                # Mesmo título em outra URL é a mesma página, só que movida.
                novo = por_caminho.get(caminho_da_url(p["url"])) or por_titulo.get(p["titulo"])
                if novo is None:
                    problemas.append("'%s' (%s) sairia do índice." % (p["titulo"], p["url"]))
                elif len(novo["conteudo"]) < len(p["conteudo"]) * FRACAO_MINIMA_DO_PUBLICADO:
                    problemas.append(
                        "'%s' encolheria de %d para %d caracteres."
                        % (p["titulo"], len(p["conteudo"]), len(novo["conteudo"]))
                    )
    return problemas


# ─────────────────────────────────────────────
# Publicação no Supabase
# ─────────────────────────────────────────────

def carregar_env(caminho: Path = ARQUIVO_ENV) -> None:
    """Lê KEY=VALOR do .env sem sobrescrever variáveis já definidas no ambiente."""
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave.strip(), valor.strip().strip("'\""))


def obter_chave_secreta() -> str:
    """Retorna a chave de escrita ou encerra explicando como configurá-la."""
    chave = os.environ.get("SUPABASE_SECRET_KEY", "")
    if not chave:
        raise SystemExit(
            "SUPABASE_SECRET_KEY não definida. Coloque a chave secreta do projeto no "
            ".env (ver .env.example) ou rode com --sem-upload para só gerar o índice."
        )
    if chave.startswith("sb_publishable_"):
        raise SystemExit(
            "SUPABASE_SECRET_KEY contém a chave publicável, que não pode gravar no "
            "bucket. Use a chave secreta (sb_secret_...)."
        )
    return chave


def url_publica_do_indice() -> str:
    base = os.environ.get("SUPABASE_URL", SUPABASE_URL_PADRAO).rstrip("/")
    return "%s/storage/v1/object/public/%s/%s" % (base, SUPABASE_BUCKET, SUPABASE_OBJETO)


def baixar_publicado():
    """Retorna o conteúdo atual do arquivo publicado, ou None se ele ainda não existir."""
    # O parâmetro evita receber uma cópia antiga guardada em cache.
    resp = requests.get(
        url_publica_do_indice(), params={"v": int(time.time())}, timeout=REQUEST_TIMEOUT * 2,
    )
    if resp.status_code in (400, 404):
        return None
    resp.raise_for_status()
    return resp.content


def publicar_no_supabase(dados: bytes, atual, chave: str) -> None:
    """Salva o backup, substitui o arquivo e confere o que ficou publicado."""
    if atual is not None:
        PASTA_BACKUPS.mkdir(exist_ok=True)
        backup = PASTA_BACKUPS / ("indice-busca.%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        backup.write_bytes(atual)
        log.info("Backup do arquivo anterior: %s", backup)

    base = os.environ.get("SUPABASE_URL", SUPABASE_URL_PADRAO).rstrip("/")
    resp = requests.post(
        "%s/storage/v1/object/%s/%s" % (base, SUPABASE_BUCKET, SUPABASE_OBJETO),
        data=dados,
        headers={
            "apikey": chave,
            "Authorization": "Bearer " + chave,
            "Content-Type": "application/json",
            "Cache-Control": SUPABASE_CACHE,
            "x-upsert": "true",
        },
        timeout=120,
    )
    if not resp.ok:
        raise RuntimeError("Supabase recusou o envio (HTTP %d): %s" % (resp.status_code, resp.text[:300]))

    conferido = baixar_publicado()
    if conferido is None or hashlib.sha256(conferido).digest() != hashlib.sha256(dados).digest():
        raise RuntimeError("O arquivo publicado não é idêntico ao enviado.")

    log.info("Publicado e conferido: %s (%d bytes)", url_publica_do_indice(), len(dados))
    log.info("A busca pode levar até 5 minutos para mostrar o novo conteúdo (cache).")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scraper do Portal de Parcerias — gera o índice da busca e publica no Supabase"
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
    parser.add_argument(
        "--sem-upload",
        action="store_true",
        help="Só gera e valida o índice; não altera o Supabase.",
    )
    parser.add_argument(
        "--publicar-arquivo",
        metavar="JSON",
        help="Não faz o crawl: valida e publica este arquivo (ex.: um backup).",
    )
    parser.add_argument(
        "--forcar",
        action="store_true",
        help="Publica mesmo com validações reprovadas.",
    )
    args = parser.parse_args()

    carregar_env()
    # Sem chave, falha já aqui, e não depois de vários minutos de crawl.
    chave = None if args.sem_upload else obter_chave_secreta()

    if args.publicar_arquivo:
        arquivo = args.publicar_arquivo
        erros = []
    else:
        arquivo = args.output
        erros = crawl(base_url=args.url, delay=args.delay, output=arquivo)

    dados = Path(arquivo).read_bytes()
    try:
        atual = baixar_publicado()
    except requests.RequestException as exc:
        # Sem o arquivo atual não há comparação nem backup.
        log.error("Não foi possível ler o índice publicado: %s", exc)
        return 1
    publicado = json.loads(atual) if atual is not None else None

    problemas = ["Erro no crawl: " + e for e in erros]
    problemas += validar_indice(json.loads(dados), publicado)
    if len(dados) > SUPABASE_LIMITE_BYTES:
        problemas.append("Arquivo com %d bytes, acima do limite do bucket." % len(dados))

    if problemas:
        log.warning("Validação reprovada (%d problema(s)):", len(problemas))
        for problema in problemas:
            log.warning("  • %s", problema)
    else:
        log.info("Validação aprovada: %s (%d bytes).", arquivo, len(dados))

    if args.sem_upload:
        log.info("--sem-upload: o Supabase não foi alterado.")
        return 1 if problemas else 0

    if problemas and not args.forcar:
        log.error("Nada foi publicado. Corrija os problemas ou use --forcar.")
        return 1

    try:
        publicar_no_supabase(dados, atual, chave)
    except (requests.RequestException, RuntimeError) as exc:
        log.error("Falha na publicação: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())