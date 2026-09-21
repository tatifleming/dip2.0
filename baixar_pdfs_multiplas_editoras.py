#!/usr/bin/env python3
# -*- coding: utf-8 -*-

r"""
baixar_pdfs_multiplas_editoras.py

Versão mais rápida e conservadora do baixador original.

Mudanças principais:
- Removeu Academic Press / ScienceDirect.
- Timeout de PDF menor e configurável.
- Menos sleeps fixos; usa esperas do Selenium quando possível.
- Detecta arquivos .crdownload para acompanhar downloads em andamento.
- Mantém sessão única do Chrome para reaproveitar cookies/login/CAPTCHA resolvido manualmente.

Importante:
Este script não contorna paywalls, CAPTCHAs ou controles de acesso. Ele apenas tenta baixar PDFs
que estejam acessíveis pela sessão aberta no navegador.

Editoras suportadas:
- Frontiers
- SAGE
- World Scientific
- Emerald
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ============================================================
# Configurações das Editoras
# ============================================================

# Frontiers Media
PUBLISHERS_FRONTIERS = {
    "frontiers media s.a.",
    "frontiers media sa",
    "frontiers media s. a",
    "frontiers media s.a",
    "frontiers",
}

# SAGE Publications
PUBLISHERS_SAGE = {
    "sage publications inc.",
    "sage publications inc",
    "sage publications ltd",
    "sage publications india pvt. ltd",
    "sage publications",
    "sage",
}

# World Scientific
PUBLISHERS_WORLD_SCIENTIFIC = {
    "world scientific",
    "world scientific publishing co. pte ltd",
    "world scientific publ co pte ltd",
    "world scientific pub co pte ltd",
}

# Emerald Publishing
PUBLISHERS_EMERALD = {
    "emerald publishing",
    "emerald group holdings ltd.",
    "emerald group publishing ltd",
    "emerald",
}

# Mapeamento de editora para domínios e estratégias de URL
PUBLISHER_CONFIG = {
    "frontiers": {
        "nomes": PUBLISHERS_FRONTIERS,
        "dominios": ["frontiersin.org", "www.frontiersin.org"],
        "need_access": False,
    },
    "sage": {
        "nomes": PUBLISHERS_SAGE,
        "dominios": ["journals.sagepub.com", "sagepub.com"],
        "need_access": False,
    },
    "world_scientific": {
        "nomes": PUBLISHERS_WORLD_SCIENTIFIC,
        "dominios": ["worldscientific.com", "www.worldscientific.com"],
        "need_access": True,
    },
    "emerald": {
        "nomes": PUBLISHERS_EMERALD,
        "dominios": ["emerald.com", "www.emerald.com"],
        "need_access": True,
    },
}

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)

LOG_COLUMNS = [
    "titulo",
    "DOI",
    "artigo_url",
    "pdf_url",
    "status",
    "arquivo_salvo",
    "editora_detectada",
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
    editora_tipo: str = ""


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


def detectar_editora(publisher: str) -> Optional[str]:
    """Detecta qual editora com base no nome do publisher."""
    pub_normalizado = normalizar_texto(publisher)

    for editora_tipo, config in PUBLISHER_CONFIG.items():
        for nome in config["nomes"]:
            if normalizar_texto(nome) == pub_normalizado:
                return editora_tipo

    return None


def publisher_valido(valor) -> bool:
    """Verifica se o publisher está na lista de editoras suportadas."""
    return detectar_editora(valor) is not None


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
    for sep in [";", "|", " and ", ","]:
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


def montar_urls(doi: str, editora_tipo: str) -> tuple[str, list[str]]:
    """Retorna (artigo_url, lista_pdf_urls) baseado no tipo de editora."""
    doi_path = quote(doi, safe="/")

    if editora_tipo == "frontiers":
        artigo_url = f"https://www.frontiersin.org/journals/-/articles/{doi_path}"
        pdf_urls = [
            f"https://www.frontiersin.org/articles/{doi_path}/pdf",
            f"https://www.frontiersin.org/articles/{doi_path}/full",
        ]

    elif editora_tipo == "sage":
        artigo_url = f"https://journals.sagepub.com/doi/full/{doi_path}"
        pdf_urls = [
            f"https://journals.sagepub.com/doi/pdf/{doi_path}",
            f"https://journals.sagepub.com/doi/pdf/{doi_path}?download=true",
        ]

    elif editora_tipo == "world_scientific":
        artigo_url = f"https://www.worldscientific.com/doi/abs/{doi_path}"
        pdf_urls = [
            f"https://www.worldscientific.com/doi/pdf/{doi_path}",
            f"https://www.worldscientific.com/doi/epdf/{doi_path}",
        ]

    elif editora_tipo == "emerald":
        artigo_url = f"https://www.emerald.com/insight/doi/full/{doi_path}"
        pdf_urls = [
            f"https://www.emerald.com/insight/doi/pdf/{doi_path}",
            f"https://www.emerald.com/insight/doi/epdf/{doi_path}",
        ]

    else:
        artigo_url = f"https://doi.org/{doi_path}"
        pdf_urls = [f"https://doi.org/{doi_path}"]

    return artigo_url, pdf_urls


def parece_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def snapshot_arquivos(pasta: Path) -> set[Path]:
    """Retorna os arquivos atuais da pasta, ignorando diretórios."""
    return {p for p in pasta.iterdir() if p.is_file()}


def arquivo_estavel(path: Path, pausa: float = 0.7) -> bool:
    """Verifica se o tamanho do arquivo parou de mudar."""
    try:
        tamanho_1 = path.stat().st_size
        time.sleep(pausa)
        tamanho_2 = path.stat().st_size
        return tamanho_1 == tamanho_2 and tamanho_2 > 0
    except OSError:
        return False


def mover_para_destino(origem: Path, destino: Path) -> Path:
    if origem.resolve() == destino.resolve():
        return destino
    if destino.exists():
        destino.unlink()
    shutil.move(str(origem), str(destino))
    return destino


# ============================================================
# Selenium e downloads
# ============================================================

def criar_driver_com_pasta_download(pasta_download: Path, page_load_timeout: int = 20):
    """Cria driver do Chrome com pasta de download configurada."""
    options = Options()

    prefs = {
        "download.default_directory": str(pasta_download.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "profile.default_content_settings.popups": 0,
        "safebrowsing.enabled": True,
    }
    options.add_experimental_option("prefs", prefs)

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1300,900")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(page_load_timeout)
    return driver


def carregar_url(driver, url: str, page_wait: int = 12) -> bool:
    """Carrega URL com timeout curto e tenta parar carregamentos longos."""
    try:
        driver.get(url)
    except TimeoutException:
        try:
            driver.execute_script("window.stop();")
        except WebDriverException:
            pass
        return False
    except WebDriverException:
        return False

    try:
        WebDriverWait(driver, page_wait).until(
            lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"}
        )
    except Exception:
        return False

    return True


def aguardar_resolucao_captcha(driver, timeout: int = 300) -> bool:
    """Aguarda o usuário resolver o CAPTCHA manualmente."""
    print("\n" + "=" * 60)
    print("⚠️  AGUARDANDO RESOLUÇÃO MANUAL DO CAPTCHA / LOGIN, SE NECESSÁRIO")
    print("=" * 60)
    print("O navegador está aberto. Por favor:")
    print("1. Resolva verificação/CAPTCHA se aparecer")
    print("2. Faça login institucional se for necessário e se você tiver acesso")
    print("3. Aguarde a página do artigo carregar")
    print("4. Depois volte aqui e pressione ENTER")
    print("=" * 60)

    input("\n>>> Pressione ENTER após resolver e carregar a página: ")

    try:
        WebDriverWait(driver, min(timeout, 10)).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        print("✅ Continuando...")
        return True
    except Exception:
        print("⚠️ Página pode não ter carregado completamente, continuando mesmo assim...")
        return True


def aguardar_novo_pdf(
    pasta_saida: Path,
    antes: set[Path],
    destino: Path,
    timeout_pdf: int = 15,
    intervalo: float = 0.5,
) -> tuple[Optional[Path], str]:
    """
    Aguarda aparecer um PDF novo na pasta de download.

    Retorna:
    - (Path, "ok") se encontrou um PDF válido
    - (None, motivo) se não encontrou
    """
    fim = time.monotonic() + timeout_pdf
    ultimo_motivo = "timeout_sem_pdf"

    while time.monotonic() < fim:
        agora = snapshot_arquivos(pasta_saida)
        novos = agora - antes

        downloads_parciais = [p for p in agora if p.suffix.lower() == ".crdownload"]
        if downloads_parciais:
            ultimo_motivo = "download_em_andamento_timeout"

        candidatos_pdf = [p for p in novos if p.suffix.lower() == ".pdf"]

        # Caso o Chrome baixe diretamente com o nome final esperado.
        if destino.exists() and destino not in antes:
            candidatos_pdf.append(destino)

        for candidato in candidatos_pdf:
            if not candidato.exists():
                continue
            if not arquivo_estavel(candidato):
                ultimo_motivo = "arquivo_ainda_mudando"
                continue
            if parece_pdf(candidato):
                return candidato, "ok"
            ultimo_motivo = "arquivo_baixado_nao_parece_pdf"

        time.sleep(intervalo)

    return None, ultimo_motivo


def baixar_pdf_com_sessao(
    driver,
    item: ItemPDF,
    pasta_saida: Path,
    timeout_pdf: int = 15,
    page_wait: int = 12,
) -> tuple[str, str, str]:
    """Usa a sessão existente para baixar o PDF."""
    destino = pasta_saida / item.nome_sugerido

    if destino.exists() and destino.stat().st_size > 0 and parece_pdf(destino):
        return "ja_existia", str(destino), "arquivo_pdf_ja_existia"

    motivos = []

    for pdf_url in item.pdf_urls:
        print(f"    > Tentando: {pdf_url}")
        antes = snapshot_arquivos(pasta_saida)

        carregou = carregar_url(driver, pdf_url, page_wait=page_wait)
        if not carregou:
            motivos.append(f"carregamento_lento_ou_falhou: {pdf_url}")

        candidato, motivo = aguardar_novo_pdf(
            pasta_saida=pasta_saida,
            antes=antes,
            destino=destino,
            timeout_pdf=timeout_pdf,
        )

        if candidato is None:
            motivos.append(f"{motivo}: {pdf_url}")
            continue

        mover_para_destino(candidato, destino)
        if parece_pdf(destino):
            return "baixado", str(destino), "pdf_baixado_com_sucesso"

        motivos.append(f"arquivo_final_nao_parece_pdf: {pdf_url}")

    return "falhou", "", " | ".join(motivos) if motivos else "nenhuma_url_funcionou"


# ============================================================
# Processo principal
# ============================================================

def preparar_itens(df: pd.DataFrame) -> list[ItemPDF]:
    itens = []

    for _, row in df.iterrows():
        doi = limpar_doi(row.get("DOI", ""))
        if not doi:
            continue

        publisher = txt(row.get("Publisher", ""))
        editora_tipo = detectar_editora(publisher)

        if not editora_tipo:
            continue

        artigo_url, pdf_urls = montar_urls(doi, editora_tipo)
        nome = nome_seguro_pdf(row)

        itens.append(
            ItemPDF(
                titulo=txt(row.get("Title", "")),
                doi=doi,
                artigo_url=artigo_url,
                pdf_urls=pdf_urls,
                nome_sugerido=nome,
                publisher=publisher,
                editora_tipo=editora_tipo,
            )
        )

    return itens


def baixar_todos_artigos(
    df: pd.DataFrame,
    pasta_saida: Path,
    delay: float = 0.5,
    timeout_pdf: int = 15,
    page_wait: int = 12,
    page_load_timeout: int = 20,
) -> list[dict]:
    """Processo principal com sessão única."""
    pasta_saida.mkdir(parents=True, exist_ok=True)

    itens = preparar_itens(df)

    if not itens:
        print("Nenhum item válido encontrado.")
        return []

    print(f"\n📋 Itens para processar: {len(itens)}")
    print(f"📁 Pasta de destino: {pasta_saida.resolve()}")
    print(f"⏱️ Timeout por URL de PDF: {timeout_pdf}s")
    print(f"⏱️ Delay entre artigos: {delay}s")

    editoras_count = {}
    for item in itens:
        editoras_count[item.editora_tipo] = editoras_count.get(item.editora_tipo, 0) + 1

    print("\n📊 Distribuição por editora:")
    for editora, count in editoras_count.items():
        print(f"    - {editora}: {count}")

    print("\n🚀 Abrindo navegador...")
    driver = criar_driver_com_pasta_download(
        pasta_download=pasta_saida,
        page_load_timeout=page_load_timeout,
    )

    logs = []

    try:
        primeiro_item = itens[0]
        print("\n🌐 Abrindo primeiro artigo para resolver CAPTCHA/login, se necessário...")
        print(f"   Editora: {primeiro_item.editora_tipo}")
        print(f"   URL: {primeiro_item.artigo_url}")

        carregar_url(driver, primeiro_item.artigo_url, page_wait=page_wait)
        aguardar_resolucao_captcha(driver)

        for i, item in enumerate(itens, start=1):
            print(f"\n[{i}/{len(itens)}] Editora: {item.editora_tipo}")
            print(f"    DOI: {item.doi}")
            print(f"    Título: {item.titulo[:70]}")

            if i > 1:
                carregar_url(driver, item.artigo_url, page_wait=page_wait)

            status, arquivo, motivo = baixar_pdf_com_sessao(
                driver=driver,
                item=item,
                pasta_saida=pasta_saida,
                timeout_pdf=timeout_pdf,
                page_wait=page_wait,
            )

            logs.append(
                {
                    "titulo": item.titulo,
                    "DOI": item.doi,
                    "artigo_url": item.artigo_url,
                    "pdf_url": item.pdf_urls[0] if item.pdf_urls else "",
                    "status": status,
                    "arquivo_salvo": arquivo,
                    "editora_detectada": item.editora_tipo,
                    "motivo": motivo,
                }
            )

            if status == "baixado":
                print(f"    ✅ Baixado: {Path(arquivo).name}")
            elif status == "ja_existia":
                print(f"    📁 Já existia: {Path(arquivo).name}")
            else:
                print(f"    ❌ Falhou: {motivo}")

            if delay > 0 and i < len(itens):
                time.sleep(delay)

    finally:
        print("\n🔒 Fechando navegador...")
        driver.quit()

    return logs


def ler_csv_filtrado(caminho_csv: Path, limite: int = 0) -> pd.DataFrame:
    """Lê e filtra o CSV pelos publishers configurados."""
    df = pd.read_csv(caminho_csv, dtype=str).fillna("")

    for col in ["Title", "DOI", "Publisher", "Year", "Authors"]:
        if col not in df.columns:
            df[col] = ""

    df = df[df["Publisher"].apply(publisher_valido)].copy()

    if limite and limite > 0:
        df = df.head(limite).copy()

    return df


def imprimir_editoras_suportadas() -> None:
    print("\nEditoras suportadas:")
    for editora, config in PUBLISHER_CONFIG.items():
        nomes_exemplo = ", ".join(list(config["nomes"])[:3])
        print(f"   - {editora}: {nomes_exemplo}...")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Baixa PDFs com sessão única - Suporta Frontiers, SAGE, World Scientific e Emerald"
    )

    parser.add_argument("--csv", default="artigos.csv", help="Arquivo CSV com os artigos")
    parser.add_argument("--saida", default="pdfs", help="Pasta para salvar os PDFs")
    parser.add_argument("--log", default="download_log.csv", help="Arquivo de log")
    parser.add_argument("--limite", type=int, default=0, help="Limite de artigos para teste")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay entre artigos, em segundos")
    parser.add_argument("--timeout-pdf", type=int, default=15, help="Timeout por URL de PDF, em segundos")
    parser.add_argument("--page-wait", type=int, default=12, help="Tempo máximo para esperar página ficar pronta")
    parser.add_argument("--page-load-timeout", type=int, default=20, help="Timeout bruto de carregamento do Chrome")

    args = parser.parse_args()

    caminho_csv = Path(args.csv)
    pasta_saida = Path(args.saida)
    caminho_log = Path(args.log)

    if not caminho_csv.exists():
        print(f"❌ Arquivo não encontrado: {caminho_csv}")
        return

    print("\n" + "=" * 60)
    print("📚 BAIXADOR DE ARTIGOS - VERSÃO RÁPIDA")
    print("   Suporte: Frontiers, SAGE, World Scientific, Emerald")
    print("   Academic Press / ScienceDirect foi removido desta versão")
    print("=" * 60)

    df = ler_csv_filtrado(caminho_csv, args.limite)

    if df.empty:
        print("❌ Nenhum artigo das editoras suportadas encontrado no CSV")
        imprimir_editoras_suportadas()
        return

    print(f"📄 Artigos encontrados: {len(df)}")

    logs = baixar_todos_artigos(
        df=df,
        pasta_saida=pasta_saida,
        delay=args.delay,
        timeout_pdf=args.timeout_pdf,
        page_wait=args.page_wait,
        page_load_timeout=args.page_load_timeout,
    )

    if logs:
        pd.DataFrame(logs, columns=LOG_COLUMNS).to_csv(
            caminho_log,
            index=False,
            encoding="utf-8-sig",
        )

    print("\n" + "=" * 60)
    print("📊 RESUMO FINAL")
    print("=" * 60)

    if logs:
        status_counts = pd.Series([x["status"] for x in logs]).value_counts()
        for status, count in status_counts.items():
            emoji = "✅" if status == "baixado" else "📁" if status == "ja_existia" else "❌"
            print(f"   {emoji} {status}: {count}")

        print("\n📊 Por editora:")
        df_log = pd.DataFrame(logs)
        for editora in df_log["editora_detectada"].unique():
            editora_logs = df_log[df_log["editora_detectada"] == editora]
            sucesso = len(editora_logs[editora_logs["status"] == "baixado"])
            total = len(editora_logs)
            print(f"   - {editora}: {sucesso}/{total} baixados")

    print(f"\n📁 PDFs salvos em: {pasta_saida.resolve()}")
    print(f"📄 Log salvo em: {caminho_log.resolve()}")


if __name__ == "__main__":
    main()
