#!/usr/bin/env python3
"""
arboris-novel → NovelForge 数据迁移脚本

从 arboris-novel 的 SQLite 数据库中读取指定项目数据，
转换为 NovelForge v1.1.0 导入 JSON 格式。

用法:
    python migrate_from_arboris.py [--db PATH] [--project-id ID] [--output PATH]
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# === 默认配置 ===
DEFAULT_DB_PATH = r"E:\github\arboris-novel\backend\storage\arboris_from_docker.db"
DEFAULT_PROJECT_ID = "96771c24-0f98-44be-a527-fb27cee9f1f1"
DEFAULT_OUTPUT = r"E:\github\NovelForge\scripts\arboris_import.json"


def dict_factory(cursor, row):
    """sqlite3 row → dict"""
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    return conn


# === 数据提取 ===

def fetch_project(conn, pid: str) -> dict:
    row = conn.execute(
        "SELECT * FROM novel_projects WHERE id = ?", (pid,)
    ).fetchone()
    if not row:
        sys.exit(f"错误: 找不到项目 {pid}")
    return row


def fetch_blueprint(conn, pid: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM novel_blueprints WHERE project_id = ?", (pid,)
    ).fetchone()


def fetch_characters(conn, pid: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM blueprint_characters WHERE project_id = ? ORDER BY position",
        (pid,),
    ).fetchall()


def fetch_relationships(conn, pid: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM blueprint_relationships WHERE project_id = ? ORDER BY position",
        (pid,),
    ).fetchall()


def fetch_outlines(conn, pid: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM chapter_outlines WHERE project_id = ? ORDER BY chapter_number",
        (pid,),
    ).fetchall()


def fetch_chapters_with_content(conn, pid: str) -> list[dict]:
    """获取章节及其选中版本的内容"""
    rows = conn.execute(
        """
        SELECT c.id, c.chapter_number, c.real_summary, c.status,
               c.word_count, c.selected_version_id, c.created_at,
               cv.content AS version_content, cv.version_label
        FROM chapters c
        LEFT JOIN chapter_versions cv ON cv.id = c.selected_version_id
        WHERE c.project_id = ?
        ORDER BY c.chapter_number
        """,
        (pid,),
    ).fetchall()
    return rows


def fetch_foreshadowings(conn, pid: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM foreshadowings WHERE project_id = ?", (pid,)
    ).fetchall()


def fetch_memories(conn, pid: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM project_memories WHERE project_id = ?", (pid,)
    ).fetchall()


# === 数据转换 ===

def parse_world_setting(raw) -> dict:
    """解析 world_setting JSON 字段"""
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw


def build_project_meta(project: dict, blueprint: dict | None) -> dict:
    """构建 NovelForge 项目元数据"""
    ws = parse_world_setting(blueprint.get("world_setting")) if blueprint else {}

    # 从 world_setting 中提取地点和氛围
    locations = ws.get("key_locations", [])
    location_text = "；".join(
        f"{loc['name']}：{loc['description']}" for loc in locations[:3]
    ) if locations else ""

    factions = ws.get("factions", [])
    faction_text = "；".join(
        f"{f['name']}：{f['description']}" for f in factions[:3]
    ) if factions else ""

    # 组合描述
    desc_parts = []
    if blueprint:
        if blueprint.get("one_sentence_summary"):
            desc_parts.append(blueprint["one_sentence_summary"])
        if blueprint.get("full_synopsis"):
            desc_parts.append(f"\n\n【剧情简介】\n{blueprint['full_synopsis']}")
    if project.get("initial_prompt"):
        desc_parts.append(f"\n\n【创作灵感】\n{project['initial_prompt']}")

    # 世界观规则
    world_rules = ws.get("core_rules", "")

    meta = {
        "title": project["title"],
        "description": "\n".join(desc_parts) if desc_parts else "",
        "genre": blueprint.get("genre", "") if blueprint else "",
        "theme": blueprint.get("tone", "") if blueprint else "",
        "status": "writing",
        "target_words": (blueprint.get("planned_chapters", 0) or 0) * 3000,
        "world_rules": world_rules,
        "world_location": location_text,
        "world_atmosphere": faction_text,
        "outline_mode": "one-to-one",
    }
    return meta


def build_characters(chars: list[dict]) -> list[dict]:
    """转换角色数据"""
    result = []
    for c in chars:
        # 组合 background: goals + abilities
        bg_parts = []
        if c.get("goals"):
            bg_parts.append(f"【目标】{c['goals']}")
        if c.get("abilities"):
            bg_parts.append(f"【能力】{c['abilities']}")
        if c.get("relationship_to_protagonist"):
            bg_parts.append(f"【与主角关系】{c['relationship_to_protagonist']}")

        result.append({
            "name": c["name"],
            "role_type": _map_identity_to_role(c.get("identity", "")),
            "personality": c.get("personality", ""),
            "background": "\n".join(bg_parts) if bg_parts else "",
            "appearance": "",  # arboris 没有外貌字段
        })
    return result


def _map_identity_to_role(identity: str) -> str:
    """将 arboris 的 identity 映射为 NovelForge 的 role_type"""
    if not identity:
        return "supporting"
    lower = identity
    if "男主角" in lower or "女主角" in lower:
        return "protagonist"
    if "反派" in lower or "邪修" in lower or "护法" in lower:
        return "antagonist"
    if "工具人" in lower:
        return "minor"
    # 发小、师妹、掌门、灵兽等都是 supporting
    return "supporting"


def build_relationships(rels: list[dict]) -> list[dict]:
    """转换角色关系"""
    return [
        {
            "source_name": r["character_from"],
            "target_name": r["character_to"],
            "relationship_name": "",  # arboris 没有关系类型字段
            "description": r.get("description", ""),
            "intimacy_level": 50,
            "status": "active",
        }
        for r in rels
    ]


def build_outlines(outlines: list[dict]) -> list[dict]:
    """转换大纲"""
    return [
        {
            "title": o["title"] or f"第{o['chapter_number']}章",
            "content": o.get("summary", ""),
            "order_index": o["chapter_number"],
        }
        for o in outlines
    ]


def build_chapters(
    chapters: list[dict], outlines: list[dict]
) -> list[dict]:
    """转换章节，关联大纲标题"""
    # 建立 chapter_number → outline title 映射
    outline_map = {o["chapter_number"]: o["title"] for o in outlines}

    result = []
    for ch in chapters:
        content = ch.get("version_content", "") or ""
        if not content and ch.get("status") == "waiting_for_confirm":
            continue  # 跳过无内容的待确认章节

        title = outline_map.get(ch["chapter_number"], f"第{ch['chapter_number']}章")
        result.append({
            "title": title,
            "content": content,
            "summary": ch.get("real_summary", ""),
            "chapter_number": ch["chapter_number"],
            "word_count": ch.get("word_count", 0) or len(content),
            "status": "draft",
            "outline_title": title,
            "created_at": ch.get("created_at", ""),
        })
    return result


def build_export_json(
    project: dict,
    blueprint: dict | None,
    characters: list[dict],
    relationships: list[dict],
    outlines: list[dict],
    chapters: list[dict],
) -> dict:
    """组装完整的 NovelForge v1.1.0 导入 JSON"""
    return {
        "version": "1.1.0",
        "export_time": datetime.now().isoformat(),
        "project": build_project_meta(project, blueprint),
        "characters": build_characters(characters),
        "outlines": build_outlines(outlines),
        "chapters": build_chapters(chapters, outlines),
        "relationships": build_relationships(relationships),
        "organizations": [],
        "organization_members": [],
        "writing_styles": [],
        "generation_history": [],
        "careers": [],
        "character_careers": [],
        "story_memories": [],
        "plot_analysis": [],
        "project_default_style": None,
    }


# === 主流程 ===

def main():
    parser = argparse.ArgumentParser(
        description="arboris-novel → NovelForge 数据迁移"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH, help="arboris SQLite 数据库路径"
    )
    parser.add_argument(
        "--project-id", default=DEFAULT_PROJECT_ID, help="要迁移的项目 ID"
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT, help="输出 JSON 文件路径"
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"错误: 数据库文件不存在 {db_path}")

    print(f"连接数据库: {db_path}")
    conn = connect_db(str(db_path))

    pid = args.project_id
    print(f"读取项目: {pid}")

    # 提取数据
    project = fetch_project(conn, pid)
    blueprint = fetch_blueprint(conn, pid)
    characters = fetch_characters(conn, pid)
    relationships = fetch_relationships(conn, pid)
    outlines = fetch_outlines(conn, pid)
    chapters = fetch_chapters_with_content(conn, pid)

    print(f"  项目: {project['title']}")
    print(f"  角色: {len(characters)} 个")
    print(f"  关系: {len(relationships)} 条")
    print(f"  大纲: {len(outlines)} 章")
    print(f"  章节: {len(chapters)} 章 (含内容)")

    # 转换
    export_data = build_export_json(
        project, blueprint, characters, relationships, outlines, chapters
    )

    # 统计
    ch_with_content = sum(
        1 for c in export_data["chapters"] if c.get("content")
    )
    total_words = sum(c.get("word_count", 0) for c in export_data["chapters"])

    print(f"\n转换完成:")
    print(f"  角色: {len(export_data['characters'])} 个")
    print(f"  关系: {len(export_data['relationships'])} 条")
    print(f"  大纲: {len(export_data['outlines'])} 章")
    print(f"  章节: {len(export_data['chapters'])} 章 ({ch_with_content} 章有内容)")
    print(f"  总字数: {total_words}")

    # 输出
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)

    print(f"\n已输出到: {output_path}")
    print(f"文件大小: {output_path.stat().st_size / 1024:.1f} KB")
    print("\n下一步: 在 NovelForge 中使用「导入项目」功能导入此 JSON 文件")

    conn.close()


if __name__ == "__main__":
    main()
