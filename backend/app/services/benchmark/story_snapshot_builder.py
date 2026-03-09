"""故事快照构建服务。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter import Chapter


@dataclass
class BuiltStorySnapshot:
    project_id: str
    chapter_start: int
    chapter_end: int
    chapter_count: int
    source_mode: str
    content: str
    content_hash: str
    trigger_chapter_number: Optional[int] = None


class StorySnapshotBuilder:
    """把章节拼接成整书评测快照。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def build(
        self,
        project_id: str,
        scope: str = "latest_10",
        start_chapter: Optional[int] = None,
        end_chapter: Optional[int] = None,
        source_mode: str = "latest",
        trigger_chapter_number: Optional[int] = None,
    ) -> BuiltStorySnapshot:
        chapters = await self._load_chapters(project_id, scope, start_chapter, end_chapter, trigger_chapter_number)
        if not chapters:
            raise ValueError("当前范围内没有可评测的已完成章节")

        content_parts: List[str] = []
        for chapter in chapters:
            title = chapter.title or f"第{chapter.chapter_number}章"
            content = (chapter.content or "").strip()
            if not content:
                continue
            content_parts.append(f"## 第{chapter.chapter_number}章 {title}\n\n{content}")

        full_content = "\n\n".join(content_parts).strip()
        if not full_content:
            raise ValueError("当前范围内章节内容为空，无法生成快照")

        return BuiltStorySnapshot(
            project_id=project_id,
            chapter_start=chapters[0].chapter_number,
            chapter_end=chapters[-1].chapter_number,
            chapter_count=len(chapters),
            source_mode=source_mode,
            content=full_content,
            content_hash=hashlib.sha256(full_content.encode("utf-8")).hexdigest(),
            trigger_chapter_number=trigger_chapter_number,
        )

    async def _load_chapters(
        self,
        project_id: str,
        scope: str,
        start_chapter: Optional[int],
        end_chapter: Optional[int],
        trigger_chapter_number: Optional[int],
    ) -> List[Chapter]:
        result = await self.db.execute(
            select(Chapter)
            .where(Chapter.project_id == project_id)
            .where(Chapter.content.is_not(None))
            .where(Chapter.status == "completed")
            .order_by(Chapter.chapter_number)
        )
        all_chapters = [chapter for chapter in result.scalars().all() if (chapter.content or "").strip()]
        if not all_chapters:
            return []

        if scope == "project":
            return all_chapters

        if scope == "chapter_range":
            if start_chapter is None or end_chapter is None:
                raise ValueError("chapter_range 模式必须提供 start_chapter 和 end_chapter")
            return [
                chapter for chapter in all_chapters
                if start_chapter <= chapter.chapter_number <= end_chapter
            ]

        end_number = trigger_chapter_number or all_chapters[-1].chapter_number
        start_number = max(1, end_number - 9)
        return [
            chapter for chapter in all_chapters
            if start_number <= chapter.chapter_number <= end_number
        ]
