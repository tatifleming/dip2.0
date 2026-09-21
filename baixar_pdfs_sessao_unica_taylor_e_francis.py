#!/usr/bin/env python3
# -*- coding: utf-8 -*-

r"""
baixar_pdfs_sessao_unica_taylor_e_francis.py

Abre o navegador UMA ÚNICA VEZ. Você resolve o CAPTCHA manualmente.
Depois o programa usa a mesma sessão para baixar TODOS os artigos.

Funciona para Taylor & Francis e Wiley.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote, unquote, urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ============================================================
# Configurações
# ============================================================

# Para Wiley
PUBLISHERS_WILEY = {
    "John Wiley and Sons Inc",
    "John Wiley and Sons Inc.",
    "John Wiley and Sons Ltd",
    "John Wiley & Sons Inc",
    "John Wiley & Sons Inc.",
    "John Wiley & Sons Ltd",
    "Wiley",
    "Wiley-Blackwell",
}

# Para Taylor & Francis (opcional)
PUBLISHERS_TAYLOR = {
    "ROUTLEDGE JOURNALS, TAYLOR & FRANCIS LTD",
    "TAYLOR & FRANCIS INC",
    "TAYLOR & FRANCIS LTD",
}

EDITORA = "wiley"  # Pode ser "wiley" ou "taylor"

DOMINIOS = {
    "wiley": ["onlinelibrary.wiley.com", "wiley.com"],
    "taylor": ["tandfonline.com", "www.tandfonline.com"],
}

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)

LOG_COLUMNS = [
    "titulo",
    "DOI",
    "artigo_url",
    "pdf_url",
    "status",
    "arquivo_salvo",
    "motivo",
]


@dataclass
class ItemPDF:
    titulo: str
    doi: str
    artigo_url: str
    pdf_urls: list[str]
    nome_sugerido: str
    publisher: str = ""


# ============================================================
# Utilidades
# ============================================================

def txt(valor) -> str:
    if pd.isna(valor):
        return ""
    return str(valor).strip()


def normalizar_texto(valor) -> str:
    s = txt(valor)
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.replace(" ", "")


def publisher_valido(valor, editora) -> bool:
    pubs = PUBLISHERS_WILEY if editora == "wiley" else PUBLISHERS_TAYLOR
    n = normalizar_texto(valor)
    if not n:
        return False
    for p in pubs:
        if normalizar_texto(p) == n:
            return True
    return False


def limpar_doi(valor) -> str:
    s = unquote(txt(valor))
    if not s:
        return ""
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    s = s.strip().strip(" .;,)")
    m = DOI_RE.search(s)
    return m.group(0).lower() if m else ""


def slugify(texto: str, limite: int = 80) -> str:
    texto = unicodedata.normalize("NFKD", txt(texto))
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return (texto or "sem-info")[:limite]


def primeiro_autor(authors: str) -> str:
    autores = txt(authors)
    if not autores:
        return "autor-desconhecido"
    for sep in [";", "|", " and "]:
        if sep in autores:
            return autores.split(sep)[0].strip()
    return autores.split()[0].strip() if autores else "autor-desconhecido"


def nome_seguro_pdf(row: dict) -> str:
    titulo = txt(row.get("Title", ""))
    ano_raw = txt(row.get("Year", ""))
    autores = txt(row.get("Authors", ""))
    doi = limpar_doi(row.get("DOI", ""))

    ano_match = re.search(r"\d{4}", ano_raw)
    ano = ano_match.group(0) if ano_match else "sem-ano"

    autor = slugify(primeiro_autor(autores), 35)
    titulo_slug = slugify(titulo, 70)

    if doi:
        ident = slugify(doi.split("/", 1)[-1], 55)
    else:
        ident = hashlib.sha1(f"{titulo}|{autores}".encode()).hexdigest()[:10]

    nome = f"{ano}_{autor}_{titulo_slug}_{ident}.pdf"
    nome = re.sub(r'[<>:"/\\|?*]', "_", nome)
    return nome[:190]


def montar_urls(doi: str, editora: str) -> tuple[str, list[str]]:
    """Retorna (artigo_url, lista_pdf_urls)"""
    doi_path = quote(doi, safe="/")
    
    if editora == "wiley":
        artigo_url = f"https://onlinelibrary.wiley.com/doi/full/{doi_path}"
        pdf_urls = [
            f"https://onlinelibrary.wiley.com/doi/pdf/{doi_path}",
            f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi_path}",
        ]
    else:  # taylor
        artigo_url = f"https://www.tandfonline.com/doi/full/{doi_path}"
        pdf_urls = [
            f"https://www.tandfonline.com/doi/epdf/{doi_path}?needAccess=true",
            f"https://www.tandfonline.com/doi/pdf/{doi_path}?download=true",
            f"https://www.tandfonline.com/doi/pdf/{doi_path}",
        ]
    
    return artigo_url, pdf_urls


def parece_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except:
        return False


# ============================================================
# Download com sessão única
# ============================================================

def criar_driver_com_pasta_download(pasta_download: Path, editora: str):
    """Cria driver do Chrome com pasta de download configurada."""
    options = Options()
    
    # Pasta de download
    prefs = {
        "download.default_directory": str(pasta_download.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)
    
    # Argumentos
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1300,900")
    
    # Criar driver
    driver = webdriver.Chrome(options=options)
    return driver


def aguardar_resolucao_captcha(driver, timeout: int = 300) -> bool:
    """Aguarda o usuário resolver o CAPTCHA manualmente."""
    print("\n" + "="*60)
    print("⚠️  AGUARDANDO RESOLUÇÃO MANUAL DO CAPTCHA")
    print("="*60)
    print("O navegador está aberto. Por favor:")
    print("1. Se aparecer verificação do Cloudflare, resolva manualmente")
    print("2. Aguarde a página do artigo carregar completamente")
    print("3. Depois volte aqui e pressione ENTER")
    print("="*60)
    
    input("\n>>> Pressione ENTER após resolver o CAPTCHA e a página carregar: ")
    
    # Verificar se o CAPTCHA foi resolvido
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        print("✅ Continuando...")
        return True
    except:
        print("⚠️ Página pode não ter carregado completamente, continuando mesmo assim...")
        return True


def baixar_pdf_com_sessao(driver, item: ItemPDF, pasta_saida: Path, timeout: int = 60) -> tuple[str, str]:
    """Usa a sessão existente para baixar o PDF."""
    
    destino = pasta_saida / item.nome_sugerido
    
    # Verificar se já existe
    if destino.exists() and destino.stat().st_size > 0 and parece_pdf(destino):
        return "ja_existia", str(destino)
    
    # Tentar cada URL de PDF
    for pdf_url in item.pdf_urls:
        try:
            print(f"    > Tentando: {pdf_url}")
            
            # Limpar downloads antigos para detecção
            before = set(pasta_saida.glob("*.pdf"))
            
            # Ir para URL do PDF
            driver.get(pdf_url)
            time.sleep(2)
            
            # Aguardar download
            for _ in range(timeout):
                time.sleep(1)
                after = set(pasta_saida.glob("*.pdf"))
                novos = after - before
                if novos:
                    novo = list(novos)[0]
                    # Aguardar arquivo terminar de baixar
                    time.sleep(2)
                    if novo.stat().st_size > 0:
                        # Mover para nome correto
                        if novo != destino:
                            shutil.move(str(novo), str(destino))
                        if parece_pdf(destino):
                            return "baixado", str(destino)
            
        except Exception as e:
            continue
    
    return "falhou", ""


def baixar_todos_artigos(
    df: pd.DataFrame,
    pasta_saida: Path,
    editora: str,
    delay: float = 2.0,
) -> list[dict]:
    """Processo principal com sessão única."""
    
    pasta_saida.mkdir(parents=True, exist_ok=True)
    
    # Lista de itens para baixar
    itens = []
    for _, row in df.iterrows():
        doi = limpar_doi(row.get("DOI", ""))
        if not doi:
            continue
        
        artigo_url, pdf_urls = montar_urls(doi, editora)
        nome = nome_seguro_pdf(row)
        
        itens.append(ItemPDF(
            titulo=txt(row.get("Title", "")),
            doi=doi,
            artigo_url=artigo_url,
            pdf_urls=pdf_urls,
            nome_sugerido=nome,
            publisher=txt(row.get("Publisher", "")),
        ))
    
    if not itens:
        print("Nenhum item válido encontrado.")
        return []
    
    print(f"\n📋 Itens para processar: {len(itens)}")
    print(f"📁 Pasta de destino: {pasta_saida.resolve()}")
    
    # Criar driver e abrir sessão
    print("\n🚀 Abrindo navegador...")
    driver = criar_driver_com_pasta_download(pasta_saida, editora)
    
    logs = []
    
    try:
        # Abrir primeiro artigo e aguardar resolução do CAPTCHA
        primeiro_item = itens[0]
        print(f"\n🌐 Abrindo primeiro artigo para resolver CAPTCHA...")
        print(f"   URL: {primeiro_item.artigo_url}")
        
        driver.get(primeiro_item.artigo_url)
        
        # Aguardar usuário resolver CAPTCHA
        aguardar_resolucao_captcha(driver)
        
        # Agora processar todos os itens com a mesma sessão
        for i, item in enumerate(itens, start=1):
            print(f"\n[{i}/{len(itens)}] DOI: {item.doi}")
            print(f"    Título: {item.titulo[:70]}")
            
            # Se não for o primeiro, navegar diretamente
            if i > 1:
                driver.get(item.artigo_url)
                time.sleep(3)
            
            status, arquivo = baixar_pdf_com_sessao(driver, item, pasta_saida)
            
            logs.append({
                "titulo": item.titulo,
                "DOI": item.doi,
                "artigo_url": item.artigo_url,
                "pdf_url": item.pdf_urls[0] if item.pdf_urls else "",
                "status": status,
                "arquivo_salvo": arquivo,
                "motivo": "",
            })
            
            if status == "baixado":
                print(f"    ✅ Baixado: {Path(arquivo).name}")
            elif status == "ja_existia":
                print(f"    📁 Já existia: {Path(arquivo).name}")
            else:
                print(f"    ❌ Falhou - possível paywall")
            
            if delay > 0 and i < len(itens):
                time.sleep(delay)
        
    finally:
        print("\n🔒 Fechando navegador...")
        driver.quit()
    
    return logs


def ler_csv_filtrado(caminho_csv: Path, editora: str, limite: int = 0) -> pd.DataFrame:
    """Lê e filtra o CSV pelo publisher."""
    df = pd.read_csv(caminho_csv, dtype=str).fillna("")
    
    for col in ["Title", "DOI", "Publisher", "Year", "Authors"]:
        if col not in df.columns:
            df[col] = ""
    
    df = df[df["Publisher"].apply(lambda x: publisher_valido(x, editora))].copy()
    
    if limite and limite > 0:
        df = df.head(limite).copy()
    
    return df


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Baixa PDFs com sessão única - resolve CAPTCHA uma vez só!"
    )
    
    parser.add_argument("--csv", default="artigos.csv", help="Arquivo CSV com os artigos")
    parser.add_argument("--editora", choices=["wiley", "taylor"], default="wiley", 
                       help="Editora: wiley ou taylor")
    parser.add_argument("--saida", default="pdfs", help="Pasta para salvar os PDFs")
    parser.add_argument("--log", default="download_log.csv", help="Arquivo de log")
    parser.add_argument("--limite", type=int, default=0, help="Limite de artigos")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay entre downloads")
    
    args = parser.parse_args()
    
    caminho_csv = Path(args.csv)
    pasta_saida = Path(args.saida)
    caminho_log = Path(args.log)
    
    if not caminho_csv.exists():
        print(f"❌ Arquivo não encontrado: {caminho_csv}")
        return
    
    print("\n" + "="*60)
    print(f"📚 BAIXADOR DE ARTIGOS - {args.editora.upper()}")
    print("="*60)
    
    # Filtrar CSV
    df = ler_csv_filtrado(caminho_csv, args.editora, args.limite)
    
    if df.empty:
        print(f"❌ Nenhum artigo da {args.editora} encontrado no CSV")
        return
    
    print(f"📄 Artigos encontrados: {len(df)}")
    
    # Baixar todos
    logs = baixar_todos_artigos(df, pasta_saida, args.editora, args.delay)
    
    # Salvar log
    if logs:
        pd.DataFrame(logs, columns=LOG_COLUMNS).to_csv(caminho_log, index=False, encoding="utf-8-sig")
    
    # Resumo
    print("\n" + "="*60)
    print("📊 RESUMO FINAL")
    print("="*60)
    
    if logs:
        status_counts = pd.Series([x["status"] for x in logs]).value_counts()
        for status, count in status_counts.items():
            emoji = "✅" if status == "baixado" else "📁" if status == "ja_existia" else "❌"
            print(f"   {emoji} {status}: {count}")
    
    print(f"\n📁 PDFs salvos em: {pasta_saida.resolve()}")
    print(f"📄 Log salvo em: {caminho_log.resolve()}")


if __name__ == "__main__":
    main()