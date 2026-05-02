"""
Find never-used Python functions across the codebase.

Skips:
  - directories starting with '.'
  - files ending in .log or .txt

Output: unused_functions.txt
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = Path(__file__).resolve().parent / "unused_functions.txt"

DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
SKIP_DIRS = {"."}  # any dir whose name starts with one of these prefixes
SKIP_EXTS = {".log", ".txt"}


def _skip_dir(name: str) -> bool:
    return name.startswith(".")


def collect_py_files(root: Path) -> list[Path]:
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.suffix == ".py" and p.suffix not in SKIP_EXTS:
                result.append(p)
    return result


def extract_definitions(files: list[Path]) -> list[tuple[str, Path, int]]:
    """Returns list of (func_name, file, lineno)."""
    defs = []
    for path in files:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            m = DEF_RE.match(line)
            if m:
                defs.append((m.group(1), path, lineno))
    return defs


def find_references(
    name: str,
    files: list[Path],
    skip_file: Path,
    skip_lineno: int,
) -> dict[Path, int]:
    """Count occurrences of `name` as a word token across all files."""
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")
    refs: dict[Path, int] = defaultdict(int)
    for path in files:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            if path == skip_file and lineno == skip_lineno:
                continue
            if pattern.search(line):
                refs[path] += 1
    return refs


def main() -> None:
    print("Collecting Python files…")
    files = collect_py_files(ROOT)
    print(f"  {len(files)} files found")

    print("Extracting function definitions…")
    defs = extract_definitions(files)
    print(f"  {len(defs)} functions found")

    # Deduplicate: if the same name is defined in multiple files, track all.
    # We process each (name, file, lineno) independently.

    print("Scanning references (this may take a moment)…")
    results = []
    for i, (name, src_file, src_lineno) in enumerate(defs):
        if i % 50 == 0:
            print(f"  {i}/{len(defs)}…", end="\r", flush=True)
        refs = find_references(name, files, src_file, src_lineno)
        total = sum(refs.values())
        results.append((name, src_file, src_lineno, total, refs))

    print()

    # Sort: never-used first, then by call count ascending, then name.
    results.sort(key=lambda r: (r[3], r[1], r[2]))

    rel = lambda p: p.relative_to(ROOT)

    lines_out: list[str] = []
    lines_out.append("=" * 72)
    lines_out.append("PYTHON DEAD-CODE REPORT")
    lines_out.append(f"Root: {ROOT}")
    lines_out.append(f"Files scanned: {len(files)}")
    lines_out.append(f"Functions found: {len(results)}")
    unused = sum(1 for r in results if r[3] == 0)
    lines_out.append(f"Never referenced: {unused}")
    lines_out.append("=" * 72)
    lines_out.append("")

    lines_out.append("── NEVER REFERENCED (" + str(unused) + ") ────────────────────────────────────")
    lines_out.append("")
    for name, src_file, src_lineno, total, refs in results:
        if total > 0:
            continue
        lines_out.append(f"  def {name}")
        lines_out.append(f"    defined at {rel(src_file)}:{src_lineno}")
        lines_out.append("")

    lines_out.append("")
    lines_out.append("── REFERENCED FUNCTIONS (sorted by call count) ──────────────────────")
    lines_out.append("")
    for name, src_file, src_lineno, total, refs in results:
        if total == 0:
            continue
        lines_out.append(f"  def {name}   [{total} reference(s)]")
        lines_out.append(f"    defined at {rel(src_file)}:{src_lineno}")
        for ref_file, count in sorted(refs.items(), key=lambda x: str(x[0])):
            lines_out.append(f"    called in  {rel(ref_file)}  ({count}x)")
        lines_out.append("")

    text = "\n".join(lines_out) + "\n"
    OUTPUT.write_text(text)
    print(f"Report written to {OUTPUT}")
    print(f"Never-referenced: {unused} / {len(results)}")


if __name__ == "__main__":
    main()
