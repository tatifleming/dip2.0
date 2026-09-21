#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Baixa PDFs de artigos da Elsevier e de seus imprints/afiliadas quando o acesso é permitido.

Entrada: artigos.csv com colunas:
Authors, Title, Year, DOI, Link, Cited by, Abstract, Document Type, Publisher, Open Access

Saídas:
- downloads_elsevier/*.pdf
- resultado_downloads.csv

O script usa DOI e a API oficial da Elsevier/ScienceDirect quando houver
credenciais autorizadas. Não tenta burlar paywalls, autenticação, JavaScript,
cookies de navegador, proxy institucional ou qualquer mecanismo de proteção.
"""

from __future__ import annotations

import csv
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import quote, urlparse

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


INPUT_CSV = "artigos.csv"
OUTPUT_DIR = Path("downloads_elsevier")
LOG_CSV = "resultado_downloads.csv"

# Se True, o script consulta o LOG_CSV existente antes de sobrescrevê-lo e
# reaproveita os downloads bem-sucedidos que ainda existem no disco.
RESUME_FROM_EXISTING_LOG = True

# KeAi é uma joint venture cofundada pela Elsevier. Deixe True para tratar como
# parte do guarda-chuva Elsevier/afiliadas neste lote.
INCLUDE_ELSEVIER_AFFILIATES = True

# Defina estas variáveis de ambiente somente se você tiver acesso autorizado.
ELSEVIER_API_KEY = os.getenv("ELSEVIER_API_KEY", "").strip()
ELSEVIER_INSTTOKEN = os.getenv("ELSEVIER_INSTTOKEN", "").strip()
ELSEVIER_ACCESS_TOKEN = os.getenv("ELSEVIER_ACCESS_TOKEN", "").strip()

REQUEST_DELAY_SECONDS = 0.35
MIN_PDF_BYTES = 1024

# Nomes encontrados no arquivo artigos.csv e no log resultado_downloads.csv.
# A normalização feita abaixo ignora maiúsculas/minúsculas e diferenças simples
# de ponto/espaço, mas mantemos as variantes para facilitar auditoria.
ELSEVIER_CORE_PUBLISHERS_RAW = {
    "Elsevier",
    "ELSEVIER",
    "Elsevier B.V.",
    "Elsevier B.V",
    "Elsevier BV",
    "Elsevier GmbH",
    "Elsevier Inc.",
    "Elsevier Ltd",
    "ELSEVIER SCI LTD",
    "ELSEVIER SCIENCE BV",
    "ELSEVIER SCIENCE INC",
    "ELSEVIER SCIENCE SA",
}

ELSEVIER_IMPRINT_PUBLISHERS_RAW = {
    "Academic Press",
    "Academic Press Inc.",
    "ACADEMIC PRESS LTD- ELSEVIER SCIENCE LTD",
    "Cell Press",
    "CELL PRESS",
    "PERGAMON-ELSEVIER SCIENCE LTD",
}

ELSEVIER_AFFILIATE_PUBLISHERS_RAW = {
    "KeAi Communications Co.",
    "KeAi Publishing Communications Ltd.",
    "KEAI PUBLISHING LTD",
}

OFFICIAL_ELSEVIER_DOMAINS = {
    "sciencedirect.com",
    "www.sciencedirect.com",
    "api.elsevier.com",
    "linkinghub.elsevier.com",
    "elsevier.com",
    "www.elsevier.com",
    # KeAi é mantida como afiliada/joint venture; incluímos o domínio oficial
    # apenas para fallback de artigos OA quando o link retornar PDF diretamente.
    "keaipublishing.com",
    "www.keaipublishing.com",
}


def normalize_publisher(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip().casefold()
    text = re.sub(r"[.\s]+", " ", text)
    text = text.replace(" - ", "-")
    text = re.sub(r"\s*-\s*", "-", text)
    return text.strip()


ELSEVIER_CORE_PUBLISHERS_NORMALIZED = {
    normalize_publisher(p) for p in ELSEVIER_CORE_PUBLISHERS_RAW
}
ELSEVIER_IMPRINT_PUBLISHERS_NORMALIZED = {
    normalize_publisher(p) for p in ELSEVIER_IMPRINT_PUBLISHERS_RAW
}
ELSEVIER_AFFILIATE_PUBLISHERS_NORMALIZED = {
    normalize_publisher(p) for p in ELSEVIER_AFFILIATE_PUBLISHERS_RAW
}


def is_elsevier_publisher(value: object) -> bool:
    publisher = normalize_publisher(value)
    if not publisher:
        return False

    # Variantes corporativas da Elsevier, inclusive nomes históricos como
    # "Elsevier Science" e grafias sem ponto como "Elsevier BV".
    if publisher in ELSEVIER_CORE_PUBLISHERS_NORMALIZED:
        return True
    if "elsevier" in publisher:
        return True

    # Imprints/editoras do grupo encontrados no CSV.
    if publisher in ELSEVIER_IMPRINT_PUBLISHERS_NORMALIZED:
        return True
    if publisher in {"academic press", "academic press inc", "cell press"}:
        return True
    if publisher.startswith("pergamon"):
        return True

    # Afiliadas/joint ventures. KeAi entra aqui para completar o lote pedido;
    # desative INCLUDE_ELSEVIER_AFFILIATES se quiser processar só o núcleo/imprints.
    if INCLUDE_ELSEVIER_AFFILIATES and publisher in ELSEVIER_AFFILIATE_PUBLISHERS_NORMALIZED:
        return True

    return False


def clean_doi(value: object) -> str:
    if pd.isna(value):
        return ""
    doi = str(value).strip().replace("\u200b", "")
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    return doi.strip().strip(".")


def is_valid_doi(doi: str) -> bool:
    return bool(re.match(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", doi, flags=re.I))


def is_open_access(value: object) -> bool:
    if pd.isna(value):
        return False
    text = str(value).strip().casefold()
    return text in {
        "yes", "y", "true", "t", "1", "sim", "s",
        "open", "open access", "oa", "gold", "hybrid", "bronze",
    }


def safe_filename(text: str, max_length: int = 160) -> str:
    text = str(text or "").strip()
    text = re.sub(r"^https?://", "", text, flags=re.I)
    text = text.replace("/", "_").replace("\\", "_")
    text = re.sub(r'[<>:"|?*\n\r\t]', "_", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip(" ._")
    return (text or "artigo_sem_nome")[:max_length]


def filename_for_article(title: str, doi: str) -> str:
    return safe_filename(doi if doi else title) + ".pdf"


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "elsevier-pdf-downloader/1.1 (+authorized-use-only)",
    })
    return session


def elsevier_headers(accept: str = "application/pdf") -> Dict[str, str]:
    headers = {"Accept": accept}
    if ELSEVIER_API_KEY:
        headers["X-ELS-APIKey"] = ELSEVIER_API_KEY
    if ELSEVIER_INSTTOKEN:
        headers["X-ELS-Insttoken"] = ELSEVIER_INSTTOKEN
    if ELSEVIER_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {ELSEVIER_ACCESS_TOKEN}"
    return headers


def response_failure_reason(response: requests.Response) -> str:
    status = response.status_code
    els_status = response.headers.get("X-ELS-Status", "").strip()
    content_type = response.headers.get("Content-Type", "").strip()

    if status in (401, 403):
        return "Acesso não autorizado ou sem entitlement para o PDF"
    if status == 404:
        return "Artigo não encontrado pela fonte consultada"
    if status == 429:
        return "Limite de requisições atingido ou throttling"
    if 500 <= status <= 599:
        return "Erro temporário no servidor da fonte consultada"

    details = []
    if els_status:
        details.append(f"X-ELS-Status={els_status}")
    if content_type:
        details.append(f"Content-Type={content_type}")
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"Resposta HTTP {status}{suffix}"


def save_pdf_response(response: requests.Response, output_path: Path) -> Tuple[bool, str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    bytes_written = 0

    try:
        with temp_path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    file.write(chunk)
                    bytes_written += len(chunk)

        if bytes_written < MIN_PDF_BYTES:
            temp_path.unlink(missing_ok=True)
            return False, f"Resposta muito pequena para PDF ({bytes_written} bytes)"

        with temp_path.open("rb") as file:
            if file.read(5) != b"%PDF-":
                temp_path.unlink(missing_ok=True)
                ctype = response.headers.get("Content-Type", "desconhecido")
                return False, f"Resposta não parece PDF válido; Content-Type={ctype}"

        temp_path.replace(output_path)
        return True, "PDF salvo com sucesso"

    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        return False, f"Erro ao salvar arquivo: {exc}"


def valid_existing_pdf(path_value: object) -> Optional[Path]:
    path_text = str(path_value or "").strip()
    if not path_text:
        return None

    path = Path(path_text).expanduser()
    try:
        if not path.exists() or path.stat().st_size < MIN_PDF_BYTES:
            return None
        with path.open("rb") as file:
            if file.read(5) != b"%PDF-":
                return None
        return path
    except OSError:
        return None


def load_existing_successful_downloads(log_csv: str) -> Dict[str, Path]:
    """Retorna DOI normalizado -> arquivo PDF válido, usando log anterior."""
    successful: Dict[str, Path] = {}
    log_path = Path(log_csv)
    if not RESUME_FROM_EXISTING_LOG or not log_path.exists():
        return successful

    try:
        previous = pd.read_csv(log_path, dtype=str, keep_default_na=False)
    except (OSError, pd.errors.ParserError):
        return successful

    required = {"doi", "status", "caminho_arquivo"}
    if not required.issubset(previous.columns):
        return successful

    for _, row in previous.iterrows():
        status = str(row.get("status", "")).strip().casefold()
        if status not in {"baixado", "ja_existia", "já_existia"}:
            continue

        doi = clean_doi(row.get("doi", ""))
        if not doi:
            continue

        existing_pdf = valid_existing_pdf(row.get("caminho_arquivo", ""))
        if existing_pdf:
            successful[doi.casefold()] = existing_pdf

    return successful


def download_from_elsevier_api(
    session: requests.Session,
    doi: str,
    output_path: Path,
) -> Tuple[str, str]:
    """
    Usa a API oficial Article Retrieval por DOI.
    A própria API decide, por credenciais/entitlement, se o PDF pode ser entregue.
    """
    if not ELSEVIER_API_KEY and not ELSEVIER_ACCESS_TOKEN:
        return "falha", "Credenciais Elsevier ausentes: defina ELSEVIER_API_KEY ou ELSEVIER_ACCESS_TOKEN"

    encoded_doi = quote(doi, safe="/")
    url = f"https://api.elsevier.com/content/article/doi/{encoded_doi}"
    params = {"httpAccept": "application/pdf"}

    try:
        response = session.get(
            url,
            params=params,
            headers=elsevier_headers("application/pdf"),
            timeout=60,
            stream=True,
        )
    except requests.RequestException as exc:
        return "falha", f"Erro de conexão na API Elsevier: {exc}"

    if response.status_code != 200:
        return "falha", response_failure_reason(response)

    ok, reason = save_pdf_response(response, output_path)
    return ("baixado", reason) if ok else ("falha", reason)


def is_official_elsevier_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.casefold()
    except ValueError:
        return False
    return bool(host) and (
        host in OFFICIAL_ELSEVIER_DOMAINS
        or any(host.endswith("." + d) for d in OFFICIAL_ELSEVIER_DOMAINS)
    )


def download_from_public_official_link(
    session: requests.Session,
    link: str,
    output_path: Path,
) -> Tuple[str, str]:
    """
    Fallback conservador: só baixa se Link for oficial e retornar PDF diretamente.
    Não extrai PDF escondido de HTML e não reutiliza sessão de navegador.
    """
    link = str(link or "").strip()

    if not re.match(r"^https?://", link, flags=re.I):
        return "falha", "Link inválido ou ausente"
    if not is_official_elsevier_url(link):
        return "falha", "Link não pertence a domínio oficial da Elsevier/ScienceDirect/KeAi"

    try:
        response = session.get(
            link,
            headers={"Accept": "application/pdf"},
            timeout=60,
            stream=True,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return "falha", f"Erro de conexão no link público: {exc}"

    if response.status_code != 200:
        return "falha", response_failure_reason(response)

    content_type = response.headers.get("Content-Type", "").casefold()
    if "pdf" not in content_type and "octet-stream" not in content_type:
        return "falha", f"Link oficial não retornou PDF; Content-Type={content_type or 'desconhecido'}"

    ok, reason = save_pdf_response(response, output_path)
    return ("baixado", reason) if ok else ("falha", reason)


def log_row(
    title: str,
    doi: str,
    publisher: str,
    status: str,
    file_path: Optional[Path],
    reason: str,
) -> Dict[str, str]:
    return {
        "titulo": title,
        "doi": doi,
        "publisher": publisher,
        "status": status,
        "caminho_arquivo": str(file_path) if file_path else "",
        "motivo": reason,
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        df = pd.read_csv(INPUT_CSV, dtype=str, keep_default_na=False)
    except FileNotFoundError:
        raise SystemExit(f"Arquivo não encontrado: {INPUT_CSV}")
    except pd.errors.ParserError as exc:
        raise SystemExit(f"Erro ao ler CSV: {exc}")

    required = {"Title", "DOI", "Publisher", "Open Access"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit("Colunas obrigatórias ausentes: " + ", ".join(sorted(missing)))

    elsevier_df = df[df["Publisher"].apply(is_elsevier_publisher)].copy()
    successful_previous_downloads = load_existing_successful_downloads(LOG_CSV)
    session = create_session()
    results = []

    print(f"Total de registros no CSV: {len(df)}")
    print(f"Registros Elsevier/imprints/afiliadas filtrados: {len(elsevier_df)}")
    print(f"Downloads bem-sucedidos reaproveitáveis no log anterior: {len(successful_previous_downloads)}")
    print(f"Pasta de saída: {OUTPUT_DIR.resolve()}")
    print("\nPublishers incluídos neste lote:")
    publisher_counts = elsevier_df["Publisher"].value_counts(dropna=False)
    for publisher, count in publisher_counts.items():
        print(f"  - {publisher or '(sem publisher)'}: {count}")

    for index, row in elsevier_df.iterrows():
        title = str(row.get("Title", "")).strip()
        publisher = str(row.get("Publisher", "")).strip()
        doi = clean_doi(row.get("DOI", ""))
        link = str(row.get("Link", "")).strip() if "Link" in row else ""
        oa = is_open_access(row.get("Open Access", ""))

        print(f"\n[{index}] Processando: {title[:100] or '(sem título)'}")

        if not doi:
            reason = "DOI ausente"
            results.append(log_row(title, doi, publisher, "falha", None, reason))
            print("  Falha:", reason)
            continue

        if not is_valid_doi(doi):
            reason = "DOI inválido"
            results.append(log_row(title, doi, publisher, "falha", None, reason))
            print("  Falha:", reason)
            continue

        output_file = OUTPUT_DIR / filename_for_article(title, doi)

        if output_file.exists() and output_file.stat().st_size >= MIN_PDF_BYTES:
            results.append(log_row(title, doi, publisher, "ja_existia", output_file, "Arquivo já existia"))
            print("  Já existia:", output_file)
            continue

        previous_file = successful_previous_downloads.get(doi.casefold())
        if previous_file:
            results.append(log_row(title, doi, publisher, "ja_existia", previous_file, "Arquivo já constava no log anterior"))
            print("  Já constava no log anterior:", previous_file)
            continue

        status, reason = download_from_elsevier_api(session, doi, output_file)

        if status == "baixado":
            results.append(log_row(title, doi, publisher, status, output_file, reason))
            print("  Baixado via API Elsevier:", output_file)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        api_failure = reason
        print("  API Elsevier não baixou:", api_failure)

        if oa:
            status, reason = download_from_public_official_link(session, link, output_file)
            if status == "baixado":
                results.append(log_row(title, doi, publisher, status, output_file, reason))
                print("  Baixado via link público oficial:", output_file)
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            final_reason = f"API Elsevier: {api_failure}; fallback Link oficial/OA: {reason}"
        else:
            final_reason = (
                f"API Elsevier: {api_failure}; "
                "fallback por Link não tentado porque Open Access não indica acesso público"
            )

        results.append(log_row(title, doi, publisher, "falha", None, final_reason))
        print("  Falha:", final_reason)
        time.sleep(REQUEST_DELAY_SECONDS)

    with open(LOG_CSV, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["titulo", "doi", "publisher", "status", "caminho_arquivo", "motivo"],
        )
        writer.writeheader()
        writer.writerows(results)

    print(f"\nLog salvo em: {Path(LOG_CSV).resolve()}")


if __name__ == "__main__":
    main()
