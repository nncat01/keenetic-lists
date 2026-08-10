#!/usr/bin/env python3
"""
Собирает списки доменов и подсетей из репозитория itdoginfo/allow-domains
и подготавливает их под ограничения Keenetic (KeeneticOS):

  - список доменных имён:  не более 300 строк на файл  -> режем по 299
  - список подсетей:       не более 1024 строк на файл -> режем по 1023
  - список подсетей отдаётся в формате .bat (route ADD <net> MASK <mask> 0.0.0.0)

Источники:
  Categories/*.lst        -> output/domains/Categories/<name><N>.lst
  Services/*.lst          -> output/domains/Services/<name><N>.lst
  Russia/inside-raw.lst   -> output/domains/Russia/inside-raw<N>.lst
  Subnets/IPv4/*.lst      -> output/subnets/<name><N>.bat

Скрипт полностью пересобирает output/ на каждом запуске (идемпотентно),
включая ситуацию, когда в источнике файл переименовали/удалили/добавили новый.
"""

import ipaddress
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC_REPO_URL = "https://github.com/itdoginfo/allow-domains.git"

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / ".src-allow-domains"
OUT_DIR = ROOT / "output"

DOMAIN_CHUNK_SIZE = 299   # у Keenetic лимит 300 строк на список доменов
SUBNET_CHUNK_SIZE = 1023  # у Keenetic лимит 1024 строк на список подсетей

DOMAIN_SOURCE_DIRS = {
    "Categories": "Categories",
    "Services": "Services",
}
RUSSIA_RAW_FILE = ("Russia", "inside-raw.lst", "inside-raw")
SUBNET_SOURCE_DIR = "Subnets/IPv4"


def log(msg: str) -> None:
    print(msg, flush=True)


def clone_source() -> None:
    if SRC_DIR.exists():
        shutil.rmtree(SRC_DIR)
    log(f"Клонирую {SRC_REPO_URL} ...")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", SRC_REPO_URL, str(SRC_DIR)],
        check=True,
    )


def read_list_lines(path: Path) -> list[str]:
    """Читает .lst файл, убирает пустые строки и комментарии, сохраняет порядок."""
    lines: list[str] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            lines.append(line)
    return lines

def normalize_domains(lines: list[str]) -> list[str]:
    """Keenetic не принимает домены с точкой в начале (.ua, .com и т.п.) —
    у itdoginfo такие записи встречаются как минимум в Russia/inside-raw.lst.
    Срезаем ведущие точки; если после этого строка пустая — выбрасываем её.
    Заодно убираем дубликаты, которые могли возникнуть из-за такой нормализации
    (например, если в списке одновременно были и ".ua", и "ua"), сохраняя порядок.
    """
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        domain = line.lstrip(".")
        if not domain or domain in seen:
            continue
        seen.add(domain)
        result.append(domain)
    return result

def chunk_list(items: list[str], size: int) -> list[list[str]]:
    if not items:
        return [[]]
    return [items[i : i + size] for i in range(0, len(items), size)]


def clear_generated(out_subdir: Path, stem: str, ext: str) -> None:
    if not out_subdir.exists():
        return
    for old in out_subdir.glob(f"{stem}[0-9]*{ext}"):
        old.unlink()


def write_domain_file(out_subdir: Path, stem: str, index: int, lines: list[str]) -> str:
    out_subdir.mkdir(parents=True, exist_ok=True)
    fname = f"{stem}{index}.lst"
    (out_subdir / fname).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fname


def cidr_to_route_line(cidr: str) -> str:
    net = ipaddress.ip_network(cidr, strict=False)
    return f"route ADD {net.network_address} MASK {net.netmask} 0.0.0.0"


def write_subnet_file(out_subdir: Path, stem: str, index: int, route_lines: list[str]) -> str:
    out_subdir.mkdir(parents=True, exist_ok=True)
    fname = f"{stem}{index}.bat"
    # CRLF — классический синтаксис .bat, так исторически ждёт парсер Keenetic
    content = "\r\n".join(route_lines) + "\r\n"
    (out_subdir / fname).write_text(content, encoding="utf-8")
    return fname


def process_domain_file(src_file: Path, stem: str, out_subdir: Path) -> dict:
    clear_generated(out_subdir, stem, ".lst")
    lines = normalize_domains(read_list_lines(src_file))
    files = []
    for idx, part in enumerate(chunk_list(lines, DOMAIN_CHUNK_SIZE)):
        fname = write_domain_file(out_subdir, stem, idx, part)
        files.append({"file": fname, "lines": len(part)})
    return {"source": str(src_file.relative_to(SRC_DIR)), "total_domains": len(lines), "files": files}


def process_subnet_file(src_file: Path, stem: str, out_subdir: Path) -> dict:
    clear_generated(out_subdir, stem, ".bat")
    cidrs = read_list_lines(src_file)
    route_lines = []
    skipped = []
    for c in cidrs:
        try:
            route_lines.append(cidr_to_route_line(c))
        except ValueError:
            skipped.append(c)
    files = []
    for idx, part in enumerate(chunk_list(route_lines, SUBNET_CHUNK_SIZE)):
        fname = write_subnet_file(out_subdir, stem, idx, part)
        files.append({"file": fname, "lines": len(part)})
    result = {
        "source": str(src_file.relative_to(SRC_DIR)),
        "total_subnets": len(route_lines),
        "files": files,
    }
    if skipped:
        result["skipped_invalid"] = skipped
    return result


def build_index_md(manifest: dict, repo_slug: str) -> str:
    branch = "main"
    base_raw = f"https://raw.githubusercontent.com/{repo_slug}/{branch}/output"

    lines = [
        "# Сгенерированные списки для Keenetic",
        "",
        f"Обновлено: {manifest['generated_at']} (UTC)",
        "",
        "Файл собран автоматически, не редактируйте руками — он будет перезаписан",
        "при следующем запуске workflow. Правьте `scripts/build.py`.",
        "",
        "## Списки доменов (Список доменных имён, до 300 строк)",
        "",
    ]
    for key in sorted(manifest["domains"]):
        entry = manifest["domains"][key]
        lines.append(f"### {key}  (`{entry['total_domains']}` доменов, источник `{entry['source']}`)")
        for f in entry["files"]:
            url = f"{base_raw}/domains/{key.rsplit('/', 1)[0]}/{f['file']}" if "/" in key else f"{base_raw}/domains/{f['file']}"
            lines.append(f"- `{f['file']}` ({f['lines']} строк) — {url}")
        lines.append("")

    lines += [
        "## Списки подсетей (Список адресов, .bat, до 1024 строк)",
        "",
    ]
    for key in sorted(manifest["subnets"]):
        entry = manifest["subnets"][key]
        lines.append(f"### {key}  (`{entry['total_subnets']}` подсетей, источник `{entry['source']}`)")
        for f in entry["files"]:
            url = f"{base_raw}/subnets/{f['file']}"
            lines.append(f"- `{f['file']}` ({f['lines']} строк) — {url}")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    clone_source()

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "domains": {},
        "subnets": {},
    }

    # Categories + Services
    for label, subdir in DOMAIN_SOURCE_DIRS.items():
        src_dir = SRC_DIR / subdir
        out_subdir = OUT_DIR / "domains" / label
        if not src_dir.exists():
            log(f"ВНИМАНИЕ: в источнике нет папки {subdir}, пропускаю")
            continue
        for src_file in sorted(src_dir.glob("*.lst")):
            stem = src_file.stem
            manifest["domains"][f"{label}/{stem}"] = process_domain_file(src_file, stem, out_subdir)

    # Russia/inside-raw.lst
    russia_dir, russia_fname, russia_stem = RUSSIA_RAW_FILE
    src_file = SRC_DIR / russia_dir / russia_fname
    if src_file.exists():
        out_subdir = OUT_DIR / "domains" / russia_dir
        manifest["domains"][f"{russia_dir}/{russia_stem}"] = process_domain_file(src_file, russia_stem, out_subdir)
    else:
        log(f"ВНИМАНИЕ: не найден {src_file}, пропускаю")

    # Subnets/IPv4
    src_dir = SRC_DIR / SUBNET_SOURCE_DIR
    out_subdir = OUT_DIR / "subnets"
    if src_dir.exists():
        for src_file in sorted(src_dir.glob("*.lst")):
            stem = src_file.stem
            manifest["subnets"][stem] = process_subnet_file(src_file, stem, out_subdir)
    else:
        log(f"ВНИМАНИЕ: не найдена папка {SUBNET_SOURCE_DIR}, пропускаю")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    repo_slug = os.environ.get("GITHUB_REPOSITORY", "YOUR_USER/YOUR_REPO")
    (OUT_DIR / "INDEX.md").write_text(build_index_md(manifest, repo_slug), encoding="utf-8")

    shutil.rmtree(SRC_DIR, ignore_errors=True)
    log("Готово.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        log(f"Ошибка при выполнении команды: {e}")
        sys.exit(1)
