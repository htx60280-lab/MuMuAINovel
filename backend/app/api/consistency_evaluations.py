"""整书一致性评测 API。"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.consistency_evaluation import ConsistencyEvaluation, ConsistencyIssue
from app.models.project import Project
from app.schemas.consistency_evaluation import (
    ConsistencyEvaluationCreateRequest,
    ConsistencyEvaluationDetailResponse,
    ConsistencyEvaluationListResponse,
    ConsistencyIssueListResponse,
)
from app.services.benchmark.consistency_evaluation_service import ConsistencyEvaluationService


router = APIRouter(prefix="/consistency-evaluations", tags=["整书一致性评测"])


async def verify_project_access(project_id: str, user_id: str, db: AsyncSession) -> Project:
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user_id,
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在或无权访问")
    return project


@router.post("/projects/{project_id}", response_model=ConsistencyEvaluationDetailResponse, summary="创建整书一致性评测")
async def create_consistency_evaluation(
    project_id: str,
    payload: ConsistencyEvaluationCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    project = await verify_project_access(project_id, user_id, db)
    service = ConsistencyEvaluationService(db)
    evaluation = await service.create_evaluation(
        project=project,
        user_id=user_id,
        scope=payload.scope,
        start_chapter=payload.start_chapter,
        end_chapter=payload.end_chapter,
        snapshot_mode=payload.snapshot_mode,
        provider=payload.provider,
        model=payload.model,
        trigger_type=payload.trigger_type,
        trigger_chapter_number=payload.trigger_chapter_number,
    )
    background_tasks.add_task(service.run_evaluation, evaluation.id, user_id)
    detail = ConsistencyEvaluationDetailResponse.model_validate(evaluation)
    detail.issues = []
    return detail


@router.get("/projects/{project_id}", response_model=ConsistencyEvaluationListResponse, summary="获取项目评测历史")
async def list_project_consistency_evaluations(
    project_id: str,
    request: Request,
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    await verify_project_access(project_id, user_id, db)
    result = await db.execute(
        select(ConsistencyEvaluation)
        .where(ConsistencyEvaluation.project_id == project_id)
        .order_by(ConsistencyEvaluation.created_at.desc())
        .limit(limit)
    )
    items = result.scalars().all()
    return ConsistencyEvaluationListResponse(total=len(items), items=items)


@router.get("/{evaluation_id}", response_model=ConsistencyEvaluationDetailResponse, summary="获取评测详情")
async def get_consistency_evaluation_detail(
    evaluation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(select(ConsistencyEvaluation).where(ConsistencyEvaluation.id == evaluation_id))
    evaluation = result.scalar_one_or_none()
    if not evaluation:
        raise HTTPException(status_code=404, detail="评测任务不存在")

    await verify_project_access(evaluation.project_id, user_id, db)
    issues_result = await db.execute(
        select(ConsistencyIssue)
        .where(ConsistencyIssue.evaluation_id == evaluation_id)
        .order_by(ConsistencyIssue.created_at.desc())
    )
    detail = ConsistencyEvaluationDetailResponse.model_validate(evaluation)
    detail.issues = issues_result.scalars().all()
    return detail


@router.get("/{evaluation_id}/issues", response_model=ConsistencyIssueListResponse, summary="获取评测问题列表")
async def list_consistency_evaluation_issues(
    evaluation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    eval_result = await db.execute(select(ConsistencyEvaluation).where(ConsistencyEvaluation.id == evaluation_id))
    evaluation = eval_result.scalar_one_or_none()
    if not evaluation:
        raise HTTPException(status_code=404, detail="评测任务不存在")

    await verify_project_access(evaluation.project_id, user_id, db)
    issue_result = await db.execute(
        select(ConsistencyIssue)
        .where(ConsistencyIssue.evaluation_id == evaluation_id)
        .order_by(ConsistencyIssue.created_at.desc())
    )
    items = issue_result.scalars().all()
    return ConsistencyIssueListResponse(total=len(items), items=items)
