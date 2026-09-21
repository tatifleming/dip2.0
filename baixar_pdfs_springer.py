#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
baixar_pdfs_springer.py

Baixa PDFs de artigos Springer Nature de forma responsável e legalmente segura.

Características:
- Lê artigos.csv
- Filtra apenas publishers Springer/Springer Nature
- Usa DOI como principal fonte de busca
- Consulta páginas oficiais em link.springer.com e nature.com
- Tenta localizar PDFs apenas em URLs oficiais
- Valida se o conteúdo baixado é realmente PDF
- Não contorna paywall, login institucional, autenticação ou restrições técnicas
- Gera log detalhado em download_log.csv
"""

from __future__ import annotations

import argparse
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# Configurações gerais
# ============================================================

DEFAULT_INPUT_CSV = "artigos.csv"
DEFAULT_OUTPUT_DIR = "pdfs_springer"
DEFAULT_LOG_CSV = "download_log.csv"

REQUEST_TIMEOUT = (10, 30)          # timeout de conexão e leitura
REQUEST_DELAY = 1.5                 # pausa entre artigos
PDF_ATTEMPT_DELAY = 0.5             # pausa entre tentativas de URLs de PDF
MAX_PDF_BYTES = 200 * 1024 * 1024   # 200 MB

USER_AGENT = (
    "SpringerNatureResponsiblePDFDownloader/2.0 "
    "(academic use; no paywall bypass; contact: seu-email@exemplo.com)"
)

ALLOWED_HOSTS = {
    "link.springer.com",
    "nature.com",
    "www.nature.com",
}

SPRINGER_NATURE_PUBLISHERS = [
    "Springer Science and Business Media Deutschland GmbH",  # 78
    "Springer",  # 64
    "Springer Science and Business Media B.V.",  # 61
    "Nature Research",  # 31
    "Springer Nature",  # 26
    "Springer Netherlands",  # 21
    "Nature Publishing Group",  # 17
    "Springer New York LLC",  # 15
    "Kluwer Academic Publishers",  # 12
    "Springer Verlag",  # 12
    "BioMed Central Ltd",  # 9
    "SPRINGER",  # 7
    "SPRINGER HEIDELBERG",  # 6
    "Springer International Publishing",  # 6
    "Springer Science and Business Media, LLC",  # 6
    "Palgrave Macmillan",  # 4
    "Springer Japan",  # 3
    "BioMed Central Ltd.",  # 2
    "NATURE PORTFOLIO",  # 2
    "Palgrave Macmillan Ltd.",  # 2
    "Springer Tokyo",  # 2
    "SpringerOpen",  # 2
    "NATURE PUBLISHING GROUP",  # 1
    "PHYSICA-VERLAG GMBH & CO",  # 1
    "SPRINGER SINGAPORE PTE LTD",  # 1
    "Springer-Verlag London Ltd",  # 1
    "SPRINGERNATURE",  # 1
    "SPRINGEROPEN",  # 1
]

# Alias mantido para preservar compatibilidade com o restante do script
# e eventuais usos externos desse nome.
SPRINGER_PUBLISHERS = SPRINGER_NATURE_PUBLISHERS

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


# ============================================================
# Estrutura para resultado de download
# ============================================================

@dataclass
class DownloadResult:
    ok: bool
    status: str
    message: str
    final_url: str = ""
    http_status: str = ""
    content_type: str = ""
    bytes_saved: int = 0
    access_category: str = ""


# ============================================================
# Normalização de texto, publisher e DOI
# ============================================================

def normalize_text(value: object) -> str:
    """
    Normaliza texto para comparação tolerante:
    - remove acentos;
    - ignora maiúsculas/minúsculas;
    - remove pontuação;
    - compacta espaços;
    - trata '&' como 'and'.
    """
    if value is None or pd.isna(value):
        return ""

    text = str(value).strip()
    text = text.replace("&", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Para tolerar casos como "Springer Nature" e "SPRINGERNATURE"
    return text.replace(" ", "")


SPRINGER_PUBLISHERS_NORMALIZED = {
    normalize_text(publisher)
    for publisher in SPRINGER_NATURE_PUBLISHERS
}


def is_springer_publisher(publisher: object) -> bool:
    """
    Verifica se o publisher pertence ao conjunto Springer Nature informado.

    A lógica é um pouco mais tolerante do que igualdade exata:
    - aceita correspondência normalizada exata;
    - aceita nomes que começam com 'springer', útil para variações como
      'Springer Nature Switzerland AG'.
    """
    normalized = normalize_text(publisher)

    if not normalized:
        return False

    if normalized in SPRINGER_PUBLISHERS_NORMALIZED:
        return True

    # Tolerância controlada para editoras associadas ao grupo Springer
    if normalized.startswith("springer"):
        return True

    return False


def clean_doi(raw_doi: object) -> str:
    """
    Extrai DOI de formatos comuns:
    - 10.1007/s00125-005-1926-9
    - DOI: 10.1007/s00125-005-1926-9
    - https://doi.org/10.1007/s00125-005-1926-9
    """
    if raw_doi is None or pd.isna(raw_doi):
        return ""

    doi = str(raw_doi).strip()
    doi = re.sub(r"(?i)^doi:\s*", "", doi)
    doi = re.sub(r"(?i)^https?://(dx\.)?doi\.org/", "", doi)
    doi = doi.strip().strip(" .;,")
    doi = doi.replace(" ", "")

    match = DOI_RE.search(doi)
    if not match:
        return ""

    return match.group(0).rstrip(".,;").lower()


def is_valid_doi(doi: str) -> bool:
    return bool(DOI_RE.fullmatch(doi or ""))


# ============================================================
# Sessão HTTP responsável
# ============================================================

def make_session() -> requests.Session:
    """
    Cria sessão requests com User-Agent, timeout nas chamadas
    e retries moderados para erros transitórios.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/pdf;q=0.8,*/*;q=0.7"
        ),
    })

    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


def allowed_official_host(url: str) -> bool:
    """Aceita apenas link.springer.com, nature.com ou www.nature.com."""
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False

    return host in ALLOWED_HOSTS


def looks_like_html_response(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    return "html" in content_type or "text/" in content_type


def looks_like_pdf_bytes(first_bytes: bytes, content_type: str) -> bool:
    """
    Valida se o conteúdo parece PDF.

    O teste usa:
    - Content-Type application/pdf;
    - assinatura binária %PDF no início do arquivo.
    """
    content_type = (content_type or "").lower()

    return (
        "application/pdf" in content_type
        or first_bytes.startswith(b"%PDF")
        or b"%PDF" in first_bytes[:1024]
    )


# ============================================================
# Classificação de acesso
# ============================================================

def classify_access_from_text(text: str) -> str:
    """
    Classifica indícios de acesso a partir do texto HTML.

    Isso não decide sozinho se pode baixar.
    A decisão final é sempre: só salva se o servidor entregar PDF real.
    """
    text = (text or "").lower()
    text = re.sub(r"\s+", " ", text)

    if "preview of subscription content" in text:
        return "subscription_preview"

    if "buy article pdf" in text or "buy now" in text or "purchase this article" in text:
        return "paid_article"

    if (
        "log in via an institution" in text
        or "institutional access" in text
        or "access provided by" in text
        or "sign in" in text
    ):
        return "institution_login_or_signin_possible"

    if (
        "open access" in text
        or "this article is open access" in text
        or "you have full access to this open access article" in text
    ):
        return "open_access"

    if "you have full access" in text:
        return "site_says_full_access"

    return "unknown"


def classify_access_from_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    return classify_access_from_text(text)


def classify_open_access_column(value: object) -> str:
    """
    Classifica a coluna Open Access do CSV, sem confiar nela como autorização.
    Ela ajuda no diagnóstico, mas o script ainda valida o PDF real.
    """
    if value is None or pd.isna(value):
        return "empty"

    text = str(value).strip().lower()

    if not text:
        return "empty"

    positive_values = {
        "yes",
        "y",
        "true",
        "1",
        "open",
        "open access",
        "oa",
    }

    negative_values = {
        "no",
        "n",
        "false",
        "0",
        "closed",
        "not open access",
    }

    if text in positive_values:
        return "csv_says_open_access"

    if text in negative_values:
        return "csv_says_not_open_access"

    if "open" in text:
        return "csv_probably_open_access"

    return "csv_unknown_value"


# ============================================================
# Localização da página oficial
# ============================================================

def official_link_from_csv(link_value: object) -> Optional[str]:
    """
    Usa a coluna Link como fallback somente se apontar para domínio oficial.
    O DOI continua sendo a fonte principal.
    """
    if link_value is None or pd.isna(link_value):
        return None

    url = str(link_value).strip()

    if not url:
        return None

    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        return None

    if allowed_official_host(url):
        return url

    return None


def locate_official_article_page(
    session: requests.Session,
    doi: str,
    link_from_csv: object = "",
) -> Tuple[Optional[str], Optional[requests.Response], Optional[str]]:
    """
    Tenta localizar a página oficial do artigo.

    Estratégia:
    1. Resolver DOI via doi.org.
    2. Tentar rotas oficiais do link.springer.com.
    3. Usar a coluna Link apenas se já for um domínio oficial permitido.
    """
    encoded_doi_path = quote(doi, safe="/")
    encoded_doi_query = quote(f"doi:{doi}", safe=":/")

    candidate_urls = [
        f"https://doi.org/{encoded_doi_path}",
        f"https://link.springer.com/article/{encoded_doi_path}",
        f"https://link.springer.com/chapter/{encoded_doi_path}",
        f"https://link.springer.com/openurl?genre=article&id={encoded_doi_query}",
        f"https://link.springer.com/openurl?genre=bookitem&id={encoded_doi_query}",
    ]

    csv_link = official_link_from_csv(link_from_csv)
    if csv_link:
        candidate_urls.append(csv_link)

    # Remove duplicatas preservando ordem
    candidate_urls = deduplicate_preserve_order(candidate_urls)

    last_error = None

    for url in candidate_urls:
        try:
            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            last_error = f"Falha de conexão ao consultar {url}: {exc}"
            continue

        final_url = response.url

        if response.status_code == 404:
            last_error = f"Página não encontrada: {final_url}"
            continue

        if response.status_code in (401, 403):
            last_error = f"Acesso não permitido à página: HTTP {response.status_code}"
            continue

        if response.status_code >= 400:
            last_error = f"Erro HTTP {response.status_code} ao consultar {final_url}"
            continue

        if not allowed_official_host(final_url):
            last_error = f"DOI resolveu para domínio não permitido: {final_url}"
            continue

        if not looks_like_html_response(response):
            last_error = f"Resposta não parece ser HTML: {final_url}"
            continue

        return final_url, response, None

    return None, None, last_error or "Página oficial não localizada."


# ============================================================
# Extração de candidatos a PDF
# ============================================================

def deduplicate_preserve_order(urls: Iterable[str]) -> List[str]:
    seen = set()
    result = []

    for url in urls:
        if not url:
            continue

        if url not in seen:
            seen.add(url)
            result.append(url)

    return result


def extract_pdf_candidates(page_url: str, html: str, doi: str) -> List[str]:
    """
    Extrai possíveis URLs de PDF a partir da página oficial.

    Fontes:
    - meta citation_pdf_url;
    - links <a> com texto/href relacionado a PDF;
    - padrão Springer /content/pdf/{doi}.pdf;
    - endpoint Springer /openurl/pdf?id=doi:{doi};
    - padrão Nature /articles/{id}.pdf.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: List[str] = []

    # 1. Metadados bibliográficos comuns
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").strip().lower()
        content = (meta.get("content") or "").strip()

        if not content:
            continue

        if name in {
            "citation_pdf_url",
            "bepress_citation_pdf_url",
            "dc.format",
        }:
            if ".pdf" in content.lower() or "citation_pdf_url" in name:
                candidates.append(urljoin(page_url, content))

    # 2. Links visíveis ou ocultos no HTML
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        text = anchor.get_text(" ", strip=True).lower()
        aria = (anchor.get("aria-label") or "").strip().lower()
        title = (anchor.get("title") or "").strip().lower()

        absolute = urljoin(page_url, href)
        href_lower = absolute.lower()

        if (
            ".pdf" in href_lower
            or "/content/pdf/" in href_lower
            or "download pdf" in text
            or text == "pdf"
            or "pdf" in text
            or "download pdf" in aria
            or "pdf" in aria
            or "download pdf" in title
            or "pdf" in title
        ):
            candidates.append(absolute)

    parsed = urlparse(page_url)
    host = parsed.netloc.lower()

    # 3. Padrão comum da Nature:
    #    https://www.nature.com/articles/s41586-... -> .../s41586-....pdf
    if host in {"nature.com", "www.nature.com"}:
        clean_path = parsed.path.rstrip("/")
        if clean_path.startswith("/articles/") and not clean_path.endswith(".pdf"):
            candidates.append(f"{parsed.scheme}://{parsed.netloc}{clean_path}.pdf")

    # 4. Padrão Springer para PDF por DOI
    encoded_doi_path_slash = quote(doi, safe="/")
    encoded_doi_path_encoded_slash = quote(doi, safe="")

    candidates.append(
        f"https://link.springer.com/content/pdf/{encoded_doi_path_slash}.pdf"
    )
    candidates.append(
        f"https://link.springer.com/content/pdf/{encoded_doi_path_encoded_slash}.pdf"
    )

    # 5. Endpoint OpenURL PDF da Springer
    encoded_doi_query = quote(f"doi:{doi}", safe=":/")
    candidates.append(
        f"https://link.springer.com/openurl/pdf?id={encoded_doi_query}"
    )

    # Mantém apenas domínios oficiais permitidos
    candidates = [
        url
        for url in candidates
        if allowed_official_host(url)
    ]

    return deduplicate_preserve_order(candidates)


# ============================================================
# Nomes de arquivos
# ============================================================

def safe_filename_from_doi_or_title(doi: str, title: object) -> str:
    """
    Gera nome de arquivo seguro.

    Preferência:
    1. DOI;
    2. título;
    3. hash.
    """
    base = doi.strip() if doi else str(title or "").strip()

    if not base:
        base = hashlib.sha1(str(title).encode("utf-8", errors="ignore")).hexdigest()

    base = base.replace("/", "_")
    base = re.sub(r'[<>:"\\|?*\x00-\x1F]', "_", base)
    base = re.sub(r"\s+", "_", base)
    base = re.sub(r"_+", "_", base)
    base = base.strip("._-")

    if not base:
        base = hashlib.sha1(str(title).encode("utf-8", errors="ignore")).hexdigest()

    base = base[:180]

    return f"{base}.pdf"


def is_existing_valid_pdf(path: Path) -> bool:
    """Verifica se um arquivo existente parece PDF."""
    if not path.exists() or not path.is_file():
        return False

    try:
        with path.open("rb") as f:
            first_bytes = f.read(1024)

        return b"%PDF" in first_bytes[:1024]
    except OSError:
        return False


def unique_output_path(output_dir: Path, filename: str) -> Path:
    """Gera caminho único para não sobrescrever arquivos."""
    path = output_dir / filename

    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix

    for i in range(2, 10000):
        candidate = output_dir / f"{stem}_{i}{suffix}"

        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Não foi possível gerar nome único para {filename}")


# ============================================================
# Download responsável
# ============================================================

def decode_small_html_sample(data: bytes) -> str:
    """Decodifica pequeno trecho HTML para diagnóstico."""
    try:
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def download_pdf_if_allowed(
    session: requests.Session,
    pdf_url: str,
    output_path: Path,
) -> DownloadResult:
    """
    Baixa PDF somente se o servidor entregar diretamente um PDF válido.

    Não faz:
    - login;
    - uso de credenciais;
    - bypass de paywall;
    - manipulação de cookies especiais;
    - extração de PDFs de páginas protegidas;
    - tentativa de contornar restrições técnicas.
    """
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")

    try:
        with session.get(
            pdf_url,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=True,
            headers={"Accept": "application/pdf,*/*;q=0.8"},
        ) as response:

            final_url = response.url
            http_status = str(response.status_code)
            content_type = response.headers.get("Content-Type", "")

            if not allowed_official_host(final_url):
                return DownloadResult(
                    ok=False,
                    status="download_not_allowed",
                    message=f"Redirecionado para domínio não permitido: {final_url}",
                    final_url=final_url,
                    http_status=http_status,
                    content_type=content_type,
                    access_category="external_or_unofficial_domain",
                )

            if response.status_code == 404:
                return DownloadResult(
                    ok=False,
                    status="pdf_unavailable",
                    message="PDF não encontrado HTTP 404.",
                    final_url=final_url,
                    http_status=http_status,
                    content_type=content_type,
                    access_category="pdf_404",
                )

            if response.status_code in (401, 403):
                return DownloadResult(
                    ok=False,
                    status="download_not_allowed",
                    message=f"Download não permitido: HTTP {response.status_code}.",
                    final_url=final_url,
                    http_status=http_status,
                    content_type=content_type,
                    access_category="http_unauthorized_or_forbidden",
                )

            if response.status_code >= 400:
                return DownloadResult(
                    ok=False,
                    status="connection_or_http_error",
                    message=f"Erro HTTP {response.status_code} ao baixar PDF.",
                    final_url=final_url,
                    http_status=http_status,
                    content_type=content_type,
                    access_category="http_error",
                )

            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                if int(content_length) > MAX_PDF_BYTES:
                    return DownloadResult(
                        ok=False,
                        status="download_not_allowed",
                        message=f"Arquivo excede limite de {MAX_PDF_BYTES} bytes.",
                        final_url=final_url,
                        http_status=http_status,
                        content_type=content_type,
                        access_category="file_too_large",
                    )

            iterator = response.iter_content(chunk_size=8192)

            try:
                first_chunk = next(iterator)
            except StopIteration:
                return DownloadResult(
                    ok=False,
                    status="pdf_unavailable",
                    message="Resposta vazia ao tentar baixar PDF.",
                    final_url=final_url,
                    http_status=http_status,
                    content_type=content_type,
                    access_category="empty_response",
                )

            if not looks_like_pdf_bytes(first_chunk, content_type):
                sample_text = decode_small_html_sample(first_chunk)
                access_category = classify_access_from_text(sample_text)

                return DownloadResult(
                    ok=False,
                    status="download_not_allowed_html_instead_of_pdf",
                    message=(
                        "A resposta não parece ser PDF. "
                        "Provavelmente é HTML de paywall, login, compra, erro "
                        "ou página intermediária."
                    ),
                    final_url=final_url,
                    http_status=http_status,
                    content_type=content_type,
                    access_category=access_category,
                )

            bytes_written = 0

            try:
                with tmp_path.open("wb") as f:
                    f.write(first_chunk)
                    bytes_written += len(first_chunk)

                    for chunk in iterator:
                        if not chunk:
                            continue

                        bytes_written += len(chunk)

                        if bytes_written > MAX_PDF_BYTES:
                            raise RuntimeError(
                                f"Arquivo excedeu limite de {MAX_PDF_BYTES} bytes."
                            )

                        f.write(chunk)

                tmp_path.replace(output_path)

            except Exception as exc:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)

                return DownloadResult(
                    ok=False,
                    status="save_error",
                    message=f"Erro ao salvar arquivo: {exc}",
                    final_url=final_url,
                    http_status=http_status,
                    content_type=content_type,
                    access_category="save_error",
                )

            return DownloadResult(
                ok=True,
                status="downloaded",
                message=f"PDF salvo com sucesso: {output_path.name}",
                final_url=final_url,
                http_status=http_status,
                content_type=content_type,
                bytes_saved=bytes_written,
                access_category="pdf_delivered",
            )

    except requests.RequestException as exc:
        return DownloadResult(
            ok=False,
            status="connection_error",
            message=f"Falha de conexão no download: {exc}",
            final_url=pdf_url,
            access_category="connection_error",
        )

    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# ============================================================
# Log
# ============================================================

LOG_COLUMNS = [
    "Row index",
    "DOI",
    "Title",
    "Publisher",
    "Open Access CSV",
    "Open Access CSV class",
    "Article URL",
    "Article access category",
    "PDF URL tried",
    "PDF final URL",
    "PDF HTTP status",
    "PDF Content-Type",
    "Status do download",
    "Nome do arquivo salvo",
    "Bytes salvos",
    "Mensagem de erro",
]


def empty_log_entry(row_index: int, row: pd.Series) -> Dict[str, str]:
    return {
        "Row index": str(row_index),
        "DOI": str(row.get("DOI", "")),
        "Title": str(row.get("Title", "")),
        "Publisher": str(row.get("Publisher", "")),
        "Open Access CSV": str(row.get("Open Access", "")),
        "Open Access CSV class": classify_open_access_column(row.get("Open Access", "")),
        "Article URL": "",
        "Article access category": "",
        "PDF URL tried": "",
        "PDF final URL": "",
        "PDF HTTP status": "",
        "PDF Content-Type": "",
        "Status do download": "",
        "Nome do arquivo salvo": "",
        "Bytes salvos": "",
        "Mensagem de erro": "",
    }


def save_log(logs: List[Dict[str, str]], log_csv: Path) -> None:
    pd.DataFrame(logs, columns=LOG_COLUMNS).to_csv(
        log_csv,
        index=False,
        encoding="utf-8-sig",
    )


# ============================================================
# Processamento de cada artigo
# ============================================================

def process_article(
    row_index: int,
    row: pd.Series,
    session: requests.Session,
    output_dir: Path,
    force_download: bool = False,
) -> Dict[str, str]:

    log = empty_log_entry(row_index, row)

    raw_doi = row.get("DOI", "")
    title = row.get("Title", "")
    publisher = row.get("Publisher", "")
    link = row.get("Link", "")

    doi = clean_doi(raw_doi)

    if not doi:
        log["Status do download"] = "doi_missing_or_invalid"
        log["Mensagem de erro"] = "DOI ausente ou inválido."
        return log

    if not is_valid_doi(doi):
        log["Status do download"] = "doi_invalid"
        log["Mensagem de erro"] = f"DOI inválido: {raw_doi}"
        return log

    log["DOI"] = doi

    filename = safe_filename_from_doi_or_title(doi, title)
    existing_path = output_dir / filename

    if not force_download and is_existing_valid_pdf(existing_path):
        log["Status do download"] = "already_downloaded"
        log["Nome do arquivo salvo"] = existing_path.name
        log["Mensagem de erro"] = "Arquivo PDF válido já existia na pasta de saída."
        return log

    page_url, page_response, page_error = locate_official_article_page(
        session=session,
        doi=doi,
        link_from_csv=link,
    )

    if not page_url or page_response is None:
        log["Status do download"] = "page_not_found_or_not_accessible"
        log["Mensagem de erro"] = page_error or "Página oficial não encontrada."
        return log

    log["Article URL"] = page_url

    article_access_category = classify_access_from_html(page_response.text)
    log["Article access category"] = article_access_category

    pdf_candidates = extract_pdf_candidates(
        page_url=page_url,
        html=page_response.text,
        doi=doi,
    )

    if not pdf_candidates:
        log["Status do download"] = "pdf_unavailable"
        log["Mensagem de erro"] = "Nenhum link ou candidato oficial de PDF encontrado."
        return log

    last_result: Optional[DownloadResult] = None
    last_pdf_url = ""

    for pdf_url in pdf_candidates:
        last_pdf_url = pdf_url

        output_path = unique_output_path(output_dir, filename)

        result = download_pdf_if_allowed(
            session=session,
            pdf_url=pdf_url,
            output_path=output_path,
        )

        last_result = result

        if result.ok:
            log["PDF URL tried"] = pdf_url
            log["PDF final URL"] = result.final_url
            log["PDF HTTP status"] = result.http_status
            log["PDF Content-Type"] = result.content_type
            log["Status do download"] = result.status
            log["Nome do arquivo salvo"] = output_path.name
            log["Bytes salvos"] = str(result.bytes_saved)
            log["Mensagem de erro"] = ""
            return log

        time.sleep(PDF_ATTEMPT_DELAY)

    if last_result is not None:
        log["PDF URL tried"] = last_pdf_url
        log["PDF final URL"] = last_result.final_url
        log["PDF HTTP status"] = last_result.http_status
        log["PDF Content-Type"] = last_result.content_type
        log["Status do download"] = last_result.status

        combined_category = last_result.access_category or article_access_category

        log["Mensagem de erro"] = (
            f"{last_result.message} "
            f"Categoria detectada: {combined_category}."
        )
    else:
        log["Status do download"] = "pdf_unavailable"
        log["Mensagem de erro"] = "Nenhuma tentativa de PDF foi executada."

    return log


# ============================================================
# Programa principal
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Baixa PDFs Springer Nature de forma responsável a partir de artigos.csv."
        )
    )

    parser.add_argument(
        "--csv",
        default=DEFAULT_INPUT_CSV,
        help=f"Arquivo CSV de entrada. Padrão: {DEFAULT_INPUT_CSV}",
    )

    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Pasta de saída dos PDFs. Padrão: {DEFAULT_OUTPUT_DIR}",
    )

    parser.add_argument(
        "--log",
        default=DEFAULT_LOG_CSV,
        help=f"Arquivo CSV de log. Padrão: {DEFAULT_LOG_CSV}",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"Pausa entre artigos, em segundos. Padrão: {REQUEST_DELAY}",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Força baixar novamente mesmo se o PDF já existir. "
            "Por padrão, PDFs existentes e válidos são ignorados."
        ),
    )

    args = parser.parse_args()

    input_csv = Path(args.csv)
    output_dir = Path(args.out)
    log_csv = Path(args.log)

    if not input_csv.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {input_csv}")

    output_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(input_csv, dtype=str).fillna("")

    required_columns = {
        "Authors",
        "Title",
        "Year",
        "DOI",
        "Link",
        "Cited by",
        "Abstract",
        "Document Type",
        "Publisher",
        "Open Access",
    }

    missing_columns = required_columns - set(df.columns)

    if missing_columns:
        raise ValueError(
            "O CSV não possui as colunas obrigatórias: "
            + ", ".join(sorted(missing_columns))
        )

    springer_mask = df["Publisher"].apply(is_springer_publisher)
    springer_df = df[springer_mask].copy()

    print(f"Total de registros no CSV: {len(df)}")
    print(f"Registros dos publishers Springer Nature filtrados: {len(springer_df)}")
    print(f"Pasta de saída: {output_dir.resolve()}")
    print(f"Log: {log_csv.resolve()}")
    print(f"Forçar novo download: {'sim' if args.force else 'não'}")

    session = make_session()
    logs: List[Dict[str, str]] = []

    try:
        for row_index, row in springer_df.iterrows():
            title_preview = str(row.get("Title", ""))[:90]
            doi_preview = clean_doi(row.get("DOI", "")) or "-"

            print("\n" + "-" * 80)
            print(f"Linha: {row_index}")
            print(f"DOI: {doi_preview}")
            print(f"Título: {title_preview}")

            log_entry = process_article(
                row_index=row_index,
                row=row,
                session=session,
                output_dir=output_dir,
                force_download=args.force,
            )

            logs.append(log_entry)

            print(f"Status: {log_entry['Status do download']}")
            print(f"Categoria página: {log_entry['Article access category'] or '-'}")
            print(f"HTTP PDF: {log_entry['PDF HTTP status'] or '-'}")
            print(f"Content-Type PDF: {log_entry['PDF Content-Type'] or '-'}")
            print(f"Arquivo: {log_entry['Nome do arquivo salvo'] or '-'}")

            if log_entry["Mensagem de erro"]:
                print(f"Mensagem: {log_entry['Mensagem de erro']}")

            # Salva log progressivamente para não perder resultado se interromper.
            save_log(logs, log_csv)

            time.sleep(max(0, args.delay))

    finally:
        save_log(logs, log_csv)

    print("\nConcluído.")
    print(f"Log salvo em: {log_csv.resolve()}")


if __name__ == "__main__":
    main()