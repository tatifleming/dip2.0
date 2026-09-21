from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


try:
    import pymupdf  # Nome atual da biblioteca PyMuPDF
except ImportError:
    try:
        import fitz as pymupdf  # Compatibilidade com versões antigas
    except ImportError:
        print("Erro: instale PyMuPDF com: python -m pip install PyMuPDF")
        sys.exit(1)


# Regex baseada no padrão prático usado para DOIs modernos.
# Aceita DOI puro, DOI com "doi:" e DOI em URL doi.org.
# Compatível com Python 3.14: as flags ficam fora da string da regex.
DOI_REGEX = re.compile(
    r"""
    (?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)?
    (
        10\.\d{4,9}
        (?:\.\d+)*
        /
        [-._;()/:A-Z0-9]+
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


# Regex usada para ignorar linhas comuns que não costumam ser o título.
# Compatível com Python 3.14: as flags ficam fora da string da regex.
SKIP_TITLE_LINE_REGEX = re.compile(
    r"""
    ^(
        abstract|resumo|keywords|palavras-chave|introduction|
        doi|https?://|www\.|copyright|journal|volume|vol\.|
        received|accepted|published
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


@dataclass
class PdfRecord:
    original_path: str
    relative_path: str
    doi: Optional[str]
    title_guess: Optional[str]
    file_sha256: str
    text_sha256: Optional[str]
    text_length: int
    file_size_bytes: int
    duplicate_key: str
    duplicate_method: str
    kept: bool = False
    status: str = ""
    kept_as_main: Optional[str] = None
    error: Optional[str] = None


def find_pdfs(input_root: Path, output_root: Path, no_doi_root: Optional[Path] = None) -> list[Path]:
    """Percorre recursivamente o diretório de entrada e localiza PDFs."""
    pdfs: list[Path] = []

    input_root = input_root.resolve()
    output_root = output_root.resolve()
    excluded_roots = [output_root]
    if no_doi_root is not None:
        excluded_roots.append(no_doi_root.resolve())

    for path in input_root.rglob("*"):
        if not path.is_file():
            continue

        # Evita reprocessar arquivos caso a saída ou a pasta sem DOI
        # estejam dentro da entrada.
        resolved_path = path.resolve()
        should_skip = False
        for excluded_root in excluded_roots:
            try:
                resolved_path.relative_to(excluded_root)
                should_skip = True
                break
            except ValueError:
                pass

        if should_skip:
            continue

        if path.suffix.lower() == ".pdf":
            pdfs.append(path)

    return sorted(pdfs)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Calcula SHA-256 binário do arquivo."""
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)

    return h.hexdigest()


def normalize_text_for_hash(text: str) -> str:
    """
    Normaliza texto para detectar PDFs com o mesmo conteúdo,
    mesmo que tenham pequenas diferenças de espaços, quebras de linha etc.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9À-ÿ ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sha256_text(text: str) -> Optional[str]:
    normalized = normalize_text_for_hash(text)

    # Evita usar texto muito curto como base para deduplicação.
    if len(normalized) < 500:
        return None

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def prepare_text_for_doi_search(text: str) -> str:
    """
    Normaliza texto para melhorar a chance de encontrar DOI.
    PDFs às vezes quebram DOI entre linhas ou inserem espaços estranhos.
    """
    text = unicodedata.normalize("NFKC", text)

    replacements = {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "\u00ad": "",  # soft hyphen
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\bdoi\s*:\s*", "doi:", text, flags=re.IGNORECASE)
    text = re.sub(
        r"https?://(?:dx\.)?doi\.org/\s+",
        "https://doi.org/",
        text,
        flags=re.IGNORECASE,
    )

    # Junta quebras imediatamente depois da barra do DOI.
    text = re.sub(
        r"(10\.\d{4,9}(?:\.\d+)*/)\s+",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(r"\s+", " ", text)
    return text


def clean_doi(raw_doi: str) -> str:
    """Remove prefixos, pontuação sobrando e normaliza DOI para comparação."""
    doi = raw_doi.strip()

    doi = re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)",
        "",
        doi,
        flags=re.IGNORECASE,
    )

    doi = doi.replace(" ", "")
    doi = doi.replace("\n", "")
    doi = doi.replace("\r", "")

    # Remove pontuação que normalmente vem depois do DOI em citações.
    doi = doi.rstrip(".,;:")

    # Remove parênteses/colchetes finais quando parecem ser só fechamento de frase.
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]

    while doi.endswith("]") and doi.count("]") > doi.count("["):
        doi = doi[:-1]

    doi = doi.strip().lower()
    return doi


def extract_doi(text: str) -> Optional[str]:
    prepared = prepare_text_for_doi_search(text)
    matches = DOI_REGEX.findall(prepared)

    if not matches:
        return None

    cleaned = [clean_doi(m) for m in matches]
    cleaned = [d for d in cleaned if d.startswith("10.") and "/" in d]

    if not cleaned:
        return None

    # Retorna o primeiro DOI válido encontrado.
    return cleaned[0]


def extract_title_guess(text: str, metadata_title: Optional[str] = None) -> Optional[str]:
    """
    Heurística simples para título:
    1. Usa título dos metadados do PDF, se parecer útil.
    2. Caso contrário, tenta uma linha inicial do texto.
    """

    def clean_line(line: str) -> str:
        line = unicodedata.normalize("NFKC", line)
        line = re.sub(r"\s+", " ", line).strip()
        return line

    if metadata_title:
        candidate = clean_line(metadata_title)
        if 8 <= len(candidate) <= 250 and not candidate.lower().endswith(".pdf"):
            return candidate

    lines = [clean_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    for line in lines[:80]:
        if SKIP_TITLE_LINE_REGEX.search(line):
            continue

        has_letter = re.search(r"[A-Za-zÀ-ÿ]", line) is not None
        if has_letter and 12 <= len(line) <= 220:
            return line

    return None


def extract_pdf_text_and_metadata(path: Path) -> tuple[str, Optional[str], Optional[str]]:
    """
    Extrai texto e título dos metadados.
    Retorna: texto, metadata_title, erro.
    """
    try:
        parts: list[str] = []

        with pymupdf.open(path) as doc:
            metadata = doc.metadata or {}
            metadata_title = metadata.get("title")

            # Inclui metadados no começo porque alguns PDFs guardam DOI ali.
            metadata_text = "\n".join(
                str(v) for v in metadata.values() if v is not None
            )
            if metadata_text.strip():
                parts.append(metadata_text)

            for page in doc:
                parts.append(page.get_text("text", sort=True))

        return "\n".join(parts), metadata_title, None

    except Exception as exc:
        return "", None, f"{type(exc).__name__}: {exc}"


def make_record(path: Path, input_root: Path) -> PdfRecord:
    relative_path = str(path.relative_to(input_root))
    file_hash = sha256_file(path)
    file_size = path.stat().st_size

    text, metadata_title, error = extract_pdf_text_and_metadata(path)
    doi = extract_doi(text) if text else None
    text_hash = sha256_text(text) if text else None
    title = extract_title_guess(text, metadata_title) if text else metadata_title

    if doi:
        duplicate_key = f"doi:{doi}"
        duplicate_method = "doi"
    elif text_hash:
        duplicate_key = f"text_sha256:{text_hash}"
        duplicate_method = "text_sha256_no_doi"
    else:
        duplicate_key = f"file_sha256:{file_hash}"
        duplicate_method = "file_sha256_no_doi"

    return PdfRecord(
        original_path=str(path),
        relative_path=relative_path,
        doi=doi,
        title_guess=title,
        file_sha256=file_hash,
        text_sha256=text_hash,
        text_length=len(text),
        file_size_bytes=file_size,
        duplicate_key=duplicate_key,
        duplicate_method=duplicate_method,
        error=error,
    )


def choose_main_record(records: list[PdfRecord]) -> PdfRecord:
    """
    Escolhe a versão principal de forma determinística:
    1. maior quantidade de texto extraído;
    2. maior tamanho de arquivo;
    3. menor caminho lexicográfico.
    """
    return sorted(
        records,
        key=lambda r: (-r.text_length, -r.file_size_bytes, r.original_path.lower()),
    )[0]


def copy_kept_files(records: list[PdfRecord], input_root: Path, output_root: Path) -> None:
    for record in records:
        if not record.kept:
            continue

        src = Path(record.original_path)
        dst = output_root / Path(record.relative_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def copy_no_doi_files(records: list[PdfRecord], no_doi_root: Path) -> None:
    """
    Copia todos os PDFs sem DOI para uma pasta separada, preservando a
    mesma estrutura relativa de diretórios do acervo original.

    Importante: copia todos os arquivos sem DOI analisados, inclusive aqueles
    que foram considerados duplicados por hash de texto ou hash binário.
    Assim eles ficam disponíveis para revisão manual.
    """
    no_doi_root.mkdir(parents=True, exist_ok=True)

    for record in records:
        if record.doi:
            continue

        src = Path(record.original_path)
        dst = no_doi_root / Path(record.relative_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def write_reports(records: list[PdfRecord], report_dir: Path, no_doi_root: Optional[Path] = None) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)

    csv_path = report_dir / "relatorio_pdfs.csv"
    json_path = report_dir / "relatorio_pdfs.json"
    summary_path = report_dir / "resumo.json"

    rows = [asdict(r) for r in records]

    fieldnames = [
        "original_path",
        "relative_path",
        "doi",
        "title_guess",
        "duplicate_method",
        "status",
        "kept",
        "kept_as_main",
        "file_sha256",
        "text_sha256",
        "text_length",
        "file_size_bytes",
        "error",
    ]

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    total = len(records)
    unique = sum(1 for r in records if r.kept)
    duplicates = total - unique
    without_doi = [r for r in records if not r.doi]

    duplicate_groups: dict[str, list[PdfRecord]] = defaultdict(list)
    for r in records:
        duplicate_groups[r.duplicate_key].append(r)

    groups_report = []
    for key, group in duplicate_groups.items():
        if len(group) > 1:
            groups_report.append(
                {
                    "duplicate_key": key,
                    "method": group[0].duplicate_method,
                    "kept_as_main": next(r.original_path for r in group if r.kept),
                    "duplicates": [r.original_path for r in group if not r.kept],
                }
            )

    summary = {
        "total_pdfs_analisados": total,
        "quantidade_artigos_unicos": unique,
        "quantidade_duplicatas_encontradas": duplicates,
        "quantidade_sem_doi_identificado": len(without_doi),
        "pasta_arquivos_sem_doi": str(no_doi_root) if no_doi_root else None,
        "arquivos_sem_doi_identificado": [r.original_path for r in without_doi],
        "grupos_de_duplicatas": groups_report,
    }

    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def process(input_root: Path, output_root: Path, report_dir: Path, no_doi_root: Optional[Path] = None) -> None:
    input_root = input_root.resolve()
    output_root = output_root.resolve()
    report_dir = report_dir.resolve()
    if no_doi_root is None:
        no_doi_root = output_root / "_sem_doi"
    no_doi_root = no_doi_root.resolve()

    if not input_root.exists() or not input_root.is_dir():
        raise ValueError(f"Diretório de entrada inválido: {input_root}")

    output_root.mkdir(parents=True, exist_ok=True)
    no_doi_root.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    pdfs = find_pdfs(input_root, output_root, no_doi_root)

    records: list[PdfRecord] = []
    for i, pdf_path in enumerate(pdfs, start=1):
        print(f"[{i}/{len(pdfs)}] Analisando: {pdf_path}")
        records.append(make_record(pdf_path, input_root))

    groups: dict[str, list[PdfRecord]] = defaultdict(list)
    for record in records:
        groups[record.duplicate_key].append(record)

    for group in groups.values():
        main = choose_main_record(group)

        for record in group:
            record.kept_as_main = main.original_path

            if record is main:
                record.kept = True
                if len(group) == 1:
                    if record.doi:
                        record.status = "mantido_unico_com_doi"
                    else:
                        record.status = "mantido_unico_sem_doi"
                else:
                    if record.duplicate_method == "doi":
                        record.status = "mantido_principal_grupo_doi"
                    else:
                        record.status = "mantido_principal_grupo_sem_doi"
            else:
                record.kept = False
                if record.duplicate_method == "doi":
                    record.status = "duplicado_por_doi"
                elif record.duplicate_method == "text_sha256_no_doi":
                    record.status = "duplicado_sem_doi_por_texto_normalizado"
                else:
                    record.status = "duplicado_sem_doi_por_hash_binario"

    copy_kept_files(records, input_root, output_root)
    copy_no_doi_files(records, no_doi_root)
    write_reports(records, report_dir, no_doi_root)

    total = len(records)
    unique = sum(1 for r in records if r.kept)
    duplicates = total - unique
    no_doi = sum(1 for r in records if not r.doi)

    print("\nConcluído.")
    print(f"PDFs analisados: {total}")
    print(f"Artigos únicos copiados: {unique}")
    print(f"Duplicatas encontradas: {duplicates}")
    print(f"Arquivos sem DOI identificado: {no_doi}")
    print(f"Pasta de saída: {output_root}")
    print(f"Pasta dos arquivos sem DOI: {no_doi_root}")
    print(f"Relatórios: {report_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deduplica PDFs científicos usando DOI como critério principal."
    )

    parser.add_argument(
        "--entrada",
        required=True,
        help="Diretório principal original contendo PDFs.",
    )

    parser.add_argument(
        "--saida",
        required=True,
        help="Nova pasta onde os PDFs únicos serão copiados.",
    )

    parser.add_argument(
        "--relatorio",
        required=True,
        help="Pasta onde os relatórios CSV/JSON serão salvos.",
    )

    parser.add_argument(
        "--sem-doi",
        required=False,
        default=None,
        help=(
            "Pasta separada onde todos os PDFs sem DOI serão copiados. "
            "Se não for informado, usa uma subpasta chamada _sem_doi dentro da pasta de saída."
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    process(
        input_root=Path(args.entrada),
        output_root=Path(args.saida),
        report_dir=Path(args.relatorio),
        no_doi_root=Path(args.sem_doi) if args.sem_doi else None,
    )


if __name__ == "__main__":
    main()
