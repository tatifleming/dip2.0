#!/usr/bin/env python3
# -*- coding: utf-8 -*-

r"""
baixar_pdfs_mdpi_pipeline.py

Fluxo em duas etapas, no mesmo programa:

1) Lê artigos.csv, ignora a coluna Link/Scopus, filtra Publisher MDPI e gera
   links_pendentes_mdpi.html com os links oficiais da MDPI.

2) Lê o próprio links_pendentes_mdpi.html e baixa os PDFs da coluna PDF,
   salvando-os na pasta escolhida e renomeando com a coluna Nome sugerido.

Instalação:
    pip install pandas requests beautifulsoup4 selenium

Uso recomendado:
    python baixar_pdfs_mdpi_pipeline.py --csv artigos.csv --saida pdfs_mdpi --log download_log_mdpi.csv

Teste com 5 artigos:
    python baixar_pdfs_mdpi_pipeline.py --csv artigos.csv --saida pdfs_mdpi --log download_log_mdpi.csv --limite 5

Somente gerar o HTML:
    python baixar_pdfs_mdpi_pipeline.py --modo gerar-html --csv artigos.csv

Somente baixar a partir de um HTML já existente:
    python baixar_pdfs_mdpi_pipeline.py --modo baixar-html --html links_pendentes_mdpi.html --saida pdfs_mdpi

Modos do navegador:
    --modo-navegador oculto     abre o navegador fora da tela/minimizado; costuma ser menos bloqueado que headless
    --modo-navegador headless   navegador invisível real; pode ser mais bloqueado pela MDPI
    --modo-navegador visivel    mostra o navegador

O programa usa apenas links oficiais mdpi.com/mdpi-res.com e não tenta burlar paywall,
captcha ou bloqueio. Se a MDPI retornar Access Denied, o item é registrado no log.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote, unquote, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup


PUBLISHERS_MDPI = {
    "MDPI",
    "Multidisciplinary Digital Publishing Institute (MDPI)",
    "MDPI AG",
}

DOMINIOS_OFICIAIS = {
    "mdpi.com",
    "www.mdpi.com",
    "mdpi-res.com",
    "www.mdpi-res.com",
}

# Mapeamento de código DOI MDPI -> ISSN usado na URL oficial.
# Ex.: 10.3390/su18063065 -> código "su" -> /2071-1050/18/6/3065
MDPI_CODE_TO_ISSN = {
    "su": "2071-1050",
    "math": "2227-7390",
    "en": "1996-1073",
    "app": "2076-3417",
    "ijerph": "1660-4601",
    "ijms": "1422-0067",
    "molecules": "1420-3049",
    "materials": "1996-1944",
    "sensors": "1424-8220",
    "s": "1424-8220",
    "water": "2073-4441",
    "electronics": "2079-9292",
    "remotesensing": "2072-4292",
    "rs": "2072-4292",
    "polymers": "2073-4360",
    "foods": "2304-8158",
    "animals": "2076-2615",
    "agriculture": "2077-0472",
    "buildings": "2075-5309",
    "processes": "2227-9717",
    "pr": "2227-9717",
    "jcm": "2077-0383",
    "biomedicines": "2227-9059",
    "plants": "2223-7747",
    "viruses": "1999-4915",
    "antibiotics": "2079-6382",
    "nutrients": "2072-6643",
    "pharmaceutics": "1999-4923",
    "cells": "2073-4409",
    "cancers": "2072-6694",
    "genes": "2073-4425",
    "microorganisms": "2076-2607",
    "life": "2075-1729",
    "toxins": "2072-6651",
    "metals": "2075-4701",
    "insects": "2075-4450",
    "forests": "1999-4907",
    "f": "1999-4907",
    "atmosphere": "2073-4433",
    "climate": "2225-1154",
    "cli": "2225-1154",
    "land": "2073-445X",
    "tourhosp": "2673-5768",
    "fire": "2571-6255",
    "ecologies": "2673-4133",
    "jrfm": "1911-8074",
    "e": "1099-4300",
    "fractalfract": "2504-3110",
    "wevj": "2032-6653",
    "bdcc": "2504-2289",
    "systems": "2079-8954",
    "forecast": "2571-9394",
    "resources": "2079-9276",
    "risks": "2227-9091",
    "computers": "2073-431X",
    "cleantechnol": "2571-8797",
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
    pdf_url: str
    nome_sugerido: str
    motivo_original: str = ""


def txt(valor) -> str:
    if pd.isna(valor):
        return ""
    return str(valor).strip()


def limpar_doi(valor) -> str:
    s = unquote(txt(valor))
    if not s:
        return ""
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    m = DOI_RE.search(s)
    doi = m.group(0) if m else s
    return doi.strip().rstrip(".,;)").lower()


def dominio_oficial(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in DOMINIOS_OFICIAIS or host.endswith(".mdpi.com") or host.endswith(".mdpi-res.com")


def acesso_aberto(valor) -> bool:
    s = txt(valor).lower()
    if not s:
        return False
    negativos = {"no", "false", "0", "closed", "closed access", "subscription"}
    if s in negativos or "not open" in s or "closed" in s or "subscription" in s:
        return False
    positivos = {"yes", "true", "1", "oa", "open", "open access", "gold", "gold open access"}
    return s in positivos or "open access" in s


def slugify(texto: str, limite: int = 80) -> str:
    texto = unicodedata.normalize("NFKD", txt(texto))
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return (texto or "sem-info")[:limite].strip("-")


def primeiro_autor(authors: str) -> str:
    autores = txt(authors)
    if not autores:
        return "autor-desconhecido"
    for sep in [";", "|", " and "]:
        if sep in autores:
            return autores.split(sep)[0].strip()
    if "," in autores:
        return autores.split(",")[0].strip()
    return autores.split()[0].strip()


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
        ident = hashlib.sha1(f"{titulo}|{autores}".encode("utf-8")).hexdigest()[:10]

    nome = f"{ano}_{autor}_{titulo_slug}_{ident}.pdf"
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", nome)

    if len(nome) > 190:
        sufixo = f"_{ident}.pdf"
        nome = nome[: 190 - len(sufixo)].rstrip("_-. ") + sufixo

    return nome


def nome_arquivo_seguro(nome: str) -> str:
    nome = txt(nome)
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", nome)
    nome = re.sub(r"\s+", " ", nome).strip()
    if not nome.lower().endswith(".pdf"):
        nome += ".pdf"
    return nome[:190]


def parse_doi_mdpi_direto(doi: str) -> Optional[dict[str, str]]:
    """
    Constrói URLs MDPI a partir do DOI 10.3390/<codigo><volume><issue><artigo>.

    Corrige o caso que a versão anterior errava:
      su172411069 -> /17/24/11069, não /172/41/1069
      app152111561 -> /15/21/11561, não /152/11/1561
    """
    doi = limpar_doi(doi)
    if not doi.startswith("10.3390/"):
        return None

    sufixo = doi.split("/", 1)[1].lower()
    m = re.fullmatch(r"([a-z]+)(\d+)", sufixo)
    if not m:
        return None

    codigo, digitos = m.group(1), m.group(2)
    issn = MDPI_CODE_TO_ISSN.get(codigo)
    if not issn:
        return None

    candidatos = []
    # Ordem de preferência:
    # - volume com 2 dígitos resolve a maioria;
    # - volume com 1 dígito resolve periódicos com volume 7, 8, 9;
    # - volume com 3 dígitos fica como fallback.
    for vol_len in [2, 1, 3]:
        if len(digitos) <= vol_len + 2:
            continue
        vol = digitos[:vol_len]
        issue = digitos[vol_len:vol_len + 2]
        artigo = digitos[vol_len + 2:]

        if not vol or not issue or not artigo:
            continue

        try:
            vol_i = int(vol)
            issue_i = int(issue)
            artigo_i = int(artigo)
        except ValueError:
            continue

        # Filtros conservadores para evitar URLs absurdas.
        if not (1 <= vol_i <= 80):
            continue
        if not (1 <= issue_i <= 60):
            continue
        if not (1 <= artigo_i <= 200000):
            continue
        if len(artigo) < 3:
            continue

        candidatos.append((vol_len, vol_i, issue_i, artigo_i))

    if not candidatos:
        return None

    # Prioriza volume de 2 dígitos; se não houver, 1 dígito.
    candidatos.sort(key=lambda x: {2: 0, 1: 1, 3: 2}.get(x[0], 9))
    _, volume, issue, artigo = candidatos[0]

    article_url = f"https://www.mdpi.com/{issn}/{volume}/{issue}/{artigo}"
    return {
        "article_url": article_url,
        "pdf_url": article_url + "/pdf?download=1",
        "origem": "doi_mdpi_direto",
    }


def crossref_lookup(doi: str, session: requests.Session, email: str = "", timeout: int = 12) -> Optional[dict[str, str]]:
    """Fallback: usa Crossref só para metadados, não para baixar PDF."""
    doi = limpar_doi(doi)
    if not doi:
        return None

    headers = {}
    if email:
        headers["User-Agent"] = f"MDPI-Pipeline/2.0 (mailto:{email})"

    url = f"https://api.crossref.org/works/{quote(doi, safe='/')}"
    try:
        r = session.get(url, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            return None
        msg = r.json().get("message", {})
    except Exception:
        return None

    cr_url = txt(msg.get("URL"))
    if cr_url and dominio_oficial(cr_url):
        base = cr_url.rstrip("/")
        return {
            "article_url": base,
            "pdf_url": base + "/pdf?download=1",
            "origem": "crossref_url",
        }

    issns = msg.get("ISSN") or []
    volume = txt(msg.get("volume"))
    issue = txt(msg.get("issue"))
    article_number = txt(msg.get("article-number")) or txt(msg.get("page"))

    if issns and volume and issue and article_number:
        issn = txt(issns[0])
        article_number = article_number.split("-")[0].strip()
        if issn and re.fullmatch(r"[0-9Xx-]+", issn) and re.fullmatch(r"[0-9A-Za-z.-]+", article_number):
            base = f"https://www.mdpi.com/{issn}/{volume}/{issue}/{article_number}"
            return {
                "article_url": base,
                "pdf_url": base + "/pdf?download=1",
                "origem": "crossref_metadados",
            }

    return None


def criar_sessao(email: str = "") -> requests.Session:
    session = requests.Session()
    ua = "MDPI-Pipeline/2.0"
    if email:
        ua += f" ({email})"
    session.headers.update({
        "User-Agent": ua,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    return session


def obter_url_mdpi(row: dict, session: requests.Session, email: str = "", usar_crossref: bool = True) -> tuple[Optional[dict[str, str]], str]:
    doi = limpar_doi(row.get("DOI", ""))
    if not doi:
        return None, "DOI ausente"

    info = parse_doi_mdpi_direto(doi)
    if info:
        return info, ""

    if usar_crossref:
        info = crossref_lookup(doi, session, email=email)
        if info:
            return info, ""

    return None, "não foi possível montar URL oficial MDPI pelo DOI"


def ler_csv_filtrado(caminho_csv: Path, limite: int = 0) -> pd.DataFrame:
    df = pd.read_csv(caminho_csv, dtype=str).fillna("")
    for col in [
        "Authors", "Title", "Year", "DOI", "Link", "Cited by",
        "Abstract", "Document Type", "Publisher", "Open Access",
    ]:
        if col not in df.columns:
            df[col] = ""

    df = df[df["Publisher"].astype(str).str.strip().isin(PUBLISHERS_MDPI)].copy()
    if limite and limite > 0:
        df = df.head(limite).copy()
    return df


def gerar_html_pendentes(
    caminho_csv: Path,
    caminho_html: Path,
    caminho_csv_pendentes: Path,
    limite: int,
    email: str,
    usar_crossref: bool,
) -> list[ItemPDF]:
    df = ler_csv_filtrado(caminho_csv, limite=limite)
    session = criar_sessao(email)

    itens: list[ItemPDF] = []
    linhas_csv = []

    print(f"Artigos MDPI filtrados: {len(df)}")
    print("Gerando HTML de links oficiais. A coluna Link/Scopus será ignorada.\n")

    for i, row in enumerate(df.to_dict(orient="records"), start=1):
        titulo = txt(row.get("Title", ""))
        doi = limpar_doi(row.get("DOI", ""))
        nome = nome_seguro_pdf(row)

        if not acesso_aberto(row.get("Open Access", "")):
            motivo = "ignorado: coluna Open Access não indica acesso aberto"
            artigo_url = ""
            pdf_url = ""
        else:
            info, falha = obter_url_mdpi(row, session, email=email, usar_crossref=usar_crossref)
            if info:
                artigo_url = info["article_url"]
                pdf_url = info["pdf_url"]
                motivo = f"link oficial gerado por {info.get('origem', '')}"
                itens.append(ItemPDF(titulo, doi, artigo_url, pdf_url, nome, motivo))
            else:
                artigo_url = ""
                pdf_url = ""
                motivo = falha

        linhas_csv.append({
            "titulo": titulo,
            "DOI": doi,
            "url_artigo_mdpi": artigo_url,
            "url_pdf_oficial": pdf_url,
            "nome_arquivo_sugerido": nome,
            "motivo": motivo,
        })

        print(f"[{i}/{len(df)}] {doi or 'sem DOI'} -> {motivo}")

    pd.DataFrame(linhas_csv).to_csv(caminho_csv_pendentes, index=False, encoding="utf-8-sig")

    html_lines = [
        "<!doctype html>",
        '<html lang="pt-BR"><head><meta charset="utf-8"><title>Links MDPI pendentes</title>',
        "<style>body{font-family:Arial,sans-serif;margin:20px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px;vertical-align:top}th{background:#f2f2f2}</style>",
        "</head><body><h1>Links oficiais MDPI pendentes</h1>",
        "<p>Arquivo gerado automaticamente a partir do CSV. A coluna Link/Scopus foi ignorada. Use apenas para artigos em acesso aberto.</p>",
        "<table><thead><tr><th>Título</th><th>DOI</th><th>Artigo</th><th>PDF</th><th>Nome sugerido</th><th>Motivo</th></tr></thead><tbody>",
    ]

    for r in linhas_csv:
        artigo = r["url_artigo_mdpi"]
        pdf = r["url_pdf_oficial"]
        html_lines.append(
            "<tr>"
            f"<td>{html_lib.escape(r['titulo'])}</td>"
            f"<td>{html_lib.escape(r['DOI'])}</td>"
            f"<td>{f'<a href={html_lib.escape(artigo)!r}>artigo</a>' if artigo else ''}</td>"
            f"<td>{f'<a href={html_lib.escape(pdf)!r}>PDF</a>' if pdf else ''}</td>"
            f"<td>{html_lib.escape(r['nome_arquivo_sugerido'])}</td>"
            f"<td>{html_lib.escape(r['motivo'])}</td>"
            "</tr>"
        )

    html_lines.extend(["</tbody></table></body></html>"])
    caminho_html.write_text("\n".join(html_lines), encoding="utf-8")

    print(f"\nHTML gerado: {caminho_html.resolve()}")
    print(f"CSV gerado:  {caminho_csv_pendentes.resolve()}")
    return itens


def ler_itens_do_html(caminho_html: Path) -> list[ItemPDF]:
    html = caminho_html.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    tabela = soup.find("table")
    if tabela is None:
        raise ValueError("Nenhuma tabela encontrada no HTML.")

    cabecalhos = [th.get_text(" ", strip=True) for th in tabela.find_all("th")]
    idx = {nome: i for i, nome in enumerate(cabecalhos)}

    obrigatorias = ["Título", "DOI", "Artigo", "PDF", "Nome sugerido"]
    faltando = [c for c in obrigatorias if c not in idx]
    if faltando:
        raise ValueError(f"Colunas ausentes no HTML: {', '.join(faltando)}")

    itens: list[ItemPDF] = []

    for tr in tabela.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if not tds:
            continue

        def texto(coluna: str) -> str:
            i = idx[coluna]
            return tds[i].get_text(" ", strip=True) if i < len(tds) else ""

        def href(coluna: str) -> str:
            i = idx[coluna]
            if i >= len(tds):
                return ""
            a = tds[i].find("a", href=True)
            return a["href"].strip() if a else ""

        pdf_url = href("PDF")
        artigo_url = href("Artigo")
        if not pdf_url or not dominio_oficial(pdf_url):
            continue

        motivo = texto("Motivo") if "Motivo" in idx else ""

        itens.append(ItemPDF(
            titulo=texto("Título"),
            doi=texto("DOI"),
            artigo_url=artigo_url,
            pdf_url=pdf_url,
            nome_sugerido=nome_arquivo_seguro(texto("Nome sugerido")),
            motivo_original=motivo,
        ))

    return itens


def criar_driver(
    browser: str,
    pasta_download: Path,
    modo_navegador: str,
    user_data_dir: Optional[str],
    profile_directory: Optional[str],
):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.edge.options import Options as EdgeOptions

    prefs = {
        "download.default_directory": str(pasta_download.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
        "plugins.always_open_pdf_externally": True,
        "download.extensions_to_open": "",
        "safebrowsing.enabled": True,
    }

    browser = browser.lower()
    modo_navegador = modo_navegador.lower()

    if browser == "edge":
        options = EdgeOptions()
    elif browser == "chrome":
        options = ChromeOptions()
    else:
        raise ValueError("browser deve ser edge ou chrome")

    options.add_experimental_option("prefs", prefs)
    options.page_load_strategy = "eager"

    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")
    options.add_argument("--log-level=3")

    if modo_navegador == "headless":
        options.add_argument("--headless=new")
    elif modo_navegador == "oculto":
        # Não é headless: reduz chance de bloqueio, mas joga a janela para fora da tela/minimizada.
        options.add_argument("--window-position=-32000,-32000")
        options.add_argument("--window-size=1200,900")
        options.add_argument("--start-minimized")
    elif modo_navegador == "visivel":
        options.add_argument("--window-size=1200,900")
    else:
        raise ValueError("modo-navegador deve ser oculto, headless ou visivel")

    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
    if profile_directory:
        options.add_argument(f"--profile-directory={profile_directory}")

    driver = webdriver.Edge(options=options) if browser == "edge" else webdriver.Chrome(options=options)

    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(pasta_download.resolve())},
        )
    except Exception:
        pass

    return driver


def pagina_indica_bloqueio(driver) -> bool:
    try:
        titulo = (driver.title or "").lower()
    except Exception:
        titulo = ""
    try:
        body = driver.find_element("tag name", "body").text.lower()
    except Exception:
        body = ""
    texto = titulo + "\n" + body[:3000]
    sinais = [
        "access denied",
        "you don't have permission",
        "request blocked",
        "forbidden",
        "challenge validation",
        "captcha",
        "akamai",
        "errors.edgesuite.net",
    ]
    return any(s in texto for s in sinais)


def snapshot_downloads(pasta: Path) -> dict[str, tuple[int, float]]:
    estado = {}
    for p in pasta.iterdir():
        if p.is_file():
            try:
                estado[p.name] = (p.stat().st_size, p.stat().st_mtime)
            except OSError:
                pass
    return estado


def parece_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def pdfs_novos(pasta: Path, antes: dict[str, tuple[int, float]]) -> list[Path]:
    novos = []
    for p in pasta.glob("*.pdf"):
        try:
            st = p.stat()
        except OSError:
            continue
        old = antes.get(p.name)
        if old is None or st.st_mtime > old[1] + 0.5 or st.st_size != old[0]:
            novos.append(p)
    novos.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return novos


def arquivo_estavel(path: Path, segundos: float = 0.8) -> bool:
    try:
        s1 = path.stat().st_size
        time.sleep(segundos)
        s2 = path.stat().st_size
        return s1 == s2 and s2 > 0
    except OSError:
        return False


def arquivos_temporarios(pasta: Path) -> list[Path]:
    return [p for p in pasta.iterdir() if p.is_file() and p.suffix.lower() in {".crdownload", ".tmp", ".part"}]


def esperar_download(driver, pasta: Path, antes: dict[str, tuple[int, float]], timeout: int) -> tuple[Optional[Path], str]:
    inicio = time.time()
    ultimo = "aguardando PDF"

    while time.time() - inicio < timeout:
        if pagina_indica_bloqueio(driver):
            return None, "bloqueado_access_denied"

        novos = pdfs_novos(pasta, antes)
        if novos:
            cand = novos[0]
            if arquivo_estavel(cand):
                if parece_pdf(cand):
                    return cand, "ok"
                return None, f"arquivo baixado não parece PDF: {cand.name}"

        temps = arquivos_temporarios(pasta)
        if temps:
            ultimo = "download em andamento: " + ", ".join(p.name for p in temps[:3])
        else:
            ultimo = "nenhum PDF apareceu ainda"

        time.sleep(0.5)

    return None, f"timeout: {ultimo}"


def mover_para_nome_sugerido(origem: Path, destino: Path) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)

    if destino.exists() and destino.stat().st_size > 0 and parece_pdf(destino):
        if origem.resolve() != destino.resolve():
            origem.unlink(missing_ok=True)
        return destino

    if origem.resolve() == destino.resolve():
        return destino

    final = destino
    contador = 2
    while final.exists():
        final = destino.with_name(f"{destino.stem}_{contador}{destino.suffix}")
        contador += 1

    shutil.move(str(origem), str(final))
    return final


def baixar_item_browser(
    driver,
    item: ItemPDF,
    pasta_saida: Path,
    timeout_pagina: int,
    timeout_download: int,
    pausa_artigo: float,
) -> dict[str, str]:
    from selenium.common.exceptions import TimeoutException, WebDriverException

    destino = pasta_saida / item.nome_sugerido
    log = {
        "titulo": item.titulo,
        "DOI": item.doi,
        "artigo_url": item.artigo_url,
        "pdf_url": item.pdf_url,
        "status": "",
        "arquivo_salvo": "",
        "motivo": "",
    }

    if destino.exists() and destino.stat().st_size > 0 and parece_pdf(destino):
        log["status"] = "ja_existia"
        log["arquivo_salvo"] = str(destino)
        return log

    if not dominio_oficial(item.pdf_url):
        log["status"] = "ignorado"
        log["motivo"] = "PDF não pertence a domínio oficial MDPI"
        return log

    try:
        driver.set_page_load_timeout(timeout_pagina)
    except Exception:
        pass

    if item.artigo_url and dominio_oficial(item.artigo_url):
        try:
            print("    abrindo artigo...")
            driver.get(item.artigo_url)
            time.sleep(pausa_artigo)
            if pagina_indica_bloqueio(driver):
                log["status"] = "bloqueado_access_denied"
                log["motivo"] = "página do artigo retornou Access Denied"
                return log
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        except WebDriverException as exc:
            log["status"] = "erro_navegador"
            log["motivo"] = f"erro ao abrir artigo: {exc}"
            return log

    antes = snapshot_downloads(pasta_saida)

    try:
        print("    solicitando PDF...")
        driver.get(item.pdf_url)
    except TimeoutException:
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
    except WebDriverException as exc:
        log["status"] = "erro_navegador"
        log["motivo"] = f"erro ao abrir PDF: {exc}"
        return log

    baixado, motivo = esperar_download(driver, pasta_saida, antes, timeout_download)

    if baixado is None:
        log["status"] = "pendente"
        log["motivo"] = motivo
        return log

    try:
        final = mover_para_nome_sugerido(baixado, destino)
    except OSError as exc:
        log["status"] = "falha_salvar"
        log["motivo"] = f"erro ao renomear/salvar: {exc}"
        return log

    log["status"] = "baixado"
    log["arquivo_salvo"] = str(final)
    return log


def salvar_log(caminho_log: Path, logs: Iterable[dict[str, str]]) -> None:
    pd.DataFrame(list(logs), columns=LOG_COLUMNS).to_csv(caminho_log, index=False, encoding="utf-8-sig")


def baixar_do_html(
    caminho_html: Path,
    pasta_saida: Path,
    caminho_log: Path,
    limite: int,
    browser: str,
    modo_navegador: str,
    timeout_pagina: int,
    timeout_download: int,
    pausa_artigo: float,
    delay: float,
    user_data_dir: Optional[str],
    profile_directory: Optional[str],
) -> list[dict[str, str]]:
    itens = ler_itens_do_html(caminho_html)
    if limite and limite > 0:
        itens = itens[:limite]

    pasta_saida.mkdir(parents=True, exist_ok=True)

    print(f"\nItens com PDF oficial no HTML: {len(itens)}")
    print(f"Pasta de destino: {pasta_saida.resolve()}")
    print(f"Navegador: {browser} | modo={modo_navegador}")
    print("O navegador será configurado para SALVAR PDFs, não para abri-los no visualizador.\n")

    driver = None
    logs: list[dict[str, str]] = []

    try:
        driver = criar_driver(
            browser=browser,
            pasta_download=pasta_saida,
            modo_navegador=modo_navegador,
            user_data_dir=user_data_dir,
            profile_directory=profile_directory,
        )

        total = len(itens)
        for i, item in enumerate(itens, start=1):
            print(f"[{i}/{total}] DOI={item.doi} | {item.titulo[:85]}")

            try:
                log = baixar_item_browser(
                    driver=driver,
                    item=item,
                    pasta_saida=pasta_saida,
                    timeout_pagina=timeout_pagina,
                    timeout_download=timeout_download,
                    pausa_artigo=pausa_artigo,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log = {
                    "titulo": item.titulo,
                    "DOI": item.doi,
                    "artigo_url": item.artigo_url,
                    "pdf_url": item.pdf_url,
                    "status": "erro_inesperado",
                    "arquivo_salvo": "",
                    "motivo": str(exc),
                }

            logs.append(log)
            salvar_log(caminho_log, logs)

            print(f"    -> {log['status']}" + (f" - {log['motivo']}" if log["motivo"] else ""))

            if delay > 0 and i < total:
                time.sleep(delay)

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    return logs


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera links_pendentes_mdpi.html e baixa PDFs oficiais MDPI a partir desse HTML.")
    parser.add_argument("--modo", choices=["pipeline", "gerar-html", "baixar-html"], default="pipeline")
    parser.add_argument("--csv", default="artigos.csv", help="CSV de entrada.")
    parser.add_argument("--html", default="links_pendentes_mdpi.html", help="HTML de links oficiais.")
    parser.add_argument("--pendentes-csv", default="links_pendentes_mdpi.csv", help="CSV de links oficiais.")
    parser.add_argument("--saida", default="pdfs_mdpi", help="Pasta onde salvar PDFs.")
    parser.add_argument("--log", default="download_log_mdpi.csv", help="Log dos downloads.")
    parser.add_argument("--limite", type=int, default=0, help="Processa apenas N artigos/itens.")
    parser.add_argument("--email", default="", help="E-mail opcional para User-Agent/Crossref.")
    parser.add_argument("--sem-crossref", action="store_true", help="Não usa Crossref como fallback de metadados.")

    parser.add_argument("--browser", choices=["edge", "chrome"], default="edge")
    parser.add_argument(
        "--modo-navegador",
        choices=["oculto", "headless", "visivel"],
        default="oculto",
        help="Padrão: oculto. 'headless' pode ser mais bloqueado pela MDPI; 'visivel' mostra janela.",
    )
    parser.add_argument("--timeout-pagina", type=int, default=20)
    parser.add_argument("--timeout-download", type=int, default=35)
    parser.add_argument("--pausa-artigo", type=float, default=1.0)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--user-data-dir", default=None)
    parser.add_argument("--profile-directory", default=None)

    args = parser.parse_args()

    caminho_csv = Path(args.csv)
    caminho_html = Path(args.html)
    caminho_pendentes_csv = Path(args.pendentes_csv)
    pasta_saida = Path(args.saida)
    caminho_log = Path(args.log)

    if args.modo in {"pipeline", "gerar-html"}:
        if not caminho_csv.exists():
            raise FileNotFoundError(f"CSV não encontrado: {caminho_csv}")

        gerar_html_pendentes(
            caminho_csv=caminho_csv,
            caminho_html=caminho_html,
            caminho_csv_pendentes=caminho_pendentes_csv,
            limite=args.limite,
            email=args.email,
            usar_crossref=not args.sem_crossref,
        )

    if args.modo in {"pipeline", "baixar-html"}:
        if not caminho_html.exists():
            raise FileNotFoundError(f"HTML não encontrado: {caminho_html}")

        logs = baixar_do_html(
            caminho_html=caminho_html,
            pasta_saida=pasta_saida,
            caminho_log=caminho_log,
            limite=args.limite,
            browser=args.browser,
            modo_navegador=args.modo_navegador,
            timeout_pagina=args.timeout_pagina,
            timeout_download=args.timeout_download,
            pausa_artigo=args.pausa_artigo,
            delay=args.delay,
            user_data_dir=args.user_data_dir,
            profile_directory=args.profile_directory,
        )

        print("\nConcluído.")
        print(f"PDFs salvos em: {pasta_saida.resolve()}")
        print(f"Log salvo em: {caminho_log.resolve()}")

        if logs:
            print("\nResumo:")
            print(pd.Series([x["status"] for x in logs]).value_counts().to_string())


if __name__ == "__main__":
    main()
