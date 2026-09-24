import sys
import re
sys.stdout.reconfigure(encoding='utf-8')
import requests
from bs4 import BeautifulSoup

TAGS_TO_REMOVE = [
    "header", "footer", "nav", "script", "style", "noscript",
    "svg", "iframe", "img", "figure", "picture",
]

CLASSES_TO_REMOVE = [
    "wpr-menu-item", "wpr-nav-menu", "wpr-mobile-nav-items",
    "hfe-search-button-wrapper", "hfe-nav-menu",
    "elementor-social-icon", "elementor-social-icons-wrapper",
    "elementor-widget-search-form", "elementor-widget-breadcrumb",
    "elementor-widget-sidebar",
    "elementor-button-back-to-top",
    "elementor-element-149eaefd",
    "elementor-location-footer",
    "hfe-footer",
    "site-footer",
    "elementor-location-header",
    "hfe-header",
    "site-header",
]

def extrair(url):
    print(f"\n--- Diagnosticando: {url} ---")
    resp = requests.get(url, verify=False)
    soup = BeautifulSoup(resp.text, "lxml")
    
    page = BeautifulSoup(str(soup), "lxml")
    
    for tag_name in TAGS_TO_REMOVE:
        for tag in page.find_all(tag_name):
            tag.decompose()
            
    for cls in CLASSES_TO_REMOVE:
        for el in page.find_all(class_=re.compile(re.escape(cls))):
            el.decompose()
            
    conteudo = (
        page.find("main")
        or page.find(id="content")
        or page.find(id="primary")
        or page.find(class_=re.compile(r"elementor-page"))
        or page.find(class_=re.compile(r"elementor"))
        or page.body
    )
    
    if not conteudo:
        print("Conteúdo não encontrado!")
        return

    subtitulos = []
    
    # h2, h3, h4
    for tag in conteudo.find_all(["h2", "h3", "h4"]):
        texto = tag.get_text(separator=" ", strip=True)
        if texto:
            subtitulos.append(f"[{tag.name}] {texto}")
            
    # summaries
    for details in conteudo.find_all("details", class_=re.compile(r"e-n-accordion-item")):
        summary = details.find("summary")
        if summary:
            texto = summary.get_text(separator=" ", strip=True)
            if texto:
                subtitulos.append(f"[summary] {texto}")
                
    print("SUBTÍTULOS ENCONTRADOS (h2, h3, h4, summary):")
    for s in subtitulos:
        print("  - " + s)
        
    # Elementor heading titles that are not h1, h2, h3, h4, h5, h6
    elementor_headings = conteudo.find_all(class_=re.compile(r"elementor-heading-title"))
    other_headings = []
    for h in elementor_headings:
        if h.name not in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            texto = h.get_text(separator=" ", strip=True)
            if texto:
                other_headings.append(f"[{h.name}] {texto}")
                
    print("\nOUTROS WIDGETS DE TÍTULO ELEMENTOR (sem h1-h6):")
    if other_headings:
        for o in other_headings:
            print("  - " + o)
    else:
        print("  Nenhum encontrado.")
        
urls = [
    "https://hom-portaldeparcerias.prefeitura.sp.gov.br/perguntas-frequentes/",
    "https://hom-portaldeparcerias.prefeitura.sp.gov.br/parcerias-mrosc/",
    "https://hom-portaldeparcerias.prefeitura.sp.gov.br/editais/"
]

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

for u in urls:
    extrair(u)
