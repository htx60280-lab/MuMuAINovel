import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_SOURCE = Path("D:/E-drive-19437/anzhuang/220个提示词/220个/220个")


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def normalize_category(path: Path, root: Path) -> str:
    relative_parts = path.parent.relative_to(root).parts
    if not relative_parts:
        return "未分类"
    if relative_parts[0] == "续写" and len(relative_parts) > 1:
        return f"续写-{relative_parts[1]}"
    return relative_parts[-1]


def make_template_key(category: str, name: str) -> str:
    raw = f"{category}/{name}"
    short_hash = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8].upper()
    safe = re.sub(r"[^A-Za-z0-9]+", "_", name.upper()).strip("_")
    if not safe:
        safe = "PROMPT"
    key = f"EXT_{safe}_{short_hash}"
    return key[:100]


def build_templates(root: Path) -> list[dict]:
    templates = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".txt", ".md"}:
            continue
        content = read_text(path)
        if not content:
            continue
        category = normalize_category(path, root)
        name = path.stem.strip()
        template_key = make_template_key(category, name)
        templates.append(
            {
                "template_key": template_key,
                "template_name": name,
                "template_content": content,
                "description": f"来源: 220个提示词/{category}",
                "category": category,
                "parameters": "[]",
                "is_active": True,
                "is_customized": True,
                "system_content_hash": None,
            }
        )
    return templates


def load_selection(selection_path: Path) -> list[Path]:
    if not selection_path.exists():
        raise SystemExit(f"Selection file not found: {selection_path}")
    items: list[Path] = []
    for line in selection_path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        items.append(Path(item))
    return items


def build_selected_templates(root: Path, selected_files: list[Path]) -> list[dict]:
    templates: list[dict] = []
    for path in selected_files:
        if not path.exists() or not path.is_file():
            continue
        if path.suffix.lower() not in {".txt", ".md"}:
            continue
        content = read_text(path)
        if not content:
            continue
        category = normalize_category(path, root) if path.is_relative_to(root) else path.parent.name
        name = path.stem.strip()
        template_key = make_template_key(category, name)
        templates.append(
            {
                "template_key": template_key,
                "template_name": name,
                "template_content": content,
                "description": f"来源: 220个提示词/{category}",
                "category": category,
                "parameters": "[]",
                "is_active": True,
                "is_customized": True,
                "system_content_hash": None,
            }
        )
    return templates


def build_statistics(templates: list[dict]) -> dict:
    categories: dict[str, int] = {}
    for template in templates:
        category = template.get("category") or "未分类"
        categories[category] = categories.get(category, 0) + 1
    return {
        "total": len(templates),
        "categories": dict(sorted(categories.items(), key=lambda item: item[0])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build prompt template import JSON.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Prompt folder root")
    parser.add_argument("--selection", type=Path, default=None, help="Path to selection list")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("prompt_templates_import.json"),
        help="Output JSON file",
    )
    args = parser.parse_args()

    source = args.source
    if not source.exists():
        raise SystemExit(f"Source folder not found: {source}")

    if args.selection:
        selected_files = load_selection(args.selection)
        templates = build_selected_templates(source, selected_files)
    else:
        templates = build_templates(source)
    payload = {
        "templates": templates,
        "export_time": datetime.now(timezone.utc).isoformat(),
        "version": "2.0",
        "statistics": build_statistics(templates),
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Export complete: {args.output} (total={len(templates)})")


if __name__ == "__main__":
    main()
