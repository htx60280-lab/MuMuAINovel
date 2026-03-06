"""导出指定项目的大纲为txt文件"""
import asyncio
import sys
from pathlib import Path
import json
import os

# 添加backend目录到Python路径
backend_path = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from sqlalchemy import select, Column, String, Text, Integer, DateTime, JSON
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

# 获取数据库URL（Docker部署时端口映射到5433）
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://novelforge:123456@localhost:5433/novelforge_db")

# 定义Base
Base = declarative_base()

# 定义简化的模型（仅用于查询）
class Project(Base):
    __tablename__ = "projects"
    id = Column(String(36), primary_key=True)
    user_id = Column(String(100))
    title = Column(String(200))
    description = Column(Text)
    theme = Column(Text)
    genre = Column(String(50))
    target_words = Column(Integer)
    current_words = Column(Integer)

class Outline(Base):
    __tablename__ = "outlines"
    id = Column(String(36), primary_key=True)
    project_id = Column(String(36))
    title = Column(String(200))
    content = Column(Text)
    structure = Column(Text)
    order_index = Column(Integer)


async def export_outline(project_title: str, output_file: str = None):
    """导出指定项目的大纲

    Args:
        project_title: 项目标题
        output_file: 输出文件路径，默认为 项目名_大纲.txt
    """
    # 创建数据库引擎
    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as session:
        # 查找项目
        result = await session.execute(
            select(Project).where(Project.title == project_title)
        )
        project = result.scalar_one_or_none()

        if not project:
            print(f"❌ 未找到项目: {project_title}")
            return

        print(f"✅ 找到项目: {project.title} (ID: {project.id})")

        # 获取大纲列表
        result = await session.execute(
            select(Outline)
            .where(Outline.project_id == project.id)
            .order_by(Outline.order_index)
        )
        outlines = result.scalars().all()

        if not outlines:
            print(f"⚠️ 项目 {project_title} 没有大纲")
            return

        print(f"📝 找到 {len(outlines)} 个大纲")

        # 生成输出文件名
        if not output_file:
            safe_title = "".join(c for c in project.title if c.isalnum() or c in (' ', '-', '_', '(', ')'))
            output_file = f"{safe_title}_大纲.txt"

        # 导出大纲
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(f"项目: {project.title}\n")
            f.write(f"描述: {project.description or '无'}\n")
            f.write(f"类型: {project.genre or '无'}\n")
            f.write(f"主题: {project.theme or '无'}\n")
            f.write(f"目标字数: {project.target_words or 0}\n")
            f.write(f"当前字数: {project.current_words}\n")
            f.write(f"大纲数量: {len(outlines)}\n")
            f.write("=" * 80 + "\n\n")

            for outline in outlines:
                f.write(f"【第 {outline.order_index} 章】 {outline.title}\n")
                f.write("-" * 80 + "\n")

                # 如果有structure字段，解析并显示详细信息
                if outline.structure:
                    try:
                        structure_data = json.loads(outline.structure)

                        # 显示摘要
                        summary = structure_data.get("summary") or structure_data.get("content", "")
                        if summary:
                            f.write(f"摘要:\n{summary}\n\n")

                        # 显示角色
                        characters = structure_data.get("characters", [])
                        if characters:
                            f.write(f"涉及角色: {', '.join(characters)}\n\n")

                        # 显示场景
                        scenes = structure_data.get("scenes", [])
                        if scenes:
                            f.write(f"场景:\n")
                            for i, scene in enumerate(scenes, 1):
                                f.write(f"  {i}. {scene}\n")
                            f.write("\n")

                        # 显示关键事件
                        key_events = structure_data.get("key_events", [])
                        if key_events:
                            f.write(f"关键事件:\n")
                            for i, event in enumerate(key_events, 1):
                                f.write(f"  {i}. {event}\n")
                            f.write("\n")

                        # 显示其他字段
                        for key in ["emotional_tone", "conflict_type", "narrative_goal"]:
                            if key in structure_data:
                                label = {
                                    "emotional_tone": "情感基调",
                                    "conflict_type": "冲突类型",
                                    "narrative_goal": "叙事目标"
                                }.get(key, key)
                                f.write(f"{label}: {structure_data[key]}\n")

                    except json.JSONDecodeError:
                        f.write(f"内容:\n{outline.content}\n\n")
                else:
                    # 没有structure时，显示content
                    f.write(f"内容:\n{outline.content}\n\n")

                f.write("\n")

        print(f"✅ 大纲已导出到: {output_file}")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python export_outline.py <项目名称> [输出文件]")
        print("示例: python export_outline.py '真的是天选之子(大概)'")
        sys.exit(1)

    project_title = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    asyncio.run(export_outline(project_title, output_file))
