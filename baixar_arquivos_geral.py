#!/usr/bin/env python3
"""
baixar_arquivos_geral.py

Solução robusta e pronta para produção para baixar PDFs de artigos acadêmicos
em lote a partir de um arquivo .xlsx ou .csv.

Principais recursos:
- leitura de XLSX/CSV com pandas
- validação e normalização de colunas
- resolução de DOI e parsing HTML para localizar PDFs
- confirmação de PDF por Content-Type, extensão e assinatura do arquivo
- organização dos downloads em output/Publisher/Year/
- nomes de arquivos seguros para Windows
- deduplicação por DOI, URL final e nome do arquivo
- retomada por arquivo de estado append-only (JSON Lines)
- logs em arquivo e console
- barra de progresso
- relatório final em CSV e XLSX
- paralelismo controlado com ThreadPoolExecutor

Observações importantes:
- O script NÃO tenta contornar paywall, CAPTCHA, login institucional,
  bloqueios anti-bot ou qualquer mecanismo de restrição.
- Se o PDF não estiver disponível diretamente, registra o motivo e segue.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, unquote, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests import Response, Session
from tqdm import tqdm


APP_NAME = "baixar_arquivos_geral"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36 "
    f"{APP_NAME}/1.0"
)
PDF_MAGIC = b"%PDF-"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
WILEY_HOST = "onlinelibrary.wiley.com"
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
EXPECTED_COLUMNS = {
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
REQUIRED_COLUMNS = {
    "Authors",
    "Title",
    "Year",
    "DOI",
    "Link",
    "Publisher",
    "Open Access",
}
COLUMN_ALIASES = {
    "authors": "Authors",
    "title": "Title",
    "year": "Year",
    "doi": "DOI",
    "link": "Link",
    "citedby": "Cited by",
    "abstract": "Abstract",
    "documenttype": "Document Type",
    "publisher": "Publisher",
    "openaccess": "Open Access",
    "open_access": "Open Access",
}
TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
HTML_CONTENT_HINTS = ("text/html", "application/xhtml+xml")
PDF_CONTENT_HINTS = (
    "application/pdf",
    "application/x-pdf",
    "application/acrobat",
    "applications/vnd.pdf",
    "text/pdf",
)
DIRECT_PDF_PATTERNS = (
    ".pdf",
    "/pdf",
    "/epdf",
    "/pdfdirect",
    "downloadpdf",
    "pdfdownload",
    "fulltextpdf",
    "article-pdf",
    "?pdf=",
    "&pdf=",
    "download=true",
)


class DownloadError(Exception):
    """Erro conhecido do fluxo de download."""


@dataclass
class Config:
    input_path: Path
    output_dir: Path
    workers: int = 5
    timeout: int = 20
    user_agent: str = DEFAULT_USER_AGENT
    retries: int = 3
    backoff_base: float = 1.5
    chunk_size: int = 1024 * 64
    max_filename_length: int = 140
    verify_ssl: bool = True
    report_format: str = "both"  # csv, xlsx, both
    log_level: str = "INFO"

    @property
    def state_dir(self) -> Path:
        return self.output_dir / "_state"

    @property
    def logs_dir(self) -> Path:
        return self.output_dir / "_logs"

    @property
    def reports_dir(self) -> Path:
        return self.output_dir / "_reports"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.jsonl"

    @property
    def run_id(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_text(value: Any) -> str:
    """Converte valores vazios/NaN em string vazia e remove espaços extras."""
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def canonicalize_column_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", name.lower().strip())
    return COLUMN_ALIASES.get(normalized, name.strip())


def sanitize_filename(name: str, max_length: int = 140, replacement: str = "_") -> str:
    """
    Gera um nome seguro para Windows/Linux/macOS.

    - remove caracteres inválidos
    - remove caracteres de controle
    - colapsa espaços e separadores repetidos
    - evita nomes reservados do Windows
    - limita comprimento preservando extensão, quando houver
    """
    if not name:
        return "untitled"

    name = unquote(str(name)).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', replacement, name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(rf"{re.escape(replacement)}+", replacement, name)
    name = name.strip(" ._-")
    if not name:
        name = "untitled"

    stem, ext = os.path.splitext(name)
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"

    if ext and len(stem) + len(ext) > max_length:
        stem = stem[: max(1, max_length - len(ext))].rstrip(" ._-")
    elif not ext and len(name) > max_length:
        stem = stem[:max_length].rstrip(" ._-")

    safe = f"{stem}{ext}"
    return safe or "untitled"


def safe_path_component(value: str, default: str, max_length: int = 80) -> str:
    text = sanitize_filename(normalize_text(value), max_length=max_length)
    return text or default


def normalize_doi(doi: Any) -> str:
    text = normalize_text(doi)
    if not text:
        return ""
    text = text.replace("https://doi.org/", "").replace("http://doi.org/", "")
    text = text.replace("doi:", "")
    return text.strip().lower().rstrip("/.")


def looks_like_url(value: str) -> bool:
    if not value:
        return False
    return value.lower().startswith(("http://", "https://"))


def looks_like_direct_pdf_url(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return any(pattern in lowered for pattern in DIRECT_PDF_PATTERNS)


def is_truthy_open_access(value: str) -> bool:
    value = normalize_text(value).lower()
    return value in {"1", "true", "yes", "y", "sim", "s", "open", "open access"}


def first_author(authors: str) -> str:
    authors = normalize_text(authors)
    if not authors:
        return "UnknownAuthor"
    first = re.split(r";|,| and | & ", authors, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    first = re.sub(r"\s+", " ", first)
    return first or "UnknownAuthor"


def build_output_filename(row: Dict[str, Any], max_length: int) -> str:
    year = normalize_text(row.get("Year")) or "UnknownYear"
    author = first_author(row.get("Authors", ""))
    title = normalize_text(row.get("Title")) or "Untitled"
    base = f"{year} - {author} - {title}.pdf"
    return sanitize_filename(base, max_length=max_length)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def file_starts_with_pdf_magic(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == PDF_MAGIC
    except OSError:
        return False


def normalize_headers(df: pd.DataFrame) -> pd.DataFrame:
    renamed = {column: canonicalize_column_name(str(column)) for column in df.columns}
    return df.rename(columns=renamed)


def read_input_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return pd.read_excel(path, engine="openpyxl")
    if suffix == ".csv":
        encodings = ["utf-8-sig", "utf-8", "latin-1", "cp1252"]
        last_error: Optional[Exception] = None
        for encoding in encodings:
            try:
                return pd.read_csv(path, encoding=encoding)
            except UnicodeDecodeError as exc:
                last_error = exc
        raise ValueError(f"Não foi possível ler o CSV com encodings comuns: {last_error}")
    raise ValueError("Formato de entrada não suportado. Use .xlsx ou .csv")


class ThreadLocalSessionFactory:
    """Cria uma requests.Session por thread para reduzir overhead e manter isolamento."""

    def __init__(self, user_agent: str, verify_ssl: bool = True) -> None:
        self._local = threading.local()
        self.user_agent = user_agent
        self.verify_ssl = verify_ssl

    def get(self) -> Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/pdf,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                    "Connection": "keep-alive",
                }
            )
            session.verify = self.verify_ssl
            self._local.session = session
        return session


class StateStore:
    """
    Estado append-only em JSON Lines.

    Vantagens:
    - seguro para retomada
    - grava incremental
    - tolera quedas durante execução
    """

    def __init__(self, path: Path, logger: logging.Logger) -> None:
        self.path = path
        self.logger = logger
        self.lock = threading.Lock()
        self.records_by_row_id: Dict[int, Dict[str, Any]] = {}
        self.success_dois: Dict[str, Dict[str, Any]] = {}
        self.success_final_urls: Dict[str, Dict[str, Any]] = {}
        self.success_saved_paths: Dict[str, Dict[str, Any]] = {}
        self.successful_row_ids: set[int] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        loaded = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    self.logger.warning("Linha inválida no arquivo de estado ignorada.")
                    continue
                row_id = int(record["row_id"])
                self.records_by_row_id[row_id] = record
                self._index_success_record(record)
                loaded += 1
        self.logger.info("Estado carregado: %s registros lidos de %s", loaded, self.path)

    def _index_success_record(self, record: Dict[str, Any]) -> None:
        if record.get("status") not in {"success", "success_exists"}:
            return
        self.successful_row_ids.add(int(record["row_id"]))
        doi = normalize_doi(record.get("doi"))
        if doi:
            self.success_dois[doi] = record
        final_url = normalize_text(record.get("final_url"))
        if final_url:
            self.success_final_urls[final_url] = record
        saved_path = normalize_text(record.get("saved_path"))
        if saved_path:
            self.success_saved_paths[saved_path] = record

    def append(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            row_id = int(record["row_id"])
            current = self.records_by_row_id.get(row_id)
            preserve_success = (
                current
                and current.get("status") in {"success", "success_exists"}
                and record.get("status") == "skipped_already_done"
            )
            if not preserve_success:
                self.records_by_row_id[row_id] = record
            self._index_success_record(record)

    def get_row(self, row_id: int) -> Optional[Dict[str, Any]]:
        return self.records_by_row_id.get(row_id)

    def already_succeeded_row(self, row_id: int) -> bool:
        return row_id in self.successful_row_ids


class DuplicateRegistry:
    """Índices thread-safe para evitar redownloads e colisões."""

    def __init__(self, state: StateStore) -> None:
        self.lock = threading.Lock()
        self.success_dois = set(state.success_dois.keys())
        self.success_final_urls = set(state.success_final_urls.keys())
        self.success_saved_paths = set(state.success_saved_paths.keys())
        self.in_progress_urls: set[str] = set()

    def detect_duplicate(self, doi: str, final_url: str, saved_path: str) -> Optional[Tuple[str, str]]:
        with self.lock:
            if doi and doi in self.success_dois:
                return "duplicate_doi", f"DOI já concluído anteriormente: {doi}"
            if final_url and final_url in self.success_final_urls:
                return "duplicate_final_url", f"URL final já concluída anteriormente: {final_url}"
            if saved_path and saved_path in self.success_saved_paths:
                return "duplicate_filename", f"Arquivo já salvo anteriormente: {saved_path}"
        return None

    def reserve_final_url(self, final_url: str) -> bool:
        with self.lock:
            if final_url in self.in_progress_urls or final_url in self.success_final_urls:
                return False
            self.in_progress_urls.add(final_url)
            return True

    def release_final_url(self, final_url: str) -> None:
        with self.lock:
            self.in_progress_urls.discard(final_url)

    def mark_success(self, doi: str, final_url: str, saved_path: str) -> None:
        with self.lock:
            if doi:
                self.success_dois.add(doi)
            if final_url:
                self.success_final_urls.add(final_url)
                self.in_progress_urls.discard(final_url)
            if saved_path:
                self.success_saved_paths.add(saved_path)


@dataclass
class SourceCandidate:
    url: str
    source_type: str
    source_label: str
    priority: int


@dataclass
class ProcessResult:
    row_id: int
    title: str
    doi: str
    source_url: str
    final_url: str
    status: str
    saved_path: str
    error_message: str
    publisher: str = ""
    year: str = ""
    started_at: str = ""
    finished_at: str = ""
    http_status: str = ""
    content_type: str = ""
    source_type: str = ""

    def to_record(self) -> Dict[str, Any]:
        return {
            "row_id": self.row_id,
            "title": self.title,
            "doi": self.doi,
            "source_url": self.source_url,
            "final_url": self.final_url,
            "status": self.status,
            "saved_path": self.saved_path,
            "error_message": self.error_message,
            "publisher": self.publisher,
            "year": self.year,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "source_type": self.source_type,
        }


class ArticleDownloader:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.output_dir = config.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        self.config.reports_dir.mkdir(parents=True, exist_ok=True)
        self.logger = self._setup_logger()
        self.session_factory = ThreadLocalSessionFactory(config.user_agent, config.verify_ssl)
        self.state = StateStore(config.state_path, self.logger)
        self.duplicates = DuplicateRegistry(self.state)
        self.run_id = config.run_id
        self.results_lock = threading.Lock()
        self.results: List[Dict[str, Any]] = []
        self.input_duplicate_map: Dict[int, Tuple[str, int]] = {}

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"{APP_NAME}.{id(self)}")
        logger.setLevel(getattr(logging, self.config.log_level.upper(), logging.INFO))
        logger.propagate = False
        logger.handlers.clear()

        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        log_path = self.config.logs_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        return logger

    def load_dataframe(self) -> pd.DataFrame:
        self.logger.info("Lendo arquivo de entrada: %s", self.config.input_path)
        df = read_input_file(self.config.input_path)
        df = normalize_headers(df)

        missing_required = [col for col in REQUIRED_COLUMNS if col not in df.columns]
        if missing_required:
            raise ValueError(
                "Colunas obrigatórias ausentes após normalização: "
                + ", ".join(sorted(missing_required))
            )

        missing_optional = [col for col in EXPECTED_COLUMNS if col not in df.columns]
        if missing_optional:
            self.logger.warning(
                "Colunas esperadas ausentes; serão preenchidas com vazio: %s",
                ", ".join(sorted(missing_optional)),
            )
            for column in missing_optional:
                df[column] = ""

        for column in EXPECTED_COLUMNS:
            if column not in df.columns:
                df[column] = ""

        for column in df.columns:
            df[column] = df[column].apply(normalize_text)

        df.insert(0, "row_id", range(1, len(df) + 1))
        self._prepare_input_duplicates(df)
        self.logger.info("Total de registros carregados: %s", len(df))
        return df

    def _prepare_input_duplicates(self, df: pd.DataFrame) -> None:
        seen_doi: Dict[str, int] = {}
        seen_name: Dict[str, int] = {}
        duplicate_map: Dict[int, Tuple[str, int]] = {}

        for _, row in df.iterrows():
            row_id = int(row["row_id"])
            doi = normalize_doi(row.get("DOI"))
            filename = build_output_filename(row.to_dict(), self.config.max_filename_length)

            if doi:
                if doi in seen_doi:
                    duplicate_map[row_id] = (f"duplicate_input_doi:{doi}", seen_doi[doi])
                    continue
                seen_doi[doi] = row_id

            if filename in seen_name:
                duplicate_map[row_id] = (f"duplicate_input_filename:{filename}", seen_name[filename])
                continue
            seen_name[filename] = row_id

        self.input_duplicate_map = duplicate_map
        if duplicate_map:
            self.logger.info("Duplicidades detectadas na entrada: %s", len(duplicate_map))

    def request_with_retry(
        self,
        method: str,
        url: str,
        *,
        timeout: int,
        stream: bool = False,
        allow_redirects: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> Response:
        session = self.session_factory.get()
        last_error: Optional[Exception] = None
        for attempt in range(1, self.config.retries + 2):
            try:
                response = session.request(
                    method=method,
                    url=url,
                    timeout=(min(10, timeout), timeout),
                    stream=stream,
                    allow_redirects=allow_redirects,
                    headers=headers,
                )
                if response.status_code in TRANSIENT_STATUS_CODES:
                    raise DownloadError(f"HTTP temporário {response.status_code} em {url}")
                return response
            except (requests.RequestException, DownloadError) as exc:
                last_error = exc
                if attempt > self.config.retries:
                    break
                sleep_seconds = self.config.backoff_base ** attempt
                self.logger.debug(
                    "Tentativa %s falhou para %s %s: %s. Novo retry em %.1fs",
                    attempt,
                    method,
                    url,
                    exc,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
        raise DownloadError(f"Falha ao acessar {url}: {last_error}")

    def extract_source_candidates(self, row: Dict[str, Any]) -> List[SourceCandidate]:
        candidates: List[SourceCandidate] = []
        doi = normalize_doi(row.get("DOI"))
        link = normalize_text(row.get("Link"))
        open_access = normalize_text(row.get("Open Access"))

        if doi:
            candidates.append(
                SourceCandidate(
                    url=f"https://doi.org/{doi}",
                    source_type="doi",
                    source_label="doi_resolver",
                    priority=1,
                )
            )

        if looks_like_url(link):
            candidates.append(
                SourceCandidate(
                    url=link,
                    source_type="link",
                    source_label="provided_link",
                    priority=2,
                )
            )

        if looks_like_url(open_access):
            candidates.append(
                SourceCandidate(
                    url=open_access,
                    source_type="open_access",
                    source_label="open_access_url",
                    priority=3,
                )
            )

        if not candidates and is_truthy_open_access(open_access):
            self.logger.debug(
                "Registro marcado como Open Access, mas sem DOI/Link/URL explícita."
            )

        seen = set()
        unique_candidates: List[SourceCandidate] = []
        for candidate in sorted(candidates, key=lambda c: c.priority):
            if candidate.url not in seen:
                unique_candidates.append(candidate)
                seen.add(candidate.url)
        return unique_candidates

    @staticmethod
    def is_wiley_url(url: str) -> bool:
        """Identifica URLs do Wiley Online Library."""
        try:
            return WILEY_HOST in urlparse(url).netloc.lower()
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def extract_doi_from_text(text: str) -> str:
        """Extrai um DOI de um texto qualquer, se houver."""
        if not text:
            return ""
        match = DOI_RE.search(unquote(str(text)))
        if not match:
            return ""
        return normalize_doi(match.group(0))

    def extract_wiley_doi_from_url(self, url: str) -> str:
        """
        Extrai o DOI de URLs Wiley como:
        - /doi/10.xxxx/xxxxx
        - /doi/epdf/10.xxxx/xxxxx
        - /doi/pdfdirect/10.xxxx/xxxxx
        - /doi/pdf/10.xxxx/xxxxx
        """
        try:
            parsed = urlparse(url)
            path = unquote(parsed.path)
        except Exception:  # noqa: BLE001
            path = unquote(url or "")

        if "/doi/" in path:
            tail = path.split("/doi/", 1)[1].strip("/")
            for prefix in ("full/", "abs/", "epdf/", "pdf/", "pdfdirect/"):
                if tail.lower().startswith(prefix):
                    tail = tail[len(prefix):]
                    break
            doi = self.extract_doi_from_text(tail)
            if doi:
                return doi

        return self.extract_doi_from_text(url)

    def extract_wiley_doi_from_html(self, html: str) -> str:
        """Extrai o DOI de metatags/links/HTML do Wiley."""
        if not html:
            return ""

        soup = BeautifulSoup(html, "html.parser")
        meta_names = (
            "citation_doi",
            "dc.Identifier",
            "DC.Identifier",
            "dc.identifier",
            "doi",
        )
        for name in meta_names:
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                doi = self.extract_doi_from_text(tag.get("content", ""))
                if doi:
                    return doi

        for tag in soup.find_all(["a", "link"], href=True):
            href = tag.get("href", "")
            if "doi" in href.lower():
                doi = self.extract_doi_from_text(href)
                if doi:
                    return doi

        return self.extract_doi_from_text(html)

    def extract_wiley_doi(self, page_url: str, html: str = "") -> str:
        """Extrai o DOI preferindo a URL final e usando o HTML como fallback."""
        doi = self.extract_wiley_doi_from_url(page_url)
        if doi:
            return doi
        return self.extract_wiley_doi_from_html(html)

    @staticmethod
    def build_wiley_pdf_candidates(doi: str) -> List[str]:
        """
        Gera URLs usadas pelo Wiley quando o usuário clica no botão PDF.

        Em muitos artigos, o HTML da página mostra apenas um botão/ícone que abre
        /doi/epdf/... no visualizador. O arquivo real costuma estar disponível em
        /doi/pdfdirect/...; por isso tentamos pdfdirect primeiro e deixamos epdf
        por último como fallback.
        """
        doi = normalize_doi(doi)
        if not doi:
            return []
        base = f"https://{WILEY_HOST}/doi"
        return [
            f"{base}/pdfdirect/{doi}?download=true",
            f"{base}/pdfdirect/{doi}",
            f"{base}/pdf/{doi}",
            f"{base}/epdf/{doi}",
        ]

    def parse_pdf_candidates_from_html(self, page_url: str, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        scored: List[Tuple[int, str]] = []

        def add_candidate(raw_url: Optional[str], score: int) -> None:
            if not raw_url:
                return
            raw_url = raw_url.strip()
            if not raw_url:
                return
            absolute = urljoin(page_url, raw_url)
            lowered = absolute.lower()
            if lowered.startswith("mailto:") or lowered.startswith("javascript:"):
                return
            scored.append((score, absolute))

        # Caso especial Wiley:
        # a página do artigo pode exibir somente o botão PDF, que abre /doi/epdf/...
        # em um visualizador. O download real geralmente está em /doi/pdfdirect/...
        if self.is_wiley_url(page_url):
            doi = self.extract_wiley_doi(page_url, html)
            for wiley_pdf_url in self.build_wiley_pdf_candidates(doi):
                add_candidate(wiley_pdf_url, 120)

        meta_selectors = [
            ("meta[name='citation_pdf_url']", 100),
            ("meta[name='wkhealth_pdf_url']", 95),
            ("meta[property='og:pdf']", 95),
            ("meta[name='pdf_url']", 95),
        ]
        for selector, score in meta_selectors:
            tag = soup.select_one(selector)
            if tag:
                add_candidate(tag.get("content"), score)

        for tag in soup.find_all("a", href=True):
            href = tag.get("href")
            text = normalize_text(tag.get_text(" "))
            aria_label = normalize_text(tag.get("aria-label"))
            title = normalize_text(tag.get("title"))
            combined_text = f"{text} {aria_label} {title}".lower()
            href_lower = (href or "").lower()
            score = 0
            if href_lower.endswith(".pdf"):
                score = 90
            elif looks_like_direct_pdf_url(href_lower):
                score = 80
            elif "pdf" in combined_text or "download" in combined_text or "full text" in combined_text:
                score = 70
            if score:
                add_candidate(href, score)

        for tag_name in ("iframe", "embed", "object"):
            for tag in soup.find_all(tag_name):
                src = tag.get("src") or tag.get("data")
                if src and looks_like_direct_pdf_url(src):
                    add_candidate(src, 85)

        dedup: Dict[str, int] = {}
        for score, candidate in scored:
            if candidate not in dedup or score > dedup[candidate]:
                dedup[candidate] = score
        return [url for url, _ in sorted(dedup.items(), key=lambda item: item[1], reverse=True)]

    def sniff_response_prefix(self, response: Response, max_bytes: int = 4096) -> Tuple[bytes, Iterable[bytes]]:
        iterator = response.iter_content(chunk_size=self.config.chunk_size)
        collected = b""
        try:
            while len(collected) < max_bytes:
                chunk = next(iterator)
                if not chunk:
                    continue
                collected += chunk
                if len(collected) >= max_bytes:
                    break
        except StopIteration:
            pass
        return collected, iterator

    def is_pdf_response(self, response: Response, prefix: bytes = b"") -> bool:
        content_type = normalize_text(response.headers.get("Content-Type")).lower()
        final_url = response.url.lower()
        stripped_prefix = prefix.lstrip().lower()

        if prefix.startswith(PDF_MAGIC):
            return True

        # Alguns visualizadores retornam HTML mesmo quando a URL contém "pdf".
        # Não aceite HTML como PDF.
        if stripped_prefix.startswith((b"<!doctype", b"<html")) or b"<html" in stripped_prefix[:300]:
            return False

        if any(hint in content_type for hint in PDF_CONTENT_HINTS):
            return True
        if final_url.endswith(".pdf"):
            return True
        return False

    def build_target_directory(self, row: Dict[str, Any]) -> Path:
        publisher = safe_path_component(row.get("Publisher", ""), "Unknown_Publisher")
        year = safe_path_component(row.get("Year", ""), "Unknown_Year")
        path = self.output_dir / publisher / year
        path.mkdir(parents=True, exist_ok=True)
        return path

    def nonconflicting_path(self, path: Path, row: Dict[str, Any], final_url: str) -> Path:
        if not path.exists():
            return path
        if file_starts_with_pdf_magic(path):
            return path
        fingerprint_source = "|".join(
            [
                normalize_doi(row.get("DOI")),
                normalize_text(final_url),
                normalize_text(row.get("Title")),
            ]
        )
        suffix = sha256_text(fingerprint_source)[:8]
        new_name = sanitize_filename(
            f"{path.stem} [{suffix}]{path.suffix}",
            max_length=self.config.max_filename_length,
        )
        return path.with_name(new_name)

    def download_pdf_to_disk(
        self,
        *,
        source_url: str,
        candidate_url: str,
        row: Dict[str, Any],
    ) -> Tuple[str, str, str, str, str]:
        request_headers = {
            "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
        }
        if source_url:
            request_headers["Referer"] = source_url

        response = self.request_with_retry(
            "GET",
            candidate_url,
            timeout=self.config.timeout,
            stream=True,
            allow_redirects=True,
            headers=request_headers,
        )
        prefix, iterator = self.sniff_response_prefix(response)
        if not self.is_pdf_response(response, prefix):
            response.close()
            raise DownloadError(
                f"Resposta não é PDF. Content-Type={response.headers.get('Content-Type')} URL={response.url}"
            )

        final_url = normalize_text(response.url)
        filename = build_output_filename(row, self.config.max_filename_length)
        target_dir = self.build_target_directory(row)
        target_path = self.nonconflicting_path(target_dir / filename, row, final_url)

        if target_path.exists() and file_starts_with_pdf_magic(target_path):
            self.duplicates.mark_success(
                doi=normalize_doi(row.get("DOI")),
                final_url=final_url,
                saved_path=str(target_path),
            )
            response.close()
            return (
                "success_exists",
                final_url,
                str(target_path),
                str(response.status_code),
                normalize_text(response.headers.get("Content-Type")),
            )

        duplicate = self.duplicates.detect_duplicate(
            doi=normalize_doi(row.get("DOI")),
            final_url=final_url,
            saved_path=str(target_path),
        )
        if duplicate:
            response.close()
            _, message = duplicate
            raise DownloadError(message)

        if final_url and not self.duplicates.reserve_final_url(final_url):
            response.close()
            raise DownloadError(f"URL final em processamento ou já concluída: {final_url}")

        temp_path = target_path.with_suffix(target_path.suffix + ".part")
        try:
            with temp_path.open("wb") as handle:
                if prefix:
                    handle.write(prefix)
                for chunk in iterator:
                    if chunk:
                        handle.write(chunk)

            if not file_starts_with_pdf_magic(temp_path):
                raise DownloadError("Arquivo salvo não possui assinatura PDF válida")

            temp_path.replace(target_path)
            self.duplicates.mark_success(
                doi=normalize_doi(row.get("DOI")),
                final_url=final_url,
                saved_path=str(target_path),
            )
            return (
                "success",
                final_url,
                str(target_path),
                str(response.status_code),
                normalize_text(response.headers.get("Content-Type")),
            )
        finally:
            response.close()
            self.duplicates.release_final_url(final_url)
            if temp_path.exists() and not target_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def inspect_candidate_url(
        self,
        *,
        source_url: str,
        candidate_url: str,
        row: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], List[str]]:
        messages: List[str] = []

        if looks_like_direct_pdf_url(candidate_url):
            try:
                result = self.download_pdf_to_disk(
                    source_url=source_url,
                    candidate_url=candidate_url,
                    row=row,
                )
                return (*result, messages)
            except Exception as exc:  # noqa: BLE001
                messages.append(f"Tentativa direta falhou em {candidate_url}: {exc}")

        response = self.request_with_retry(
            "GET",
            candidate_url,
            timeout=self.config.timeout,
            stream=False,
            allow_redirects=True,
        )
        content_type = normalize_text(response.headers.get("Content-Type"))

        lowered_content_type = content_type.lower()
        direct_pdf_hint = any(hint in lowered_content_type for hint in PDF_CONTENT_HINTS) or response.url.lower().endswith(".pdf")
        prefix = response.content[:4096]
        if direct_pdf_hint or prefix.startswith(PDF_MAGIC):
            response.close()
            result = self.download_pdf_to_disk(
                source_url=source_url,
                candidate_url=candidate_url,
                row=row,
            )
            return (*result, messages)

        if not any(hint in lowered_content_type for hint in HTML_CONTENT_HINTS):
            response.close()
            messages.append(
                f"Conteúdo não HTML e não PDF em {candidate_url}: {content_type or 'desconhecido'}"
            )
            return (None, None, None, str(response.status_code), content_type, messages)

        html = response.text
        page_url = response.url
        http_status = str(response.status_code)
        response.close()

        pdf_candidates = self.parse_pdf_candidates_from_html(page_url, html)
        if not pdf_candidates:
            messages.append(f"Nenhum link de PDF encontrado em {page_url}")
            return (None, page_url, None, http_status, content_type, messages)

        for pdf_url in pdf_candidates:
            try:
                result = self.download_pdf_to_disk(
                    source_url=source_url,
                    candidate_url=pdf_url,
                    row=row,
                )
                return (*result, messages)
            except Exception as exc:  # noqa: BLE001
                messages.append(f"Falha no candidato PDF {pdf_url}: {exc}")

        return (None, page_url, None, http_status, content_type, messages)

    def process_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row_id = int(row["row_id"])
        started_at = datetime.now().isoformat(timespec="seconds")
        title = normalize_text(row.get("Title"))
        doi = normalize_doi(row.get("DOI"))
        publisher = normalize_text(row.get("Publisher"))
        year = normalize_text(row.get("Year"))

        if self.state.already_succeeded_row(row_id):
            previous = self.state.get_row(row_id) or {}
            return ProcessResult(
                row_id=row_id,
                title=title,
                doi=doi,
                source_url=normalize_text(previous.get("source_url")),
                final_url=normalize_text(previous.get("final_url")),
                status="skipped_already_done",
                saved_path=normalize_text(previous.get("saved_path")),
                error_message="Linha já concluída com sucesso em execução anterior.",
                publisher=publisher,
                year=year,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                http_status=normalize_text(previous.get("http_status")),
                content_type=normalize_text(previous.get("content_type")),
            ).to_record()

        if row_id in self.input_duplicate_map:
            reason, original_row_id = self.input_duplicate_map[row_id]
            return ProcessResult(
                row_id=row_id,
                title=title,
                doi=doi,
                source_url="",
                final_url="",
                status="skipped_duplicate_input",
                saved_path="",
                error_message=f"Duplicado da linha {original_row_id}: {reason}",
                publisher=publisher,
                year=year,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            ).to_record()

        if doi and doi in self.duplicates.success_dois:
            return ProcessResult(
                row_id=row_id,
                title=title,
                doi=doi,
                source_url="",
                final_url="",
                status="skipped_duplicate_doi",
                saved_path="",
                error_message=f"DOI já baixado anteriormente: {doi}",
                publisher=publisher,
                year=year,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            ).to_record()

        candidates = self.extract_source_candidates(row)
        if not candidates:
            return ProcessResult(
                row_id=row_id,
                title=title,
                doi=doi,
                source_url="",
                final_url="",
                status="failed_no_source",
                saved_path="",
                error_message="Nenhum DOI/Link/Open Access URL utilizável foi informado.",
                publisher=publisher,
                year=year,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            ).to_record()

        collected_errors: List[str] = []
        for candidate in candidates:
            try:
                status, final_url, saved_path, http_status, content_type, messages = self.inspect_candidate_url(
                    source_url=candidate.url,
                    candidate_url=candidate.url,
                    row=row,
                )
                collected_errors.extend(messages)
                if status in {"success", "success_exists"}:
                    return ProcessResult(
                        row_id=row_id,
                        title=title,
                        doi=doi,
                        source_url=candidate.url,
                        final_url=final_url or "",
                        status=status,
                        saved_path=saved_path or "",
                        error_message=" | ".join(collected_errors)[:4000],
                        publisher=publisher,
                        year=year,
                        started_at=started_at,
                        finished_at=datetime.now().isoformat(timespec="seconds"),
                        http_status=http_status or "",
                        content_type=content_type or "",
                        source_type=candidate.source_type,
                    ).to_record()
            except Exception as exc:  # noqa: BLE001
                collected_errors.append(f"Fonte {candidate.url} falhou: {exc}")
                continue

        return ProcessResult(
            row_id=row_id,
            title=title,
            doi=doi,
            source_url=candidates[0].url if candidates else "",
            final_url="",
            status="failed_no_pdf_found",
            saved_path="",
            error_message=" | ".join(collected_errors)[:4000],
            publisher=publisher,
            year=year,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            source_type=candidates[0].source_type if candidates else "",
        ).to_record()

    def _persist_result(self, record: Dict[str, Any]) -> None:
        self.state.append(record)
        with self.results_lock:
            self.results.append(record)

    def _log_result(self, record: Dict[str, Any]) -> None:
        status = record.get("status")
        row_id = record.get("row_id")
        title = record.get("title")
        saved_path = record.get("saved_path")
        error = record.get("error_message")
        if status in {"success", "success_exists"}:
            self.logger.info("[%s] %s | salvo em %s", row_id, title, saved_path)
        elif str(status).startswith("skipped"):
            self.logger.info("[%s] %s | %s", row_id, status, error)
        else:
            self.logger.warning("[%s] %s | %s", row_id, status, error)

    def run(self) -> Tuple[pd.DataFrame, Dict[str, int]]:
        df = self.load_dataframe()
        rows = df.to_dict(orient="records")
        self.logger.info(
            "Iniciando processamento com %s workers, timeout=%ss, retries=%s",
            self.config.workers,
            self.config.timeout,
            self.config.retries,
        )

        with cf.ThreadPoolExecutor(max_workers=self.config.workers) as executor:
            future_to_row_id = {executor.submit(self.process_row, row): int(row["row_id"]) for row in rows}
            with tqdm(total=len(future_to_row_id), desc="Processando", unit="artigo") as progress:
                for future in cf.as_completed(future_to_row_id):
                    try:
                        record = future.result()
                    except Exception as exc:  # noqa: BLE001
                        row_id = future_to_row_id[future]
                        record = {
                            "row_id": row_id,
                            "title": "",
                            "doi": "",
                            "source_url": "",
                            "final_url": "",
                            "status": "failed_unhandled_exception",
                            "saved_path": "",
                            "error_message": str(exc),
                            "publisher": "",
                            "year": "",
                            "started_at": "",
                            "finished_at": datetime.now().isoformat(timespec="seconds"),
                            "http_status": "",
                            "content_type": "",
                            "source_type": "",
                        }
                    self._persist_result(record)
                    self._log_result(record)
                    progress.update(1)

        final_df = self.build_final_report_dataframe(df)
        summary = self.write_reports(final_df)
        self.logger.info("Execução concluída. Resumo: %s", summary)
        return final_df, summary

    def build_final_report_dataframe(self, input_df: pd.DataFrame) -> pd.DataFrame:
        latest_records = {int(k): v for k, v in self.state.records_by_row_id.items()}
        current_run_records: Dict[int, Dict[str, Any]] = {}
        for record in self.results:
            current_run_records[int(record["row_id"])] = record

        report_records: List[Dict[str, Any]] = []
        for _, row in input_df.iterrows():
            row_id = int(row["row_id"])
            latest = current_run_records.get(row_id) or latest_records.get(row_id)
            if latest is None:
                latest = ProcessResult(
                    row_id=row_id,
                    title=normalize_text(row.get("Title")),
                    doi=normalize_doi(row.get("DOI")),
                    source_url="",
                    final_url="",
                    status="not_processed",
                    saved_path="",
                    error_message="Linha sem resultado gravado.",
                    publisher=normalize_text(row.get("Publisher")),
                    year=normalize_text(row.get("Year")),
                ).to_record()
            report_records.append(latest)
        return pd.DataFrame(report_records)

    def write_reports(self, report_df: pd.DataFrame) -> Dict[str, int]:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = self.config.reports_dir / f"report_{timestamp}.csv"
        xlsx_path = self.config.reports_dir / f"report_{timestamp}.xlsx"

        if self.config.report_format in {"csv", "both"}:
            report_df.to_csv(csv_path, index=False, quoting=csv.QUOTE_MINIMAL, encoding="utf-8-sig")
        if self.config.report_format in {"xlsx", "both"}:
            report_df.to_excel(xlsx_path, index=False, engine="openpyxl")

        summary = report_df["status"].value_counts(dropna=False).to_dict()
        summary["total"] = int(len(report_df))
        return summary


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Baixa PDFs de artigos acadêmicos em lote a partir de XLSX/CSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="Caminho do arquivo .xlsx ou .csv")
    parser.add_argument("--output", required=True, help="Diretório de saída")
    parser.add_argument("--workers", type=int, default=5, help="Quantidade de workers paralelos")
    parser.add_argument("--timeout", type=int, default=20, help="Timeout de leitura por requisição em segundos")
    parser.add_argument("--retries", type=int, default=3, help="Número de tentativas para erros temporários")
    parser.add_argument("--backoff-base", type=float, default=1.5, help="Base exponencial do backoff")
    parser.add_argument("--max-filename-length", type=int, default=140, help="Comprimento máximo do nome do arquivo")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent HTTP configurável")
    parser.add_argument("--insecure", action="store_true", help="Desabilita verificação SSL (não recomendado)")
    parser.add_argument(
        "--report-format",
        choices=["csv", "xlsx", "both"],
        default="both",
        help="Formato do relatório final",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Nível de log",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = Config(
        input_path=Path(args.input).expanduser().resolve(),
        output_dir=Path(args.output).expanduser().resolve(),
        workers=max(1, int(args.workers)),
        timeout=max(5, int(args.timeout)),
        retries=max(0, int(args.retries)),
        backoff_base=max(1.0, float(args.backoff_base)),
        max_filename_length=max(60, int(args.max_filename_length)),
        user_agent=args.user_agent,
        verify_ssl=not args.insecure,
        report_format=args.report_format,
        log_level=args.log_level,
    )

    if not config.input_path.exists():
        print(f"Arquivo de entrada não encontrado: {config.input_path}", file=sys.stderr)
        return 2

    try:
        downloader = ArticleDownloader(config)
        _, summary = downloader.run()
        print("Resumo final:")
        for key, value in summary.items():
            print(f"  - {key}: {value}")
        return 0
    except KeyboardInterrupt:
        print("Execução interrompida pelo usuário. O estado parcial foi preservado.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Erro fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())