"""整书一致性评测服务。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.settings import _resolve_task_ai_service_standalone
from app.database import get_engine
from app.logger import get_logger
from app.models.chapter import Chapter
from app.models.consistency_evaluation import ConsistencyEvaluation, ConsistencyIssue, StorySnapshot
from app.models.project import Project
from app.services.benchmark.constory_adapter import ConStoryAdapter
from app.services.benchmark.constory_parser import normalize_constory_result
from app.services.benchmark.story_snapshot_builder import StorySnapshotBuilder


logger = get_logger(__name__)


class ConsistencyEvaluationService:
    AUTO_TRIGGER_INTERVAL = 10

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_evaluation(
        self,
        project: Project,
        user_id: str,
        scope: str = "latest_10",
        start_chapter: Optional[int] = None,
        end_chapter: Optional[int] = None,
        snapshot_mode: str = "latest",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        trigger_type: str = "manual",
        trigger_chapter_number: Optional[int] = None,
    ) -> ConsistencyEvaluation:
        builder = StorySnapshotBuilder(self.db)
        built_snapshot = await builder.build(
            project_id=project.id,
            scope=scope,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            source_mode=snapshot_mode,
            trigger_chapter_number=trigger_chapter_number,
        )

        snapshot = StorySnapshot(
            project_id=project.id,
            user_id=user_id,
            chapter_start=built_snapshot.chapter_start,
            chapter_end=built_snapshot.chapter_end,
            chapter_count=built_snapshot.chapter_count,
            source_mode=built_snapshot.source_mode,
            content=built_snapshot.content,
            content_hash=built_snapshot.content_hash,
        )
        self.db.add(snapshot)
        await self.db.flush()

        evaluation = ConsistencyEvaluation(
            project_id=project.id,
            snapshot_id=snapshot.id,
            user_id=user_id,
            trigger_type=trigger_type,
            trigger_chapter_number=trigger_chapter_number,
            status="pending",
            provider=provider,
            model=model,
        )
        self.db.add(evaluation)
        await self.db.commit()
        await self.db.refresh(evaluation)
        return evaluation

    async def run_evaluation(self, evaluation_id: str, user_id: str) -> None:
        engine = await get_engine(user_id)
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with session_factory() as run_db:
            result = await run_db.execute(
                select(ConsistencyEvaluation, StorySnapshot)
                .join(StorySnapshot, StorySnapshot.id == ConsistencyEvaluation.snapshot_id)
                .where(ConsistencyEvaluation.id == evaluation_id)
            )
            row = result.first()
            if not row:
                logger.error(f"❌ 一致性评测任务不存在: {evaluation_id}")
                return

            evaluation, snapshot = row
            try:
                evaluation.status = "running"
                evaluation.started_at = datetime.now()
                await run_db.commit()

                ai_service = await _resolve_task_ai_service_standalone(
                    user_id=evaluation.user_id,
                    task_type="review",
                    db_session=run_db,
                )
                adapter = ConStoryAdapter(ai_service)
                raw_result = await adapter.evaluate(
                    story_text=snapshot.content,
                    provider=evaluation.provider,
                    model=evaluation.model,
                )
                parsed = normalize_constory_result(raw_result)

                await run_db.execute(delete(ConsistencyIssue).where(ConsistencyIssue.evaluation_id == evaluation.id))

                for issue in parsed["issues"]:
                    run_db.add(
                        ConsistencyIssue(
                            evaluation_id=evaluation.id,
                            category=issue["category"],
                            subcategory=issue.get("subcategory"),
                            severity=issue.get("severity") or "medium",
                            title=issue.get("title") or "一致性问题",
                            description=issue.get("description"),
                            exact_quote=issue.get("exact_quote"),
                            location_text=issue.get("location_text"),
                            chapter_number=issue.get("chapter_number"),
                            evidence_json=issue.get("evidence_json"),
                        )
                    )

                evaluation.overall_score = parsed["overall_score"]
                evaluation.issue_count = parsed["issue_count"]
                evaluation.summary_json = parsed["summary"]
                evaluation.raw_result_json = parsed["raw_result"]
                evaluation.status = "succeeded"
                evaluation.finished_at = datetime.now()
                evaluation.error_message = None
                await run_db.commit()
                logger.info(f"✅ 整书一致性评测完成: {evaluation_id}, score={evaluation.overall_score}, issues={evaluation.issue_count}")
            except Exception as error:
                await run_db.rollback()
                fail_result = await run_db.execute(select(ConsistencyEvaluation).where(ConsistencyEvaluation.id == evaluation_id))
                fail_evaluation = fail_result.scalar_one_or_none()
                if fail_evaluation:
                    fail_evaluation.status = "failed"
                    fail_evaluation.error_message = str(error)[:1000]
                    fail_evaluation.finished_at = datetime.now()
                    await run_db.commit()
                logger.error(f"❌ 整书一致性评测失败: {evaluation_id}, {error}", exc_info=True)

    async def should_auto_trigger(self, project_id: str, chapter_number: int) -> bool:
        if chapter_number <= 0 or chapter_number % self.AUTO_TRIGGER_INTERVAL != 0:
            return False

        chapter_count_result = await self.db.execute(
            select(func.count(Chapter.id))
            .where(Chapter.project_id == project_id)
            .where(Chapter.status == "completed")
            .where(Chapter.content.is_not(None))
        )
        completed_count = chapter_count_result.scalar_one() or 0
        if completed_count < self.AUTO_TRIGGER_INTERVAL:
            return False

        existing_result = await self.db.execute(
            select(ConsistencyEvaluation)
            .where(ConsistencyEvaluation.project_id == project_id)
            .where(ConsistencyEvaluation.trigger_type == "auto")
            .where(ConsistencyEvaluation.trigger_chapter_number == chapter_number)
            .order_by(ConsistencyEvaluation.created_at.desc())
        )
        existing = existing_result.scalar_one_or_none()
        return existing is None
