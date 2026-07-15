#!/usr/bin/env python3
"""
Split large Markdown or plain-text files into NotebookLM-friendly parts.

Designed for batch processing and for report compilations containing headings
such as "## Report 123". Report blocks are kept intact whenever possible.
For ordinary Markdown/text files, the fallback splitter prefers headings and
paragraph boundaries before using a hard word split.

No third-party packages are required.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

WORD_RE = re.compile(r"\b[\w]+(?:['’\-][\w]+)*\b", re.UNICODE)
REPORT_RE = re.compile(r"(?m)^##\s+Report\s+(\d+)(?:\s*[—-].*)?\s*$")
TOC_RE = re.compile(r"(?mi)^##\s+Table of Contents\s*$")
MARKDOWN_HEADING_RE = re.compile(r"(?m)^(?=#{1,6}\s+\S)")
ALREADY_SPLIT_RE = re.compile(r"_part_\d+_of_\d+$", re.IGNORECASE)
SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}


@dataclass
class Unit:
    text: str
    word_count: int
    label: str | None = None


@dataclass
class OutputRecord:
    source_file: str
    output_file: str
    part: int
    total_parts: int
    word_count: int
    first_label: str
    last_label: str
    mode: str


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def safe_stem(path: Path, max_length: int = 110) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._-") or "document"
    if len(stem) <= max_length:
        return stem
    digest = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]
    return f"{stem[: max_length - 9]}_{digest}"


def extract_title(text: str, fallback: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if match:
        return match.group(1).strip()
    return fallback.replace("_", " ").strip().title()


def remove_giant_toc(prefix: str) -> str:
    """Remove a pre-report Markdown table of contents while retaining the intro."""
    match = TOC_RE.search(prefix)
    if not match:
        return prefix.strip()
    return prefix[: match.start()].rstrip()


def report_units(text: str) -> tuple[str, list[Unit]] | None:
    matches = list(REPORT_RE.finditer(text))
    if not matches:
        return None

    prefix = remove_giant_toc(text[: matches[0].start()])
    units: list[Unit] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start() : end].strip() + "\n"
        report_number = match.group(1)
        units.append(Unit(block, count_words(block), f"Report {report_number}"))
    return prefix, units


def hard_split_by_words(text: str, max_words: int) -> list[str]:
    """Split oversized text while preserving original whitespace."""
    matches = list(WORD_RE.finditer(text))
    if len(matches) <= max_words:
        return [text]

    pieces: list[str] = []
    start_char = 0
    for start_word in range(0, len(matches), max_words):
        end_word = min(start_word + max_words, len(matches))
        if end_word < len(matches):
            end_char = matches[end_word].start()
        else:
            end_char = len(text)
        piece = text[start_char:end_char].strip()
        if piece:
            pieces.append(piece + "\n")
        start_char = end_char
    return pieces


def paragraph_units(text: str, max_unit_words: int) -> list[Unit]:
    paragraphs = re.split(r"\n\s*\n", text)
    units: list[Unit] = []
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        paragraph += "\n"
        words = count_words(paragraph)
        if words <= max_unit_words:
            units.append(Unit(paragraph, words))
        else:
            for piece in hard_split_by_words(paragraph, max_unit_words):
                units.append(Unit(piece, count_words(piece)))
    return units


def generic_units(text: str, max_unit_words: int) -> tuple[str, list[Unit]]:
    """Create semantic units from Markdown headings, then paragraphs."""
    starts = [match.start() for match in MARKDOWN_HEADING_RE.finditer(text)]
    if not starts:
        return "", paragraph_units(text, max_unit_words)

    prefix = text[: starts[0]].strip()
    sections: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        sections.append(text[start:end].strip() + "\n")

    units: list[Unit] = []
    for section in sections:
        words = count_words(section)
        heading_match = re.match(r"#{1,6}\s+(.+?)\s*$", section.splitlines()[0])
        label = heading_match.group(1).strip() if heading_match else None
        if words <= max_unit_words:
            units.append(Unit(section, words, label))
        else:
            split_parts = paragraph_units(section, max_unit_words)
            for i, unit in enumerate(split_parts, 1):
                if i == 1:
                    unit.label = label
                units.append(unit)
    return prefix, units


def pack_units(units: Sequence[Unit], content_budget: int) -> list[list[Unit]]:
    chunks: list[list[Unit]] = []
    current: list[Unit] = []
    current_words = 0

    for unit in units:
        if unit.word_count > content_budget:
            # This should be rare because generic units are pre-split. It can
            # happen if one report itself is enormous, so split it safely.
            if current:
                chunks.append(current)
                current = []
                current_words = 0
            for piece in hard_split_by_words(unit.text, content_budget):
                chunks.append([Unit(piece, count_words(piece), unit.label)])
            continue

        if current and current_words + unit.word_count > content_budget:
            chunks.append(current)
            current = []
            current_words = 0

        current.append(unit)
        current_words += unit.word_count

    if current:
        chunks.append(current)
    return chunks


def make_local_toc(units: Sequence[Unit], max_entries: int = 5000) -> str:
    labels = [unit.label for unit in units if unit.label]
    if not labels or len(labels) > max_entries:
        return ""
    lines = ["## Contents of this part", ""]
    lines.extend(f"- {label}" for label in labels)
    return "\n".join(lines).rstrip() + "\n\n"


def build_part_text(
    *,
    title: str,
    source_name: str,
    prefix: str,
    units: Sequence[Unit],
    part_number: int,
    total_parts: int,
    mode: str,
    include_local_toc: bool,
) -> str:
    labels = [unit.label for unit in units if unit.label]
    first_label = labels[0] if labels else "Beginning of part"
    last_label = labels[-1] if labels else "End of part"

    header = (
        f"# {title} — Part {part_number:03d} of {total_parts:03d}\n\n"
        f"> Split for NotebookLM from `{source_name}`.  \n"
        f"> Range: {first_label} through {last_label}.  \n"
        f"> Splitting mode: {mode}.\n\n"
    )

    intro = ""
    cleaned_prefix = prefix.strip()
    if cleaned_prefix:
        # Avoid duplicating the original H1 immediately under the part H1.
        cleaned_prefix = re.sub(r"(?m)^#\s+.+?\s*$", "", cleaned_prefix, count=1).strip()
        if cleaned_prefix:
            intro = "## Original source introduction\n\n" + cleaned_prefix + "\n\n"

    toc = make_local_toc(units) if include_local_toc else ""
    body = "\n\n".join(unit.text.strip() for unit in units).strip() + "\n"
    return normalise_newlines(header + intro + toc + "---\n\n" + body)


def collect_input_files(paths: Sequence[Path], recursive: bool, output_dir: Path) -> list[Path]:
    files: list[Path] = []
    output_dir_resolved = output_dir.resolve()

    for path in paths:
        path = path.expanduser()
        if not path.exists():
            print(f"WARNING: input does not exist: {path}", file=sys.stderr)
            continue

        candidates: Iterable[Path]
        if path.is_file():
            candidates = [path]
        else:
            candidates = path.rglob("*") if recursive else path.glob("*")

        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            try:
                candidate.resolve().relative_to(output_dir_resolved)
                continue
            except ValueError:
                pass
            if ALREADY_SPLIT_RE.search(candidate.stem):
                continue
            files.append(candidate.resolve())

    return sorted(set(files))


def split_file(
    source: Path,
    output_root: Path,
    max_words: int,
    reserve_words: int,
    include_local_toc: bool,
    overwrite: bool,
    dry_run: bool,
) -> list[OutputRecord]:
    text = normalise_newlines(source.read_text(encoding="utf-8-sig", errors="replace"))
    source_words = count_words(text)
    stem = safe_stem(source)
    destination = output_root / stem

    parsed_reports = report_units(text)
    if parsed_reports:
        prefix, units = parsed_reports
        mode = "report-boundary"
    else:
        prefix, units = generic_units(text, max_words - reserve_words)
        mode = "heading/paragraph-boundary"

    content_budget = max_words - reserve_words
    if content_budget < 10_000:
        raise ValueError("max_words must exceed reserve_words by at least 10,000")

    chunks = pack_units(units, content_budget)
    if not chunks:
        raise ValueError(f"No readable content found in {source}")

    print(
        f"{source.name}: {source_words:,} words -> {len(chunks)} part(s) "
        f"using {mode} splitting"
    )

    if dry_run:
        return []

    if destination.exists():
        if overwrite:
            shutil.rmtree(destination)
        elif any(destination.iterdir()):
            raise FileExistsError(
                f"Output folder already exists and is not empty: {destination}\n"
                "Use --overwrite to replace it."
            )
    destination.mkdir(parents=True, exist_ok=True)

    title = extract_title(text, source.stem)
    total_parts = len(chunks)
    records: list[OutputRecord] = []

    for part_number, chunk_units in enumerate(chunks, 1):
        part_text = build_part_text(
            title=title,
            source_name=source.name,
            prefix=prefix,
            units=chunk_units,
            part_number=part_number,
            total_parts=total_parts,
            mode=mode,
            include_local_toc=include_local_toc,
        )
        output_words = count_words(part_text)
        if output_words > max_words:
            raise RuntimeError(
                f"Generated part {part_number} has {output_words:,} words, exceeding "
                f"the configured {max_words:,}-word limit. Increase --reserve-words."
            )

        output_name = f"{stem}_part_{part_number:03d}_of_{total_parts:03d}.md"
        output_path = destination / output_name
        output_path.write_text(part_text, encoding="utf-8", newline="\n")

        labels = [unit.label for unit in chunk_units if unit.label]
        records.append(
            OutputRecord(
                source_file=str(source),
                output_file=str(output_path),
                part=part_number,
                total_parts=total_parts,
                word_count=output_words,
                first_label=labels[0] if labels else "",
                last_label=labels[-1] if labels else "",
                mode=mode,
            )
        )

    per_source_manifest = destination / "manifest.json"
    per_source_manifest.write_text(
        json.dumps([record.__dict__ for record in records], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return records


def write_master_manifest(output_root: Path, records: Sequence[OutputRecord]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OutputRecord.__dataclass_fields__.keys()))
        writer.writeheader()
        for record in records:
            writer.writerow(record.__dict__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-split large Markdown/text files into NotebookLM-friendly parts, "
            "preserving report or document boundaries."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="One or more files or directories to process.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("notebooklm_parts"),
        help="Root output directory (default: ./notebooklm_parts).",
    )
    parser.add_argument(
        "--max-words",
        type=int,
        default=450_000,
        help="Maximum words per generated part (default: 450000).",
    )
    parser.add_argument(
        "--reserve-words",
        type=int,
        default=5_000,
        help="Words reserved for generated headers and local TOCs (default: 5000).",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Search input directories recursively.",
    )
    parser.add_argument(
        "--no-local-toc",
        action="store_true",
        help="Do not add a compact list of included report/section labels.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing output folders for matching source files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without writing output files.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output_root = args.output_dir.expanduser().resolve()

    if args.max_words <= 0 or args.reserve_words < 0:
        print("ERROR: word limits must be positive.", file=sys.stderr)
        return 2

    files = collect_input_files(args.inputs, args.recursive, output_root)
    if not files:
        print("No .md, .markdown, or .txt files found.", file=sys.stderr)
        return 1

    all_records: list[OutputRecord] = []
    failures: list[tuple[Path, str]] = []

    for source in files:
        try:
            all_records.extend(
                split_file(
                    source=source,
                    output_root=output_root,
                    max_words=args.max_words,
                    reserve_words=args.reserve_words,
                    include_local_toc=not args.no_local_toc,
                    overwrite=args.overwrite,
                    dry_run=args.dry_run,
                )
            )
        except Exception as exc:  # Continue batch processing after one bad file.
            failures.append((source, str(exc)))
            print(f"ERROR: {source}: {exc}", file=sys.stderr)

    if not args.dry_run and all_records:
        write_master_manifest(output_root, all_records)
        print(f"\nWrote {len(all_records)} part(s) to: {output_root}")
        print(f"Master manifest: {output_root / 'manifest.csv'}")

    if failures:
        print("\nFailures:", file=sys.stderr)
        for source, message in failures:
            print(f"  - {source}: {message}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
