"""章节审查 API 路由 - 五维审查功能"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.api.common import verify_project_access
from app.models.chapter import Chapter
from app.services.critic_agent import CriticAgent
from app.services.ai_service import AIService
from app.api.settings import get_user_ai_service
from app.schemas.review import ReviewRequest, ReviewResponse, DimensionResultResponse
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/review", tags=["章节审查"])


@router.post("/chapter/{chapter_id}", response_model=ReviewResponse, summary="AI深度审查章节")
async def review_chapter(
    chapter_id: str,
    request: Request,
    review_request: ReviewRequest = ReviewRequest(),
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_user_ai_service)
):
    """
    对章节进行 AI 深度审查，分析四个维度：

    - **ooc**: 角色一致性检查 - 检测角色行为是否符合设定
    - **pacing**: 节奏分析 - 评估动作/对话/描写的比例和张力曲线
    - **high_point**: 爽点密度分析 - 识别期待感和释放感的设置
    - **consistency**: 设定一致性检查 - 检查是否与设定矛盾

    **优化**: 优先返回缓存的审查结果，避免重复审查。
    如需强制重新审查，请传入 force_refresh=true

    请求体参数：
    - dimensions: 可选，指定要审查的维度列表。不提供则审查全部维度
    - force_refresh: 可选，是否强制重新审查（忽略缓存）

    返回：
    - overall_score: 综合评分 (0-100)
    - dimensions: 各维度的详细审查结果
    - reviewed_at: 审查时间
    """
    try:
        user_id = getattr(request.state, 'user_id', None)

        # 验证章节存在
        chapter_result = await db.execute(
            select(Chapter).where(Chapter.id == chapter_id)
        )
        chapter = chapter_result.scalar_one_or_none()

        if not chapter:
            raise HTTPException(status_code=404, detail="章节不存在")

        if not chapter.content or chapter.content.strip() == '':
            raise HTTPException(status_code=400, detail="章节内容为空，无法审查")

        # 验证用户权限
        await verify_project_access(chapter.project_id, user_id, db)

        # 检查是否有缓存的审查结果（且不强制刷新）
        force_refresh = getattr(review_request, 'force_refresh', False)
        if chapter.review_result and not force_refresh:
            logger.info(f"📋 返回缓存的审查结果: chapter_id={chapter_id[:8]}")
            cached = chapter.review_result

            # 转换缓存结果为响应格式
            dimensions_response = {}
            for dim_name, dim_data in cached.get("dimensions", {}).items():
                dimensions_response[dim_name] = DimensionResultResponse(
                    dimension=dim_data.get("dimension", dim_name),
                    score=dim_data.get("score", 0),
                    analysis=dim_data.get("analysis", ""),
                    suggestions=dim_data.get("suggestions", []),
                    details=dim_data.get("details", {})
                )

            return ReviewResponse(
                chapter_id=chapter_id,
                overall_score=cached.get("overall_score", 0),
                dimensions=dimensions_response,
                reviewed_at=cached.get("reviewed_at", ""),
                metadata=cached.get("metadata", {"from_cache": True})
            )

        # 创建审查 Agent
        critic = CriticAgent(db=db, ai_service=user_ai_service)

        # 执行审查
        logger.info(f"🔍 开始审查章节: {chapter_id}, 维度: {review_request.dimensions or '全部'}")

        result = await critic.review_chapter(
            chapter_id=chapter_id,
            dimensions=review_request.dimensions
        )

        # 转换为响应格式
        dimensions_response = {}
        dimensions_cache = {}
        for dim_name, dim_result in result.dimensions.items():
            dimensions_response[dim_name] = DimensionResultResponse(
                dimension=dim_result.dimension,
                score=dim_result.score,
                analysis=dim_result.analysis,
                suggestions=dim_result.suggestions,
                details=dim_result.details
            )
            # 准备缓存数据
            dimensions_cache[dim_name] = {
                "dimension": dim_result.dimension,
                "score": dim_result.score,
                "analysis": dim_result.analysis,
                "suggestions": dim_result.suggestions,
                "details": dim_result.details
            }

        # 保存审查结果到缓存
        chapter.review_result = {
            "overall_score": result.overall_score,
            "dimensions": dimensions_cache,
            "reviewed_at": result.reviewed_at,
            "metadata": result.metadata
        }
        await db.commit()
        logger.info(f"💾 审查结果已缓存: chapter_id={chapter_id[:8]}")

        return ReviewResponse(
            chapter_id=result.chapter_id,
            overall_score=result.overall_score,
            dimensions=dimensions_response,
            reviewed_at=result.reviewed_at,
            metadata=result.metadata
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 章节审查失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"审查失败: {str(e)}")


@router.get("/dimensions", summary="获取可用的审查维度")
async def get_available_dimensions():
    """
    获取所有可用的审查维度及其说明
    """
    return {
        "dimensions": [
            {
                "id": "ooc",
                "name": "角色一致性",
                "description": "检测角色行为是否符合设定（OOC检测）",
                "icon": "UserOutlined"
            },
            {
                "id": "pacing",
                "name": "节奏分析",
                "description": "评估动作/对话/描写的比例和张力曲线",
                "icon": "LineChartOutlined"
            },
            {
                "id": "high_point",
                "name": "爽点密度",
                "description": "识别期待感和释放感的设置",
                "icon": "ThunderboltOutlined"
            },
            {
                "id": "consistency",
                "name": "设定一致性",
                "description": "检查地点/时间线/角色能力是否与设定矛盾",
                "icon": "SafetyCertificateOutlined"
            }
        ]
    }
