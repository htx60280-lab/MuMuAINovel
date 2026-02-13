"""关键事件 API 路由"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import Optional
from pydantic import BaseModel, Field

from app.database import get_db
from app.api.common import verify_project_access
from app.models.key_event import KeyEvent
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/key-events", tags=["关键事件"])


class KeyEventCreate(BaseModel):
    """创建关键事件请求"""
    project_id: str
    chapter_number: int
    event_type: str
    title: str
    description: Optional[str] = None
    importance: int = Field(default=3, ge=1, le=5)


class KeyEventResolve(BaseModel):
    """标记伏笔回收请求"""
    resolved_chapter: int


@router.get("/project/{project_id}", summary="获取项目的关键事件")
async def get_project_key_events(
    project_id: str,
    request: Request,
    event_type: Optional[str] = None,
    min_importance: Optional[int] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    获取项目的关键事件列表

    可选过滤参数：
    - event_type: 事件类型
    - min_importance: 最小重要程度 (1-5)
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    await verify_project_access(project_id, user_id, db)

    query = select(KeyEvent).where(KeyEvent.project_id == project_id)

    if event_type:
        query = query.where(KeyEvent.event_type == event_type)
    if min_importance:
        query = query.where(KeyEvent.importance >= min_importance)

    query = query.order_by(KeyEvent.chapter_number, desc(KeyEvent.importance))

    result = await db.execute(query)
    events = result.scalars().all()

    return {
        "project_id": project_id,
        "total": len(events),
        "events": [
            {
                "id": e.id,
                "chapter_number": e.chapter_number,
                "event_type": e.event_type,
                "title": e.title,
                "description": e.description,
                "related_entities": e.related_entities,
                "importance": e.importance,
                "is_resolved": e.is_resolved,
                "resolved_chapter": e.resolved_chapter,
                "created_at": e.created_at.isoformat() if e.created_at else None
            }
            for e in events
        ]
    }


@router.post("", summary="创建关键事件")
async def create_key_event(
    request: Request,
    data: KeyEventCreate,
    db: AsyncSession = Depends(get_db)
):
    """手动创建关键事件"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    await verify_project_access(data.project_id, user_id, db)

    event = KeyEvent(
        project_id=data.project_id,
        chapter_number=data.chapter_number,
        event_type=data.event_type,
        title=data.title,
        description=data.description,
        importance=data.importance
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    logger.info(f"📌 创建关键事件: {event.title} (chapter={data.chapter_number})")

    return {
        "id": event.id,
        "message": "关键事件创建成功"
    }


@router.post("/{event_id}/resolve", summary="标记伏笔已回收")
async def mark_event_resolved(
    event_id: str,
    request: Request,
    data: KeyEventResolve,
    db: AsyncSession = Depends(get_db)
):
    """标记伏笔类事件已回收"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(
        select(KeyEvent).where(KeyEvent.id == event_id)
    )
    event = result.scalar_one_or_none()

    if not event:
        raise HTTPException(status_code=404, detail="事件不存在")

    await verify_project_access(event.project_id, user_id, db)

    event.is_resolved = 1
    event.resolved_chapter = data.resolved_chapter
    await db.commit()

    logger.info(f"✅ 伏笔已回收: {event.title} (resolved_chapter={data.resolved_chapter})")

    return {
        "success": True,
        "message": "伏笔已标记为回收"
    }


@router.delete("/{event_id}", summary="删除关键事件")
async def delete_key_event(
    event_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """删除关键事件"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(
        select(KeyEvent).where(KeyEvent.id == event_id)
    )
    event = result.scalar_one_or_none()

    if not event:
        raise HTTPException(status_code=404, detail="事件不存在")

    await verify_project_access(event.project_id, user_id, db)

    await db.delete(event)
    await db.commit()

    logger.info(f"🗑️ 删除关键事件: {event.title}")

    return {
        "success": True,
        "message": "关键事件已删除"
    }
