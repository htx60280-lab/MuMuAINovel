"""章节管理API"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload
import json
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime
from asyncio import Queue, Lock

from app.database import get_db
from app.api.common import verify_project_access
from app.services.chapter_context_service import (
    OneToManyContextBuilder,
    OneToOneContextBuilder
)
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.outline import Outline
from app.models.character import Character
from app.models.career import Career, CharacterCareer
from app.models.relationship import CharacterRelationship, Organization, OrganizationMember
from app.models.generation_history import GenerationHistory
from app.models.writing_style import WritingStyle
from app.models.analysis_task import AnalysisTask
from app.models.memory import PlotAnalysis, StoryMemory
from app.models.batch_generation_task import BatchGenerationTask
from app.models.regeneration_task import RegenerationTask
from app.schemas.chapter import (
    ChapterCreate,
    ChapterUpdate,
    ChapterResponse,
    ChapterListResponse,
    AnalysisTaskStatusResponse,
    BatchAnalysisStatusRequest,
    BatchAnalysisStatusResponse,
    BatchAnalyzeUnanalyzedRequest,
    BatchAnalyzeUnanalyzedResponse,
    ChapterGenerateRequest,
    BatchGenerateRequest,
    BatchGenerateResponse,
    BatchGenerateStatusResponse,
    ExpansionPlanUpdate,
    PartialRegenerateRequest
)
from app.schemas.regeneration import (
    ChapterRegenerateRequest,
    RegenerationTaskResponse,
    RegenerationTaskStatus
)
from app.services.ai_service import AIService
from app.services.prompt_service import prompt_service, PromptService, WritingStyleManager
from app.services.plot_analyzer import PlotAnalyzer
from app.services.memory_service import memory_service
from app.services.foreshadow_service import foreshadow_service
from app.services.chapter_regenerator import ChapterRegenerator
from app.services.chapter_memory_service import ChapterMemoryService
from app.services.world_state_utils import normalize_world_state_structure, normalize_state_change_payload
from app.services.critic_agent import CriticAgent
from app.services.context_manager import ContextAgent
from app.services.chapter_guardrails import ChapterGuardrails, GuardrailResult
from app.services.benchmark.consistency_evaluation_service import ConsistencyEvaluationService
from app.logger import get_logger
from app.api.settings import get_user_ai_service, get_task_ai_service, _resolve_task_ai_service_standalone
from app.utils.sse_response import SSEResponse, create_sse_response
from app.config import settings

router = APIRouter(prefix="/chapters", tags=["章节管理"])
logger = get_logger(__name__)

# 全局数据库写入锁（每个用户一个锁，用于保护SQLite写入操作）
db_write_locks: dict[str, Lock] = {}


async def get_db_write_lock(user_id: str) -> Lock:
    """获取或创建用户的数据库写入锁"""
    if user_id not in db_write_locks:
        db_write_locks[user_id] = Lock()
        logger.debug(f"🔒 为用户 {user_id} 创建数据库写入锁")
    return db_write_locks[user_id]


async def _maybe_schedule_consistency_evaluation(
    db: AsyncSession,
    background_tasks: BackgroundTasks,
    project_id: str,
    user_id: str,
    chapter_number: int,
) -> None:
    """每新增 10 章自动触发一次整书一致性评测。"""

    service = ConsistencyEvaluationService(db)
    should_trigger = await service.should_auto_trigger(project_id=project_id, chapter_number=chapter_number)
    if not should_trigger:
        return

    project_result = await db.execute(select(Project).where(Project.id == project_id))
    project = project_result.scalar_one_or_none()
    if not project:
        return

    evaluation = await service.create_evaluation(
        project=project,
        user_id=user_id,
        scope="latest_10",
        start_chapter=max(1, chapter_number - 9),
        end_chapter=chapter_number,
        snapshot_mode="latest",
        trigger_type="auto",
        trigger_chapter_number=chapter_number,
    )
    background_tasks.add_task(service.run_evaluation, evaluation.id, user_id)
    logger.info(f"📚 已自动创建整书一致性评测任务: {evaluation.id}, 触发章节={chapter_number}")


def _merge_system_prompts(*parts: Optional[str]) -> Optional[str]:
    """合并多个 system_prompt 片段，自动忽略空值。"""
    merged = [part.strip() for part in parts if part and part.strip()]
    return "\n\n".join(merged) if merged else None


async def _enhance_prompt_with_context_agent(
    db_session: AsyncSession,
    base_prompt: str,
    project_id: str,
    chapter_number: int,
    chapter_outline: str,
    user_id: str,
    top_k: int = 5,
) -> Dict[str, Any]:
    """生成前注入 Context Agent 检索上下文。"""
    try:
        context_agent = ContextAgent(db=db_session, use_pgvector=True)
        context_result = await context_agent.build_generation_context(
            project_id=project_id,
            chapter_number=chapter_number,
            chapter_outline=chapter_outline or "本章剧情推进",
            user_id=user_id,
            top_k=top_k,
        )

        context_string = (context_result.context_string or "").strip()
        enhanced_prompt = base_prompt
        if context_string:
            enhanced_prompt = (
                f"{base_prompt}\n\n"
                f"【Context Agent 增强上下文（必须遵守）】\n"
                f"{context_string}"
            )

        return {
            "prompt": enhanced_prompt,
            "system_prompt": (context_result.system_prompt or "").strip(),
            "metadata": context_result.metadata or {},
            "enabled": bool(context_string or context_result.system_prompt),
        }
    except Exception as e:
        logger.warning(f"⚠️ Context Agent 注入失败，降级使用原提示词: {e}")
        return {
            "prompt": base_prompt,
            "system_prompt": "",
            "metadata": {},
            "enabled": False,
        }


async def _build_quick_review_data(
    db_session: AsyncSession,
    project: Project,
    chapter: Chapter,
    chapter_outline: str,
) -> Dict[str, Any]:
    """构建 CriticAgent 快速审查所需上下文数据。"""
    characters_result = await db_session.execute(
        select(Character).where(Character.project_id == project.id)
    )
    characters = characters_result.scalars().all()
    character_settings = "\n".join([
        f"- {c.name}: {c.personality or '无性格描述'}"
        for c in characters
    ]) if characters else "暂无角色设定"

    previous_summary = "无前文摘要"
    if chapter.chapter_number > 1:
        previous_summary_result = await db_session.execute(
            select(Chapter.summary)
            .where(Chapter.project_id == project.id)
            .where(Chapter.chapter_number == chapter.chapter_number - 1)
        )
        previous_summary = previous_summary_result.scalar_one_or_none() or "无前文摘要"

    recent_rows_result = await db_session.execute(
        select(Chapter.chapter_number, Chapter.title, Chapter.summary)
        .where(Chapter.project_id == project.id)
        .where(Chapter.chapter_number < chapter.chapter_number)
        .where(Chapter.summary.isnot(None))
        .order_by(desc(Chapter.chapter_number))
        .limit(3)
    )
    recent_rows = list(recent_rows_result.all())
    if recent_rows:
        previous_summaries = "\n".join([
            f"第{row.chapter_number}章《{row.title}》: {row.summary}"
            for row in reversed(recent_rows)
        ])
    else:
        previous_summaries = "无前文摘要"

    world_state = normalize_world_state_structure(project.world_state)
    inventory_value = world_state.get("inventory", [])
    if isinstance(inventory_value, list):
        inventory_text = ", ".join(inventory_value) or "无"
    else:
        inventory_text = str(inventory_value or "无")

    relationships_value = world_state.get("relationships", {})
    if isinstance(relationships_value, (dict, list)):
        relationships_text = json.dumps(relationships_value, ensure_ascii=False)
    else:
        relationships_text = str(relationships_value or "无")

    return {
        "chapter_number": chapter.chapter_number,
        "title": chapter.title or "",
        "summary": chapter_outline or "",
        "character_settings": character_settings,
        "previous_summary": previous_summary,
        "previous_summaries": previous_summaries,
        "world_time_period": getattr(project, "world_time_period", None) or "未设定",
        "world_location": getattr(project, "world_location", None) or "未设定",
        "world_rules": getattr(project, "world_rules", None) or "未设定",
        "current_location": world_state.get("current_location", "未知"),
        "inventory": inventory_text,
        "relationships": relationships_text,
    }


async def _apply_quality_gate_with_rewrite(
    db_session: AsyncSession,
    ai_service: AIService,
    project: Project,
    chapter: Chapter,
    chapter_outline: str,
    current_content: str,
    base_prompt: str,
    system_prompt: Optional[str],
    max_tokens: int,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """生成后执行质量门控，未通过时注入反馈自动重写。"""
    pass_threshold = float(settings.quality_gate_pass_threshold)
    max_rewrites = max(0, int(settings.quality_gate_max_retries))

    critic_agent = CriticAgent(db=db_session, ai_service=ai_service)
    review_data = await _build_quick_review_data(
        db_session=db_session,
        project=project,
        chapter=chapter,
        chapter_outline=chapter_outline,
    )

    rewrite_count = 0
    final_review: Dict[str, Any] = {
        "passed": True,
        "overall_score": 0,
        "feedback": "",
        "details": {},
    }

    for attempt in range(max_rewrites + 1):
        final_review = await critic_agent.quick_review_content(
            content=current_content,
            chapter_data=review_data,
            pass_threshold=pass_threshold,
        )
        if final_review.get("passed", True):
            break

        if attempt >= max_rewrites:
            break

        rewrite_count += 1
        feedback = final_review.get("feedback") or "请重点修复角色一致性和设定一致性问题。"
        rewrite_prompt = f"""请对下面章节进行完整重写，修复审查问题。

【原始创作任务】
{base_prompt}

【审查反馈（必须修复）】
{feedback}

【当前章节内容】
{current_content}

【重写要求】
1. 保留本章核心事件和剧情目标，不偏离原大纲
2. 优先修复角色行为一致性和设定一致性问题
3. 字数尽量保持在目标字数附近，可上下浮动15%
4. 直接输出重写后的章节正文，不要任何解释"""

        rewrite_response = await ai_service.generate_text(
            prompt=rewrite_prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            model=model,
            auto_mcp=False,
        )
        rewritten_content = (
            rewrite_response.get("content", "")
            if isinstance(rewrite_response, dict)
            else str(rewrite_response)
        ).strip()

        if not rewritten_content:
            logger.warning(f"⚠️ 质量门控重写返回空内容，停止后续重写: chapter={chapter.id[:8]}")
            break

        current_content = rewritten_content

    return {
        "content": current_content,
        "rewrite_count": rewrite_count,
        "passed": bool(final_review.get("passed", True)),
        "overall_score": float(final_review.get("overall_score", 0)),
        "review": final_review,
    }


async def _run_data_agent_pipeline(
    db_session: AsyncSession,
    ai_service: AIService,
    chapter: Chapter,
    content: str,
    characters_mentioned: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """生成后执行 Data Agent：切片向量化 + 状态提取 + 摘要钩子同步。"""
    try:
        service = ChapterMemoryService(db=db_session, ai_service=ai_service)
        return await service.process_and_store_chapter(
            chapter_id=chapter.id,
            content=content,
            chapter_number=chapter.chapter_number,
            project_id=chapter.project_id,
            characters_mentioned=characters_mentioned,
        )
    except Exception as e:
        logger.warning(f"⚠️ Data Agent 执行失败（不阻塞主流程）: {e}")
        return {
            "stored_count": 0,
            "state_change_log": None,
            "error": str(e),
        }


# ==================== 反幻觉/反遗忘辅助函数 ====================


def _extract_names_from_text(text: str, all_names: List[str]) -> set:
    """从文本中提取出现过的角色名（纯字符串匹配，不调用 AI）。"""
    if not text or not all_names:
        return set()
    return {name for name in all_names if name and len(name) >= 2 and name in text}


async def _build_character_lists(
    db_session: AsyncSession,
    project: Project,
    chapter_outline: str,
    chapter_number: int,
) -> Dict[str, List[str]]:
    """
    构建禁止角色名单和新角色名单。

    Returns:
        {
            "all_names": [...],
            "forbidden": [...],
            "new_characters": [...],
            "introduced": [...],
        }
    """
    # 项目全部角色
    chars_result = await db_session.execute(
        select(Character.name).where(
            Character.project_id == project.id,
            Character.is_organization == False,
        )
    )
    all_character_names = [row[0] for row in chars_result.all() if row[0]]

    if not all_character_names:
        return {"all_names": [], "forbidden": [], "new_characters": [], "introduced": []}

    # 大纲中提到的角色
    outline_text = chapter_outline or ""
    outline_mentioned = _extract_names_from_text(outline_text, all_character_names)

    # 已登场角色（从最近 N 章摘要中提取）
    recent_rows = await db_session.execute(
        select(Chapter.summary)
        .where(
            Chapter.project_id == project.id,
            Chapter.chapter_number < chapter_number,
            Chapter.summary.isnot(None),
        )
        .order_by(desc(Chapter.chapter_number))
        .limit(10)
    )
    recent_summaries_text = " ".join([r[0] for r in recent_rows.all() if r[0]])
    introduced = _extract_names_from_text(recent_summaries_text, all_character_names)

    # 第一章时，大纲提及的角色视为允许
    allowed = introduced | outline_mentioned
    forbidden = [n for n in all_character_names if n not in allowed]
    new_characters = [n for n in outline_mentioned if n not in introduced]

    return {
        "all_names": all_character_names,
        "forbidden": forbidden,
        "new_characters": new_characters,
        "introduced": list(introduced),
    }


async def _generate_director_plan(
    ai_service: AIService,
    previous_summary: str,
    chapter_outline: str,
    introduced_characters: str,
    new_characters: str,
    narrative_perspective: str,
    user_id: str = None,
    db_session: AsyncSession = None,
) -> Optional[Dict[str, Any]]:
    """
    调用 AI 生成导演预规划脚本。

    Returns:
        解析后的 JSON dict，或 None。
    """
    template = await PromptService.get_template("CHAPTER_DIRECTOR_PLAN", user_id, db_session) \
        if user_id and db_session else PromptService.CHAPTER_DIRECTOR_PLAN

    if not template:
        return None

    prompt = PromptService.format_prompt(
        template,
        previous_summary=previous_summary or "无",
        chapter_outline=chapter_outline or "无",
        introduced_characters=introduced_characters or "无",
        new_characters=new_characters or "无",
        narrative_perspective=narrative_perspective or "第三人称",
    )

    import json as _json

    max_attempts = 2
    token_limits = [2048, 4096]  # 第一次 2048，重试时加大

    for attempt in range(max_attempts):
        response = await ai_service.generate_text(
            prompt=prompt,
            system_prompt="你是一个章节导演，请严格按照 JSON 格式输出，不要输出多余内容。",
            max_tokens=token_limits[min(attempt, len(token_limits) - 1)],
        )
        response_text = response.get("content", "") if isinstance(response, dict) else str(response)

        # 检测截断（finish_reason=length 或 JSON 不完整）
        finish_reason = ""
        if isinstance(response, dict):
            finish_reason = response.get("finish_reason", "")

        try:
            if "{" in response_text and "}" in response_text:
                start = response_text.index("{")
                end = response_text.rindex("}") + 1
                plan = _json.loads(response_text[start:end])
                if isinstance(plan, dict):
                    logger.info(f"📋 导演预规划完成: pov={plan.get('pov', '?')}")
                    return plan
        except (_json.JSONDecodeError, ValueError):
            pass

        # JSON 解析失败，判断是否因为截断
        if finish_reason == "length" or ("{" in response_text and "}" not in response_text):
            logger.warning(
                f"⚠️ 导演预规划输出被截断 (attempt={attempt + 1}/{max_attempts}, "
                f"max_tokens={token_limits[min(attempt, len(token_limits) - 1)]}), 重试..."
            )
            continue

        break

    logger.warning("⚠️ 导演预规划 JSON 解析失败")
    return None


def _format_director_plan_block(director_plan: Dict[str, Any]) -> str:
    """将导演预规划结果格式化为可注入 system prompt 的文本块。"""
    forbidden_actions = director_plan.get("forbidden_actions", [])
    must_mention = director_plan.get("must_mention", [])
    return (
        f"\n【📋 章节导演脚本（必须遵守）】\n"
        f"- 主视角：{director_plan.get('pov', '未指定')}\n"
        f"- 开场切入：{director_plan.get('opening_beat', '未指定')}\n"
        f"- 核心事件：{director_plan.get('core_event', '未指定')}\n"
        f"- 情绪走向：{director_plan.get('emotion_arc', '未指定')}\n"
        f"- 禁止行为：{', '.join(forbidden_actions) if forbidden_actions else '无'}\n"
        f"- 必须提及：{', '.join(must_mention) if must_mention else '无'}\n"
    )


async def _apply_guardrails_check(
    ai_service: AIService,
    content: str,
    forbidden_names: List[str],
    new_characters: List[str],
    narrative_perspective: str,
    system_prompt: Optional[str],
    max_tokens: int,
    model: Optional[str] = None,
) -> str:
    """
    执行规则护栏检查，如有违规且开启自动重写则调用 AI 修复。

    Returns:
        修复后的内容（如无违规则返回原内容）。
    """
    if not settings.enable_guardrails:
        return content

    guardrails = ChapterGuardrails()
    result = guardrails.check(
        text=content,
        forbidden_characters=forbidden_names,
        new_characters=new_characters,
        narrative_perspective=narrative_perspective,
    )

    if result.passed:
        return content

    logger.warning(f"⚠️ 规则护栏检测到 {len(result.violations)} 个违规: {result.summary}")

    if not settings.guardrails_auto_rewrite:
        return content

    violations_text = guardrails.format_violations_for_rewrite(result)
    rewrite_prompt = f"""请修复以下违规问题，保持其余内容不变：

{violations_text}

【当前章节内容】
{content}

【修复要求】
1. 移除或替换禁止出现的角色名
2. 消除全知视角的表述，改为限知视角
3. 为新角色添加合理的介绍性描写
4. 保持核心情节和字数不变
直接输出修复后的完整章节正文。"""

    try:
        rewrite_response = await ai_service.generate_text(
            prompt=rewrite_prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            model=model,
            auto_mcp=False,
        )
        rewritten = (
            rewrite_response.get("content", "")
            if isinstance(rewrite_response, dict)
            else str(rewrite_response)
        ).strip()
        if rewritten:
            logger.info("✅ 规则护栏违规已自动修复")
            return rewritten
    except Exception as e:
        logger.warning(f"⚠️ 规则护栏重写失败（保留原内容）: {e}")

    return content


@router.post("", response_model=ChapterResponse, summary="创建章节")
async def create_chapter(
    chapter: ChapterCreate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """创建新的章节"""
    # 验证用户权限和项目是否存在
    user_id = getattr(request.state, 'user_id', None)
    project = await verify_project_access(chapter.project_id, user_id, db)
    
    # 计算字数(处理content可能为None的情况)
    word_count = len(chapter.content) if chapter.content else 0
    
    db_chapter = Chapter(
        **chapter.model_dump(),
        word_count=word_count
    )
    db.add(db_chapter)
    
    # 更新项目的当前字数
    project.current_words = project.current_words + word_count
    
    await db.commit()
    await db.refresh(db_chapter)
    return db_chapter


@router.get("/project/{project_id}", response_model=ChapterListResponse, summary="获取项目的所有章节")
async def get_project_chapters(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """获取指定项目的所有章节（带大纲信息）"""
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(project_id, user_id, db)
    
    # 获取总数
    count_result = await db.execute(
        select(func.count(Chapter.id)).where(Chapter.project_id == project_id)
    )
    total = count_result.scalar_one()
    
    # 获取章节列表，同时加载关联的大纲信息
    result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == project_id)
        .order_by(Chapter.chapter_number)
    )
    chapters = result.scalars().all()
    
    # 获取所有大纲信息（用于填充outline_title）
    outline_ids = [ch.outline_id for ch in chapters if ch.outline_id]
    outlines_map = {}
    if outline_ids:
        outlines_result = await db.execute(
            select(Outline).where(Outline.id.in_(outline_ids))
        )
        outlines_map = {o.id: o for o in outlines_result.scalars().all()}
    
    # 为所有章节添加大纲信息（统一处理）
    chapters_with_outline = []
    for chapter in chapters:
        chapter_dict = {
            "id": chapter.id,
            "project_id": chapter.project_id,
            "chapter_number": chapter.chapter_number,
            "title": chapter.title,
            "content": chapter.content,
            "summary": chapter.summary,
            "word_count": chapter.word_count,
            "status": chapter.status,
            "outline_id": chapter.outline_id,
            "sub_index": chapter.sub_index,
            "expansion_plan": chapter.expansion_plan,
            "end_hook": chapter.end_hook,
            "review_result": chapter.review_result,
            "pending_state_change": chapter.pending_state_change,
            "state_change_log": chapter.state_change_log,
            "created_at": chapter.created_at,
            "updated_at": chapter.updated_at,
        }
        
        # 添加大纲信息
        if chapter.outline_id and chapter.outline_id in outlines_map:
            outline = outlines_map[chapter.outline_id]
            chapter_dict["outline_title"] = outline.title
            chapter_dict["outline_order"] = outline.order_index
        else:
            chapter_dict["outline_title"] = None
            chapter_dict["outline_order"] = None
        
        chapters_with_outline.append(chapter_dict)
    
    return ChapterListResponse(total=total, items=chapters_with_outline)


@router.get("/{chapter_id}", response_model=ChapterResponse, summary="获取章节详情")
async def get_chapter(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """根据ID获取章节详情"""
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)

    if isinstance(chapter.state_change_log, dict):
        chapter.state_change_log = normalize_state_change_payload(chapter.state_change_log)
    
    return chapter


@router.get("/{chapter_id}/navigation", summary="获取章节导航信息")
async def get_chapter_navigation(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取章节的导航信息（上一章/下一章）
    用于章节阅读器的翻页功能
    """
    # 获取当前章节
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    current_chapter = result.scalar_one_or_none()
    
    if not current_chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(current_chapter.project_id, user_id, db)
    
    # 获取上一章
    prev_result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == current_chapter.project_id)
        .where(Chapter.chapter_number < current_chapter.chapter_number)
        .order_by(Chapter.chapter_number.desc())
        .limit(1)
    )
    prev_chapter = prev_result.scalar_one_or_none()
    
    # 获取下一章
    next_result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == current_chapter.project_id)
        .where(Chapter.chapter_number > current_chapter.chapter_number)
        .order_by(Chapter.chapter_number.asc())
        .limit(1)
    )
    next_chapter = next_result.scalar_one_or_none()
    
    return {
        "current": {
            "id": current_chapter.id,
            "chapter_number": current_chapter.chapter_number,
            "title": current_chapter.title
        },
        "previous": {
            "id": prev_chapter.id,
            "chapter_number": prev_chapter.chapter_number,
            "title": prev_chapter.title
        } if prev_chapter else None,
        "next": {
            "id": next_chapter.id,
            "chapter_number": next_chapter.chapter_number,
            "title": next_chapter.title
        } if next_chapter else None
    }


@router.put("/{chapter_id}", response_model=ChapterResponse, summary="更新章节")
async def update_chapter(
    chapter_id: str,
    chapter_update: ChapterUpdate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_user_ai_service)
):
    """更新章节信息"""
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 记录旧字数和旧内容
    old_word_count = chapter.word_count or 0
    old_content = chapter.content or ""
    
    # 更新字段
    update_data = chapter_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(chapter, field, value)
    
    # 如果内容更新了，重新计算字数（包括清空内容的情况）
    if "content" in update_data:
        new_word_count = len(chapter.content) if chapter.content else 0
        chapter.word_count = new_word_count
        content_changed = (chapter.content or "") != old_content
        
        # 更新项目字数
        result = await db.execute(
            select(Project).where(Project.id == chapter.project_id)
        )
        project = result.scalar_one_or_none()
        if project:
            project.current_words = project.current_words - old_word_count + new_word_count
        
        # 如果内容被清空，清理相关数据
        if not chapter.content or chapter.content.strip() == "":
            chapter.status = "draft"

            # 清理分析任务
            analysis_tasks_result = await db.execute(
                select(AnalysisTask).where(AnalysisTask.chapter_id == chapter_id)
            )
            analysis_tasks = analysis_tasks_result.scalars().all()
            for task in analysis_tasks:
                await db.delete(task)

            # 清理分析结果
            plot_analysis_result = await db.execute(
                select(PlotAnalysis).where(PlotAnalysis.chapter_id == chapter_id)
            )
            plot_analyses = plot_analysis_result.scalars().all()
            for analysis in plot_analyses:
                await db.delete(analysis)

            # 清理故事记忆（关系数据库）
            story_memories_result = await db.execute(
                select(StoryMemory).where(StoryMemory.chapter_id == chapter_id)
            )
            story_memories = story_memories_result.scalars().all()
            for memory in story_memories:
                await db.delete(memory)

            # 清理向量数据库中的记忆数据
            try:
                await memory_service.delete_chapter_memories(
                    user_id=user_id,
                    project_id=chapter.project_id,
                    chapter_id=chapter_id
                )
                logger.info(f"✅ 已清理章节 {chapter_id[:8]} 的向量记忆数据")
            except Exception as e:
                logger.warning(f"⚠️ 清理向量记忆数据失败: {str(e)}")

            # 🔮 清理章节相关的分析伏笔数据
            try:
                foreshadow_result = await foreshadow_service.delete_chapter_foreshadows(
                    db=db,
                    project_id=chapter.project_id,
                    chapter_id=chapter_id,
                    only_analysis_source=True  # 只删除分析来源的伏笔，保留手动创建的
                )
                if foreshadow_result['deleted_count'] > 0:
                    logger.info(f"🔮 已清理章节 {chapter_id[:8]} 的 {foreshadow_result['deleted_count']} 个伏笔数据")
            except Exception as e:
                logger.warning(f"⚠️ 清理伏笔数据失败: {str(e)}")

            logger.info(f"🗑️ 章节 {chapter_id[:8]} 内容已清空，已清理分析、记忆和伏笔数据")
        elif content_changed:
            # 内容被替换（非清空），后台重建向量数据
            _chapter_id = chapter.id
            _content = chapter.content
            _chapter_number = chapter.chapter_number
            _project_id = chapter.project_id
            _user_id = user_id

            async def _rebuild_vectors(
                ch_id: str, ch_content: str, ch_number: int, proj_id: str,
                bg_user_id: str, ai_svc: AIService
            ):
                """后台任务：删除旧向量并重新切片入库"""
                try:
                    from app.database import get_engine
                    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as BgAsyncSession
                    engine = await get_engine(bg_user_id)
                    BgSessionLocal = async_sessionmaker(
                        engine, class_=BgAsyncSession, expire_on_commit=False
                    )
                    async with BgSessionLocal() as bg_db:
                        service = ChapterMemoryService(db=bg_db, ai_service=ai_svc)
                        rebuild_result = await service.process_and_store_chapter(
                            chapter_id=ch_id,
                            content=ch_content,
                            chapter_number=ch_number,
                            project_id=proj_id,
                        )
                        logger.info(
                            f"📦 章节内容更新 - 向量重建完成: chapter={ch_id[:8]}, "
                            f"chunks={rebuild_result.get('stored_count', 0)}, "
                            f"state_change={'yes' if rebuild_result.get('state_change_log') else 'no'}"
                        )
                except Exception as e:
                    logger.warning(f"⚠️ 章节内容更新 - 向量重建失败（不影响保存）: {e}")

            background_tasks.add_task(
                _rebuild_vectors,
                _chapter_id, _content, _chapter_number, _project_id,
                _user_id, user_ai_service
            )
        else:
            logger.info(f"ℹ️ 章节内容未变化，跳过向量重建和状态重新提取: chapter={chapter_id[:8]}")
    
    await db.commit()
    await db.refresh(chapter)

    if "content" in update_data and (chapter.content or "").strip() and chapter.status == "completed":
        await _maybe_schedule_consistency_evaluation(
            db=db,
            background_tasks=background_tasks,
            project_id=chapter.project_id,
            user_id=user_id,
            chapter_number=chapter.chapter_number,
        )
    
    chapter_dict = {
        "id": chapter.id,
        "project_id": chapter.project_id,
        "chapter_number": chapter.chapter_number,
        "title": chapter.title,
        "content": chapter.content,
        "summary": chapter.summary,
        "word_count": chapter.word_count,
        "status": chapter.status,
        "outline_id": chapter.outline_id,
        "sub_index": chapter.sub_index,
        "expansion_plan": chapter.expansion_plan,
        "created_at": chapter.created_at,
        "updated_at": chapter.updated_at,
        "outline_title": None,
        "outline_order": None
    }
    
    # 如果章节关联了大纲，查询大纲信息
    if chapter.outline_id:
        outline_result = await db.execute(
            select(Outline).where(Outline.id == chapter.outline_id)
        )
        outline = outline_result.scalar_one_or_none()
        if outline:
            chapter_dict["outline_title"] = outline.title
            chapter_dict["outline_order"] = outline.order_index
    
    return chapter_dict


@router.delete("/{chapter_id}", summary="删除章节")
async def delete_chapter(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """删除章节"""
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 更新项目字数
    result = await db.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = result.scalar_one_or_none()
    if project:
        # 处理 word_count 和 current_words 可能为 None 的情况
        chapter_word_count = chapter.word_count or 0
        project.current_words = max(0, (project.current_words or 0) - chapter_word_count)
    
    # 🗑️ 清理向量数据库中的记忆数据
    try:
        await memory_service.delete_chapter_memories(
            user_id=user_id,
            project_id=chapter.project_id,
            chapter_id=chapter_id
        )
        logger.info(f"✅ 已清理章节 {chapter_id[:8]} 的向量记忆数据")
    except Exception as e:
        logger.warning(f"⚠️ 清理向量记忆数据失败: {str(e)}")
        # 不阻断删除流程，继续执行
    
    # 🔮 清理与该章节相关的伏笔数据（仅分析来源的伏笔）
    try:
        foreshadow_result = await foreshadow_service.delete_chapter_foreshadows(
            db=db,
            project_id=chapter.project_id,
            chapter_id=chapter_id,
            only_analysis_source=True  # 只删除分析来源的伏笔，保留手动创建的
        )
        if foreshadow_result['deleted_count'] > 0:
            logger.info(f"🔮 已清理章节 {chapter_id[:8]} 的 {foreshadow_result['deleted_count']} 个伏笔数据")
    except Exception as e:
        logger.warning(f"⚠️ 清理伏笔数据失败: {str(e)}")
        # 不阻断删除流程，继续执行
    
    # 删除章节（关系数据库中的记忆会被级联删除）
    await db.delete(chapter)
    await db.commit()
    
    return {"message": "章节删除成功"}


async def check_prerequisites(db: AsyncSession, chapter: Chapter) -> tuple[bool, str, list[Chapter]]:
    """
    检查章节前置条件
    
    Args:
        db: 数据库会话
        chapter: 当前章节
        
    Returns:
        (可否生成, 错误信息, 前置章节列表)
    """
    # 如果是第一章，无需检查前置
    if chapter.chapter_number == 1:
        return True, "", []
    
    # 查询所有前置章节（序号小于当前章节的）
    result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == chapter.project_id)
        .where(Chapter.chapter_number < chapter.chapter_number)
        .order_by(Chapter.chapter_number)
    )
    previous_chapters = result.scalars().all()
    
    # 检查是否所有前置章节都有内容
    incomplete_chapters = [
        ch for ch in previous_chapters
        if not ch.content or ch.content.strip() == ""
    ]
    
    if incomplete_chapters:
        missing_numbers = [str(ch.chapter_number) for ch in incomplete_chapters]
        error_msg = f"需要先完成前置章节：第 {', '.join(missing_numbers)} 章"
        return False, error_msg, previous_chapters
    
    return True, "", previous_chapters


async def build_characters_info_with_careers(
    db: AsyncSession,
    project_id: str,
    characters: list[Character],
    filter_character_names: Optional[list[str]] = None
) -> str:
    """
    构建包含职业信息的角色上下文
    
    Args:
        db: 数据库会话
        project_id: 项目ID
        characters: 角色列表
        filter_character_names: 可选，筛选特定角色名称列表（用于1-1模式的structure.characters或1-n模式的expansion_plan.character_focus）
        
    Returns:
        格式化的角色信息字符串，包含职业信息
    """
    if not characters:
        return '暂无角色信息'
    
    # 如果提供了筛选名单，只保留匹配的角色
    if filter_character_names:
        filtered_characters = [c for c in characters if c.name in filter_character_names]
        if not filtered_characters:
            logger.warning(f"筛选后无匹配角色，使用全部角色。筛选名单: {filter_character_names}")
            filtered_characters = characters
        else:
            logger.info(f"根据筛选名单保留 {len(filtered_characters)}/{len(characters)} 个角色: {[c.name for c in filtered_characters]}")
        characters = filtered_characters
    
    # 获取所有职业信息（一次性查询，提高效率）
    careers_result = await db.execute(
        select(Career).where(Career.project_id == project_id)
    )
    careers_map = {c.id: c for c in careers_result.scalars().all()}
    
    # 获取所有角色的职业关联（一次性查询）
    character_ids = [c.id for c in characters]
    if not character_ids:
        return '暂无角色信息'
    
    # 构建全局角色名称映射（用于关系显示）
    all_chars_result = await db.execute(
        select(Character.id, Character.name).where(Character.project_id == project_id)
    )
    all_char_name_map = {row.id: row.name for row in all_chars_result.all()}
        
    character_careers_result = await db.execute(
        select(CharacterCareer).where(CharacterCareer.character_id.in_(character_ids))
    )
    character_careers = character_careers_result.scalars().all()
    
    # 获取所有角色的关系（一次性查询）
    from sqlalchemy import or_
    rels_result = await db.execute(
        select(CharacterRelationship).where(
            CharacterRelationship.project_id == project_id,
            or_(
                CharacterRelationship.character_from_id.in_(character_ids),
                CharacterRelationship.character_to_id.in_(character_ids)
            )
        )
    )
    all_relationships = rels_result.scalars().all()
    
    # 按角色ID分组关系
    char_rels_map: dict[str, list] = {cid: [] for cid in character_ids}
    for r in all_relationships:
        if r.character_from_id in char_rels_map:
            char_rels_map[r.character_from_id].append(r)
        if r.character_to_id in char_rels_map:
            char_rels_map[r.character_to_id].append(r)
    
    # 获取所有组织及其成员关系（一次性查询）
    orgs_result = await db.execute(
        select(Organization).where(Organization.project_id == project_id)
    )
    all_orgs = orgs_result.scalars().all()
    
    # 构建组织ID到组织名称的映射（通过关联的Character记录）
    org_name_map = {}  # org_id -> org_name
    char_id_to_org = {}  # character_id -> Organization（用于组织实体补充详情）
    for org in all_orgs:
        org_name_map[org.id] = all_char_name_map.get(org.character_id, '未知组织')
        char_id_to_org[org.character_id] = org
    
    # 获取所有组织的成员关系（一次性查询）
    org_ids = [org.id for org in all_orgs]
    all_org_members = []
    if org_ids:
        all_org_members_result = await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id.in_(org_ids)
            )
        )
        all_org_members = all_org_members_result.scalars().all()
    
    # 按组织ID分组成员（用于组织实体显示成员列表）
    org_members_map: dict[str, list] = {oid: [] for oid in org_ids}
    for m in all_org_members:
        if m.organization_id in org_members_map:
            org_members_map[m.organization_id].append(m)
    
    # 获取涉及当前非组织角色的成员关系
    non_org_char_ids = [c.id for c in characters if not c.is_organization]
    char_org_map: dict[str, list] = {cid: [] for cid in non_org_char_ids}
    for m in all_org_members:
        if m.character_id in char_org_map:
            char_org_map[m.character_id].append(m)
    
    # 构建角色ID到职业信息的映射
    char_career_map = {}
    for cc in character_careers:
        if cc.character_id not in char_career_map:
            char_career_map[cc.character_id] = {'main': None, 'sub': []}
        
        career = careers_map.get(cc.career_id)
        if not career:
            continue
            
        career_info = {
            'name': career.name,
            'stage': cc.current_stage,
            'max_stage': career.max_stage,
            'stage_progress': cc.stage_progress
        }
        
        if cc.career_type == 'main':
            char_career_map[cc.character_id]['main'] = career_info
        else:
            char_career_map[cc.character_id]['sub'].append(career_info)
    
    # 构建角色信息字符串
    characters_info_parts = []
    for c in characters:
        # 基本信息（含存活状态标记）
        entity_type = '组织' if c.is_organization else '角色'
        status_marker = ""
        char_status = getattr(c, 'status', None) or 'active'
        if char_status != 'active':
            STATUS_MARKERS = {
                'deceased': '💀已死亡',
                'missing': '❓已失踪',
                'retired': '📤已退场',
                'destroyed': '💀已覆灭'
            }
            status_marker = f" [{STATUS_MARKERS.get(char_status, char_status)}]"
        base_info = f"- {c.name}({entity_type}, {c.role_type}){status_marker}"
        
        # 组织实体：补充组织详情
        org_detail_str = ""
        if c.is_organization and c.id in char_id_to_org:
            org = char_id_to_org[c.id]
            org_detail_parts = []
            if c.organization_type:
                org_detail_parts.append(f"类型:{c.organization_type}")
            if c.organization_purpose:
                purpose_preview = c.organization_purpose[:60] if len(c.organization_purpose) > 60 else c.organization_purpose
                org_detail_parts.append(f"宗旨:{purpose_preview}")
            if org.power_level is not None:
                org_detail_parts.append(f"势力等级:{org.power_level}")
            if org.location:
                org_detail_parts.append(f"据点:{org.location}")
            if org.motto:
                org_detail_parts.append(f"口号:{org.motto}")
            if org.member_count:
                org_detail_parts.append(f"成员数:{org.member_count}")
            if org_detail_parts:
                org_detail_str = f" | {', '.join(org_detail_parts)}"
            
            # 显示组织的核心成员列表（最多5个）
            if org.id in org_members_map and org_members_map[org.id]:
                member_parts = []
                for m in sorted(org_members_map[org.id], key=lambda x: -(x.rank or 0))[:5]:
                    m_name = all_char_name_map.get(m.character_id, '未知')
                    m_desc = f"{m_name}({m.position})"
                    if m.status and m.status != 'active':
                        m_desc += f"[{m.status}]"
                    member_parts.append(m_desc)
                if member_parts:
                    org_detail_str += f" | 成员: {', '.join(member_parts)}"
        
        # 职业信息
        career_info_str = ""
        if c.id in char_career_map:
            career_data = char_career_map[c.id]
            
            # 主职业
            if career_data['main']:
                main = career_data['main']
                stage_desc = f"{main['stage']}/{main['max_stage']}阶"
                career_info_str += f" | 主职业: {main['name']}({stage_desc})"
            
            # 副职业
            if career_data['sub']:
                sub_list = []
                for sub in career_data['sub']:
                    stage_desc = f"{sub['stage']}/{sub['max_stage']}阶"
                    sub_list.append(f"{sub['name']}({stage_desc})")
                career_info_str += f" | 副职业: {', '.join(sub_list)}"
        
        # 心理状态（由章节分析自动更新）
        state_str = ""
        if c.current_state:
            state_preview = c.current_state[:50] if len(c.current_state) > 50 else c.current_state
            state_str = f" | 当前状态: {state_preview}"
            if c.state_updated_chapter:
                state_str += f"(第{c.state_updated_chapter}章)"
        
        # 组织成员信息（非组织角色才显示所属组织）
        org_str = ""
        if not c.is_organization and c.id in char_org_map and char_org_map[c.id]:
            org_parts = []
            for m in char_org_map[c.id][:3]:  # 最多显示3个组织
                o_name = org_name_map.get(m.organization_id, '未知组织')
                o_desc = f"{o_name}({m.position})"
                if m.loyalty is not None and m.loyalty != 50:
                    o_desc += f"[忠诚度:{m.loyalty}]"
                if m.status and m.status != 'active':
                    o_desc += f"[{m.status}]"
                org_parts.append(o_desc)
            if org_parts:
                org_str = f" | 所属组织: {', '.join(org_parts)}"
        
        # 关系信息
        rel_str = ""
        if c.id in char_rels_map and char_rels_map[c.id]:
            rel_parts = []
            seen_pairs = set()  # 避免重复显示同一对关系
            for r in char_rels_map[c.id][:5]:  # 最多显示5个关系
                # 确定对方角色名
                if r.character_from_id == c.id:
                    other_name = all_char_name_map.get(r.character_to_id, '未知')
                else:
                    other_name = all_char_name_map.get(r.character_from_id, '未知')
                
                pair_key = tuple(sorted([c.id, r.character_from_id if r.character_from_id != c.id else r.character_to_id]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                
                rel_name = r.relationship_name or '关联'
                rel_desc = f"{other_name}({rel_name})"
                if r.intimacy_level is not None and r.intimacy_level != 50:
                    rel_desc += f"[亲密度:{r.intimacy_level}]"
                rel_parts.append(rel_desc)
            
            if rel_parts:
                rel_str = f" | 关系: {', '.join(rel_parts)}"
        
        # 性格描述
        personality_str = ""
        if c.personality:
            personality_preview = c.personality[:100] if len(c.personality) > 100 else c.personality
            personality_str = f": {personality_preview}"
        
        # 组合完整信息
        full_info = base_info + org_detail_str + career_info_str + state_str + org_str + rel_str + personality_str
        characters_info_parts.append(full_info)
    
    return "\n".join(characters_info_parts)


@router.get("/{chapter_id}/can-generate", summary="检查章节是否可以生成")
async def check_can_generate(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    检查章节是否满足生成条件
    返回可生成状态和前置章节信息
    """
    # 获取章节
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 检查前置条件
    can_generate, error_msg, previous_chapters = await check_prerequisites(db, chapter)
    
    # 构建前置章节信息
    previous_info = [
        {
            "id": ch.id,
            "chapter_number": ch.chapter_number,
            "title": ch.title,
            "has_content": bool(ch.content and ch.content.strip()),
            "word_count": ch.word_count or 0
        }
        for ch in previous_chapters
    ]
    
    return {
        "can_generate": can_generate,
        "reason": error_msg if not can_generate else "",
        "previous_chapters": previous_info,
        "chapter_number": chapter.chapter_number
    }


async def analyze_chapter_background(
    chapter_id: str,
    user_id: str,
    project_id: str,
    task_id: str,
    ai_service: AIService
) -> bool:
    """
    后台异步分析章节（支持并发，使用锁保护数据库写入）
    
    Args:
        chapter_id: 章节ID
        user_id: 用户ID
        project_id: 项目ID
        task_id: 任务ID
        ai_service: AI服务实例
        
    Returns:
        bool: True表示分析成功，False表示分析失败
    """
    db_session = None
    write_lock = await get_db_write_lock(user_id)
    
    try:
        logger.info(f"🔍 开始分析章节: {chapter_id}, 任务ID: {task_id}")
        
        # 创建独立数据库会话
        from app.database import get_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        
        engine = await get_engine(user_id)
        AsyncSessionLocal = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
        db_session = AsyncSessionLocal()
        
        # 1. 获取任务（读操作）
        task_result = await db_session.execute(
            select(AnalysisTask).where(AnalysisTask.id == task_id)
        )
        task = task_result.scalar_one_or_none()
        
        if not task:
            logger.error(f"❌ 任务不存在: {task_id}")
            return False
        
        # 更新任务状态（写操作，需要锁）
        async with write_lock:
            task.status = 'running'
            task.started_at = datetime.now()
            task.progress = 10
            await db_session.commit()
        
        # 2. 获取章节信息（读操作）
        chapter_result = await db_session.execute(
            select(Chapter).where(Chapter.id == chapter_id)
        )
        chapter = chapter_result.scalar_one_or_none()
        if not chapter or not chapter.content:
            async with write_lock:
                task.status = 'failed'
                task.error_message = '章节不存在或内容为空'
                task.completed_at = datetime.now()
                await db_session.commit()
            logger.error(f"❌ 章节不存在或内容为空: {chapter_id}")
            return False
        
        async with write_lock:
            task.progress = 20
            await db_session.commit()
        
        # 获取已埋入的伏笔列表（用于回收匹配，传入当前章节号以启用智能标记）
        existing_foreshadows = await foreshadow_service.get_planted_foreshadows_for_analysis(
            db=db_session,
            project_id=project_id,
            current_chapter_number=chapter.chapter_number  # 传入当前章节号以启用智能标记
        )
        logger.info(f"📋 后台分析 - 已获取{len(existing_foreshadows)}个已埋入伏笔用于匹配（含智能回收标记）")
        
        # 获取项目角色信息（根据大纲/展开规划筛选本章相关角色）
        filter_character_names = None
        
        # 1-N模式：从expansion_plan中提取character_focus
        if chapter.expansion_plan:
            try:
                plan = json.loads(chapter.expansion_plan)
                focus_names = plan.get('character_focus', [])
                if focus_names:
                    filter_character_names = focus_names
                    logger.info(f"📋 从expansion_plan提取角色焦点: {filter_character_names}")
            except (json.JSONDecodeError, Exception):
                pass
        
        # 1-1模式：从outline.structure中提取characters
        if not filter_character_names and chapter.outline_id:
            try:
                outline_result = await db_session.execute(
                    select(Outline).where(Outline.id == chapter.outline_id)
                )
                chapter_outline = outline_result.scalar_one_or_none()
                if chapter_outline and chapter_outline.structure:
                    structure = json.loads(chapter_outline.structure)
                    raw_characters = structure.get('characters', [])
                    if raw_characters:
                        filter_character_names = [
                            c['name'] if isinstance(c, dict) else c
                            for c in raw_characters
                        ]
                        logger.info(f"📋 从outline.structure提取角色: {filter_character_names}")
            except (json.JSONDecodeError, Exception):
                pass
        
        # 查询角色（根据筛选名单或全部）
        characters_query = select(Character).where(Character.project_id == project_id)
        if filter_character_names:
            characters_query = characters_query.where(Character.name.in_(filter_character_names))
        characters_result = await db_session.execute(characters_query)
        project_characters = characters_result.scalars().all()
        
        # 如果筛选后无角色，降级为全部角色
        if not project_characters and filter_character_names:
            logger.warning(f"⚠️ 筛选后无匹配角色，降级为全部角色")
            characters_result = await db_session.execute(
                select(Character).where(Character.project_id == project_id)
            )
            project_characters = characters_result.scalars().all()
            filter_character_names = None
        
        characters_info = await build_characters_info_with_careers(
            db=db_session,
            project_id=project_id,
            characters=project_characters,
            filter_character_names=filter_character_names
        )
        logger.info(f"📋 后台分析 - 已获取{len(project_characters)}个角色信息用于分析")
        
        # 定义重试回调函数，用于在重试时更新任务状态
        async def on_retry_callback(attempt: int, max_retries: int, wait_time: int, error_reason: str):
            """重试时更新任务状态，让前端能感知到重试进度"""
            try:
                async with write_lock:
                    # 重新获取任务（确保获取最新状态）
                    task_result_retry = await db_session.execute(
                        select(AnalysisTask).where(AnalysisTask.id == task_id)
                    )
                    task_retry = task_result_retry.scalar_one_or_none()
                    if task_retry:
                        # 更新任务状态，保持 running 但更新 started_at 以重置超时计时器
                        task_retry.status = 'running'
                        task_retry.started_at = datetime.now()  # 重置开始时间，防止超时检测误判
                        task_retry.progress = 25 + attempt * 5  # 根据重试次数更新进度
                        task_retry.error_message = f"正在重试({attempt}/{max_retries})：{error_reason[:100]}"
                        await db_session.commit()
                        logger.info(f"🔄 分析任务重试状态已更新: 尝试 {attempt}/{max_retries}, 等待 {wait_time}s, 原因: {error_reason[:50]}...")
            except Exception as callback_error:
                logger.warning(f"⚠️ 更新重试状态失败: {callback_error}")
        
        # 3. 使用PlotAnalyzer分析章节（传入已有伏笔列表、角色信息和重试回调）
        analyzer = PlotAnalyzer(ai_service)
        analysis_result = await analyzer.analyze_chapter(
            chapter_number=chapter.chapter_number,
            title=chapter.title,
            content=chapter.content,
            word_count=chapter.word_count or len(chapter.content),
            existing_foreshadows=existing_foreshadows,
            on_retry=on_retry_callback,
            characters_info=characters_info
        )
        
        if not analysis_result:
            async with write_lock:
                task.status = 'failed'
                task.error_message = 'AI分析失败，请检查日志'
                task.completed_at = datetime.now()
                await db_session.commit()
            logger.error(f"❌ AI分析失败: {chapter_id}")
            return False
        
        async with write_lock:
            task.progress = 60
            await db_session.commit()

        # 3.5 运行 CriticAgent 三维度评审（ooc, consistency, three_line_rhythm）
        critic_result = None
        try:
            project_result = await db_session.execute(
                select(Project).where(Project.id == project_id)
            )
            project = project_result.scalar_one_or_none()

            if project:
                outline_text = ""
                if chapter.outline_id:
                    outline_result = await db_session.execute(
                        select(Outline).where(Outline.id == chapter.outline_id)
                    )
                    outline_obj = outline_result.scalar_one_or_none()
                    if outline_obj:
                        outline_text = outline_obj.content or outline_obj.title or ""

                review_data = await _build_quick_review_data(
                    db_session=db_session,
                    project=project,
                    chapter=chapter,
                    chapter_outline=outline_text,
                )
                review_data["content"] = chapter.content

                critic_agent = CriticAgent(db=db_session, ai_service=ai_service)
                critic_result = await critic_agent.quick_review_content(
                    content=chapter.content,
                    chapter_data=review_data,
                    pass_threshold=0,  # 分析模式不做门控，只需要评分
                )
                logger.info(f"📊 CriticAgent 分析完成: overall={critic_result.get('overall_score', 0):.1f}")
        except Exception as critic_error:
            logger.warning(f"⚠️ CriticAgent 分析失败（不影响主分析流程）: {critic_error}")
            critic_result = None

        # 4. 保存分析结果到数据库（写操作，需要锁）
        async with write_lock:
            existing_analysis_result = await db_session.execute(
                select(PlotAnalysis).where(PlotAnalysis.chapter_id == chapter_id)
            )
            existing_analysis = existing_analysis_result.scalar_one_or_none()
            
            if existing_analysis:
                # 更新现有记录
                logger.info(f"  更新现有分析记录: {existing_analysis.id}")
                existing_analysis.plot_stage = analysis_result.get('plot_stage', '发展')
                existing_analysis.conflict_level = analysis_result.get('conflict', {}).get('level', 0)
                existing_analysis.conflict_types = analysis_result.get('conflict', {}).get('types', [])
                existing_analysis.emotional_tone = analysis_result.get('emotional_arc', {}).get('primary_emotion', '')
                existing_analysis.emotional_intensity = analysis_result.get('emotional_arc', {}).get('intensity', 0) / 10.0
                existing_analysis.hooks = analysis_result.get('hooks', [])
                existing_analysis.hooks_count = len(analysis_result.get('hooks', []))
                existing_analysis.foreshadows = analysis_result.get('foreshadows', [])
                existing_analysis.foreshadows_planted = sum(1 for f in analysis_result.get('foreshadows', []) if f.get('type') == 'planted')
                existing_analysis.foreshadows_resolved = sum(1 for f in analysis_result.get('foreshadows', []) if f.get('type') == 'resolved')
                existing_analysis.plot_points = analysis_result.get('plot_points', [])
                existing_analysis.plot_points_count = len(analysis_result.get('plot_points', []))
                existing_analysis.character_states = analysis_result.get('character_states', [])
                existing_analysis.scenes = analysis_result.get('scenes', [])
                existing_analysis.pacing = analysis_result.get('pacing', 'moderate')
                existing_analysis.overall_quality_score = analysis_result.get('scores', {}).get('overall', 0)
                existing_analysis.pacing_score = analysis_result.get('scores', {}).get('pacing', 0)
                existing_analysis.engagement_score = analysis_result.get('scores', {}).get('engagement', 0)
                existing_analysis.coherence_score = analysis_result.get('scores', {}).get('coherence', 0)
                existing_analysis.analysis_report = analyzer.generate_analysis_summary(analysis_result)
                existing_analysis.suggestions = analysis_result.get('suggestions', [])
                existing_analysis.dialogue_ratio = analysis_result.get('dialogue_ratio', 0)
                existing_analysis.description_ratio = analysis_result.get('description_ratio', 0)
                # 保存 CriticAgent 评分
                if critic_result and critic_result.get("details"):
                    details = critic_result["details"]
                    existing_analysis.ooc_score = details.get("ooc", {}).get("score", 0)
                    existing_analysis.consistency_score = details.get("consistency", {}).get("score", 0)
                    existing_analysis.three_line_rhythm_score = details.get("three_line_rhythm", {}).get("score", 0)
                    existing_analysis.critic_details = {
                        dim: {
                            "score": info.get("score", 0),
                            "analysis": info.get("analysis", ""),
                            "suggestions": info.get("suggestions", []),
                        }
                        for dim, info in details.items()
                        if dim != "error"
                    }
            else:
                # 创建新记录
                logger.info(f"  创建新的分析记录")
                plot_analysis = PlotAnalysis(
                    chapter_id=chapter_id,
                    project_id=project_id,
                    plot_stage=analysis_result.get('plot_stage', '发展'),
                    conflict_level=analysis_result.get('conflict', {}).get('level', 0),
                    conflict_types=analysis_result.get('conflict', {}).get('types', []),
                    emotional_tone=analysis_result.get('emotional_arc', {}).get('primary_emotion', ''),
                    emotional_intensity=analysis_result.get('emotional_arc', {}).get('intensity', 0) / 10.0,
                    hooks=analysis_result.get('hooks', []),
                    hooks_count=len(analysis_result.get('hooks', [])),
                    foreshadows=analysis_result.get('foreshadows', []),
                    foreshadows_planted=sum(1 for f in analysis_result.get('foreshadows', []) if f.get('type') == 'planted'),
                    foreshadows_resolved=sum(1 for f in analysis_result.get('foreshadows', []) if f.get('type') == 'resolved'),
                    plot_points=analysis_result.get('plot_points', []),
                    plot_points_count=len(analysis_result.get('plot_points', [])),
                    character_states=analysis_result.get('character_states', []),
                    scenes=analysis_result.get('scenes', []),
                    pacing=analysis_result.get('pacing', 'moderate'),
                    overall_quality_score=analysis_result.get('scores', {}).get('overall', 0),
                    pacing_score=analysis_result.get('scores', {}).get('pacing', 0),
                    engagement_score=analysis_result.get('scores', {}).get('engagement', 0),
                    coherence_score=analysis_result.get('scores', {}).get('coherence', 0),
                    analysis_report=analyzer.generate_analysis_summary(analysis_result),
                    suggestions=analysis_result.get('suggestions', []),
                    dialogue_ratio=analysis_result.get('dialogue_ratio', 0),
                    description_ratio=analysis_result.get('description_ratio', 0),
                    ooc_score=critic_result.get("details", {}).get("ooc", {}).get("score", 0) if critic_result and critic_result.get("details") else None,
                    consistency_score=critic_result.get("details", {}).get("consistency", {}).get("score", 0) if critic_result and critic_result.get("details") else None,
                    three_line_rhythm_score=critic_result.get("details", {}).get("three_line_rhythm", {}).get("score", 0) if critic_result and critic_result.get("details") else None,
                    critic_details={
                        dim: {
                            "score": info.get("score", 0),
                            "analysis": info.get("analysis", ""),
                            "suggestions": info.get("suggestions", []),
                        }
                        for dim, info in critic_result["details"].items()
                        if dim != "error"
                    } if critic_result and critic_result.get("details") else None,
                )
                db_session.add(plot_analysis)
            
            await db_session.commit()

            # 4.5 从分析评分生成 review_result（替代独立审查功能）
            try:
                scores = analysis_result.get('scores', {})
                suggestions = analysis_result.get('suggestions', [])

                # 将 1-10 分制转换为 0-100 分制
                pacing_score = min(100, int(scores.get('pacing', 0) * 10))
                engagement_score = min(100, int(scores.get('engagement', 0) * 10))
                coherence_score = min(100, int(scores.get('coherence', 0) * 10))
                overall_score = min(100, int(scores.get('overall', 0) * 10))

                score_justification = scores.get('score_justification', '')

                critic_details = critic_result.get("details", {}) if critic_result else {}

                review_result = {
                    "overall_score": overall_score,
                    "dimensions": {
                        "pacing": {
                            "dimension": "节奏分析",
                            "score": pacing_score,
                            "analysis": score_justification if score_justification else f"节奏评分 {pacing_score}/100",
                            "suggestions": suggestions[:3] if suggestions else [],
                            "details": {}
                        },
                        "engagement": {
                            "dimension": "吸引力",
                            "score": engagement_score,
                            "analysis": f"吸引力评分 {engagement_score}/100",
                            "suggestions": [],
                            "details": {}
                        },
                        "coherence": {
                            "dimension": "连贯性",
                            "score": coherence_score,
                            "analysis": f"连贯性评分 {coherence_score}/100",
                            "suggestions": [],
                            "details": {}
                        },
                        "ooc": {
                            "dimension": "角色一致性",
                            "score": critic_details.get("ooc", {}).get("score", 0),
                            "analysis": critic_details.get("ooc", {}).get("analysis", "未执行"),
                            "suggestions": critic_details.get("ooc", {}).get("suggestions", []),
                            "details": {}
                        },
                        "consistency": {
                            "dimension": "设定一致性",
                            "score": critic_details.get("consistency", {}).get("score", 0),
                            "analysis": critic_details.get("consistency", {}).get("analysis", "未执行"),
                            "suggestions": critic_details.get("consistency", {}).get("suggestions", []),
                            "details": {}
                        },
                        "three_line_rhythm": {
                            "dimension": "三线节奏",
                            "score": critic_details.get("three_line_rhythm", {}).get("score", 0),
                            "analysis": critic_details.get("three_line_rhythm", {}).get("analysis", "未执行"),
                            "suggestions": critic_details.get("three_line_rhythm", {}).get("suggestions", []),
                            "details": {}
                        },
                    },
                    "reviewed_at": datetime.now().isoformat(),
                    "metadata": {
                        "source": "analysis",
                        "word_count": chapter.word_count or len(chapter.content or ""),
                        "has_critic_review": critic_result is not None,
                    }
                }

                chapter.review_result = review_result
                await db_session.commit()
                logger.info(f"📊 已从分析评分生成 review_result: overall={overall_score}")
            except Exception as review_build_error:
                logger.warning(f"⚠️ 生成 review_result 失败（不影响分析流程）: {review_build_error}")

            task.progress = 80
            await db_session.commit()

        # 5. 清理旧的分析伏笔（重新分析时需要先清理）
        try:
            async with write_lock:
                clean_result = await foreshadow_service.clean_chapter_analysis_foreshadows(
                    db=db_session,
                    project_id=project_id,
                    chapter_id=chapter_id
                )
            if clean_result['cleaned_count'] > 0:
                logger.info(f"🧹 重新分析前清理了 {clean_result['cleaned_count']} 个旧伏笔")
        except Exception as clean_error:
            logger.warning(f"⚠️ 清理旧伏笔失败（继续分析）: {str(clean_error)}")
        
        # 6. 提取记忆并保存到向量数据库（传入章节内容用于计算位置）
        memories = analyzer.extract_memories_from_analysis(
            analysis=analysis_result,
            chapter_id=chapter_id,
            chapter_number=chapter.chapter_number,
            chapter_content=chapter.content or "",
            chapter_title=chapter.title or ""
        )
        
        # 先删除该章节的旧记忆（写操作，需要锁）
        async with write_lock:
            old_memories_result = await db_session.execute(
                select(StoryMemory).where(StoryMemory.chapter_id == chapter_id)
            )
            old_memories = old_memories_result.scalars().all()
            for old_mem in old_memories:
                await db_session.delete(old_mem)
            await db_session.commit()
            logger.info(f"  删除旧记忆: {len(old_memories)}条")
        
        # 准备批量添加的记忆数据（不需要锁）
        memory_records = []
        for mem in memories:
            memory_id = f"{chapter_id}_{mem['type']}_{len(memory_records)}"
            memory_records.append({
                'id': memory_id,
                'content': mem['content'],
                'type': mem['type'],
                'metadata': mem['metadata']
            })
            
        # 保存到关系数据库（写操作，需要锁）
        async with write_lock:
            for mem in memories:
                memory_id = memory_records[memories.index(mem)]['id']
                text_position = mem['metadata'].get('text_position', -1)
                text_length = mem['metadata'].get('text_length', 0)
                
                story_memory = StoryMemory(
                    id=memory_id,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    memory_type=mem['type'],
                    content=mem['content'],
                    title=mem['title'],
                    importance_score=mem['metadata'].get('importance_score', 0.5),
                    tags=mem['metadata'].get('tags', []),
                    is_foreshadow=mem['metadata'].get('is_foreshadow', 0),
                    story_timeline=chapter.chapter_number,
                    chapter_position=text_position,
                    text_length=text_length,
                    related_characters=mem['metadata'].get('related_characters', []),
                    related_locations=mem['metadata'].get('related_locations', [])
                )
                db_session.add(story_memory)
                
                if text_position >= 0:
                    logger.debug(f"  保存记忆 {memory_id}: position={text_position}, length={text_length}")
            
            await db_session.commit()
        
        # 批量添加到向量数据库
        if memory_records:
            added_count = await memory_service.batch_add_memories(
                user_id=user_id,
                project_id=project_id,
                memories=memory_records
            )
            logger.info(f"✅ 添加{added_count}条记忆到向量库")
        
        # 💼 更新角色职业（根据分析结果）
        if analysis_result.get('character_states'):
            try:
                from app.services.career_update_service import CareerUpdateService
                
                logger.info(f"💼 开始根据分析结果更新角色职业...")
                career_update_result = await CareerUpdateService.update_careers_from_analysis(
                    db=db_session,
                    project_id=project_id,
                    character_states=analysis_result.get('character_states', []),
                    chapter_id=chapter_id,
                    chapter_number=chapter.chapter_number
                )
                
                if career_update_result['updated_count'] > 0:
                    logger.info(
                        f"✅ 更新了 {career_update_result['updated_count']} 个角色的职业信息"
                    )
                    if career_update_result['changes']:
                        for change in career_update_result['changes']:
                            logger.info(f"  - {change}")
                else:
                    logger.info("ℹ️ 本章节无角色职业变化")
                    
            except Exception as career_error:
                # 职业更新失败不应影响整个分析流程
                logger.error(f"⚠️ 更新角色职业失败: {str(career_error)}", exc_info=True)
        else:
            logger.debug("📋 分析结果中无角色状态信息，跳过职业更新")
        
        # 👤 更新角色心理状态和关系（根据分析结果）
        if analysis_result.get('character_states'):
            try:
                from app.services.character_state_update_service import CharacterStateUpdateService
                
                logger.info(f"👤 开始根据分析结果更新角色状态、关系和组织成员...")
                async with write_lock:
                    state_update_result = await CharacterStateUpdateService.update_from_analysis(
                        db=db_session,
                        project_id=project_id,
                        character_states=analysis_result.get('character_states', []),
                        chapter_id=chapter_id,
                        chapter_number=chapter.chapter_number
                    )
                
                total_state_changes = (
                    state_update_result['state_updated_count'] +
                    state_update_result['relationship_created_count'] +
                    state_update_result['relationship_updated_count'] +
                    state_update_result.get('org_updated_count', 0)
                )
                if total_state_changes > 0:
                    logger.info(
                        f"✅ 角色状态更新: 心理状态{state_update_result['state_updated_count']}个, "
                        f"新建关系{state_update_result['relationship_created_count']}个, "
                        f"更新关系{state_update_result['relationship_updated_count']}个, "
                        f"组织变动{state_update_result.get('org_updated_count', 0)}个"
                    )
                    if state_update_result['changes']:
                        for change in state_update_result['changes'][:8]:
                            logger.info(f"  - {change}")
                else:
                    logger.info("ℹ️ 本章节无角色状态、关系或组织变化")
                    
            except Exception as state_error:
                # 角色状态更新失败不应影响整个分析流程
                logger.error(f"⚠️ 更新角色状态、关系和组织失败: {str(state_error)}", exc_info=True)
        
        # 🏛️ 更新组织自身状态（根据分析结果）
        if analysis_result.get('organization_states'):
            try:
                from app.services.character_state_update_service import CharacterStateUpdateService
                
                logger.info(f"🏛️ 开始根据分析结果更新组织自身状态...")
                async with write_lock:
                    org_state_result = await CharacterStateUpdateService.update_organization_states(
                        db=db_session,
                        project_id=project_id,
                        organization_states=analysis_result.get('organization_states', []),
                        chapter_number=chapter.chapter_number
                    )
                
                if org_state_result['updated_count'] > 0:
                    logger.info(
                        f"✅ 组织状态更新: {org_state_result['updated_count']}个组织"
                    )
                    if org_state_result['changes']:
                        for change in org_state_result['changes'][:5]:
                            logger.info(f"  - {change}")
                else:
                    logger.info("ℹ️ 本章节无组织自身状态变化")
                    
            except Exception as org_state_error:
                # 组织状态更新失败不应影响整个分析流程
                logger.error(f"⚠️ 更新组织自身状态失败: {str(org_state_error)}", exc_info=True)
        
        # 🔮 自动更新伏笔状态（根据分析结果）
        if analysis_result.get('foreshadows'):
            try:
                logger.info(f"🔮 开始根据分析结果自动更新伏笔状态...")
                async with write_lock:
                    foreshadow_stats = await foreshadow_service.auto_update_from_analysis(
                        db=db_session,
                        project_id=project_id,
                        chapter_id=chapter_id,
                        chapter_number=chapter.chapter_number,
                        analysis_foreshadows=analysis_result.get('foreshadows', [])
                    )
                
                if foreshadow_stats['planted_count'] > 0 or foreshadow_stats['resolved_count'] > 0:
                    logger.info(
                        f"✅ 伏笔自动更新: 埋入{foreshadow_stats['planted_count']}个, "
                        f"回收{foreshadow_stats['resolved_count']}个"
                    )
                else:
                    logger.info("ℹ️ 本章节无新的伏笔状态变化")
                    
            except Exception as foreshadow_error:
                # 伏笔更新失败不应影响整个分析流程
                logger.error(f"⚠️ 自动更新伏笔失败: {str(foreshadow_error)}", exc_info=True)
        else:
            logger.debug("📋 分析结果中无伏笔信息，跳过伏笔自动更新")
        
        # 最终更新任务状态（写操作，需要锁）- 增加重试机制
        update_success = False
        for retry in range(3):
            try:
                async with write_lock:
                    task.progress = 100
                    task.status = 'completed'
                    task.completed_at = datetime.now()
                    await db_session.commit()
                    update_success = True
                    logger.info(f"✅ 章节分析完成: {chapter_id}, 提取{len(memories)}条记忆")
                    break
            except Exception as commit_error:
                logger.error(f"❌ 提交任务完成状态失败(重试{retry+1}/3): {str(commit_error)}")
                if retry < 2:
                    await asyncio.sleep(0.1)
                else:
                    logger.error(f"❌ 无法更新任务为completed状态: {task_id}")
                    # 即使失败也不抛出异常，因为分析本身已经完成
        
        if not update_success:
            logger.warning(f"⚠️  章节分析完成但状态更新失败: {chapter_id}")
        
        # 返回成功状态
        return True
        
    except Exception as e:
        logger.error(f"❌ 后台分析异常: {str(e)}", exc_info=True)
        # 确保任务状态被更新为failed（写操作，需要锁）
        if db_session:
            # 多次重试更新任务状态
            for retry in range(3):
                try:
                    async with write_lock:
                        # 重新获取任务（可能是旧会话导致的问题）
                        task_result = await db_session.execute(
                            select(AnalysisTask).where(AnalysisTask.id == task_id)
                        )
                        task = task_result.scalar_one_or_none()
                        if task:
                            task.status = 'failed'
                            task.error_message = str(e)[:500]
                            task.completed_at = datetime.now()
                            task.progress = 0
                            await db_session.commit()
                            logger.info(f"✅ 任务状态已更新为failed: {task_id} (重试{retry+1}次)")
                            break
                        else:
                            logger.error(f"❌ 无法找到任务进行状态更新: {task_id}")
                            break
                except Exception as update_error:
                    logger.error(f"❌ 更新任务状态失败(重试{retry+1}/3): {str(update_error)}")
                    if retry < 2:
                        await asyncio.sleep(0.1)  # 短暂等待后重试
                    else:
                        logger.error(f"❌ 任务状态更新失败，已达到最大重试次数: {task_id}")
        
        # 返回失败状态
        return False
        
    finally:
        if db_session:
            await db_session.close()


@router.post("/{chapter_id}/generate-stream", summary="AI创作章节内容（流式）")
async def generate_chapter_content_stream(
    chapter_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    generate_request: ChapterGenerateRequest = ChapterGenerateRequest(),
    user_ai_service: AIService = Depends(get_task_ai_service("writing"))
):
    """
    根据大纲、前置章节内容和项目信息AI创作章节完整内容（流式返回）
    要求：必须按顺序生成，确保前置章节都已完成
    
    请求体参数：
    - style_id: 可选，指定使用的写作风格ID。不提供则不使用任何风格
    - target_word_count: 可选，目标字数，默认3000字，范围500-10000字
    - enable_mcp: 可选，是否启用MCP工具增强，默认True
    
    注意：此函数不使用依赖注入的db，而是在生成器内部创建独立的数据库会话
    以避免流式响应期间的连接泄漏问题
    """
    style_id = generate_request.style_id
    target_word_count = generate_request.target_word_count or 3000
    custom_model = generate_request.model if hasattr(generate_request, 'model') else None
    temp_narrative_perspective = generate_request.narrative_perspective if hasattr(generate_request, 'narrative_perspective') else None
    # 预先验证章节存在性（使用临时会话）
    async for temp_db in get_db(request):
        try:
            result = await temp_db.execute(
                select(Chapter).where(Chapter.id == chapter_id)
            )
            chapter = result.scalar_one_or_none()
            if not chapter:
                raise HTTPException(status_code=404, detail="章节不存在")
            
            # 检查前置条件
            can_generate, error_msg, previous_chapters = await check_prerequisites(temp_db, chapter)
            if not can_generate:
                raise HTTPException(status_code=400, detail=error_msg)
            
            # 保存前置章节数据供生成器使用
            previous_chapters_data = [
                {
                    'id': ch.id,
                    'chapter_number': ch.chapter_number,
                    'title': ch.title,
                    'content': ch.content
                }
                for ch in previous_chapters
            ]
        finally:
            await temp_db.close()
        break
    
    async def event_generator():
        # 在生成器内部创建独立的数据库会话
        db_session = None
        db_committed = False
        # 获取当前用户ID（在生成器外部就需要）
        current_user_id = getattr(request.state, "user_id", "system")
        
        # 初始化标准进度追踪器
        from app.utils.sse_response import WizardProgressTracker
        tracker = WizardProgressTracker("章节")
        
        try:
            yield await tracker.start()
            
            # 创建新的数据库会话
            async for db_session in get_db(request):
                # === 加载阶段 ===
                yield await tracker.loading("加载章节信息...", 0.2)
                
                # 重新获取章节信息
                chapter_result = await db_session.execute(
                    select(Chapter).where(Chapter.id == chapter_id)
                )
                current_chapter = chapter_result.scalar_one_or_none()
                if not current_chapter:
                    yield await tracker.error("章节不存在", 404)
                    return
            
                yield await tracker.loading("加载项目信息...", 0.4)
                
                # 获取项目信息
                project_result = await db_session.execute(
                    select(Project).where(Project.id == current_chapter.project_id)
                )
                project = project_result.scalar_one_or_none()
                if not project:
                    yield await tracker.error("项目不存在", 404)
                    return
                
                # 获取项目的大纲模式
                outline_mode = project.outline_mode if project else 'one-to-many'
                logger.info(f"📋 项目大纲模式: {outline_mode}")
                
                # 获取对应的大纲（优先使用 chapter.outline_id 直接关联）
                if current_chapter.outline_id:
                    outline_result = await db_session.execute(
                        select(Outline)
                        .where(Outline.id == current_chapter.outline_id)
                        .execution_options(populate_existing=True)
                    )
                else:
                    # 回退到按序号查找
                    outline_result = await db_session.execute(
                        select(Outline)
                        .where(Outline.project_id == current_chapter.project_id)
                        .where(Outline.order_index == current_chapter.chapter_number)
                        .execution_options(populate_existing=True)
                    )
                outline = outline_result.scalar_one_or_none()
                
                # 获取写作风格
                style_content = ""
                if style_id:
                    # 使用指定的风格
                    style_result = await db_session.execute(
                        select(WritingStyle).where(WritingStyle.id == style_id)
                    )
                    style = style_result.scalar_one_or_none()
                    if style:
                        # 验证风格是否可用：全局预设风格（user_id为NULL）或者当前用户的自定义风格
                        if style.user_id is None or style.user_id == current_user_id:
                            style_content = style.prompt_content or ""
                            style_type = "全局预设" if style.user_id is None else "用户自定义"
                            logger.info(f"使用指定风格: {style.name} ({style_type})")
                        else:
                            logger.warning(f"风格 {style_id} 不属于当前项目，无法使用")
                    else:
                        logger.warning(f"未找到风格 {style_id}")
                else:
                    logger.info("未指定写作风格，使用原始提示词")
                
                # 🚀 根据大纲模式选择独立的上下文构建器
                if outline_mode == 'one-to-one':
                    # ========== 1-1模式：使用独立的简化构建器 ==========
                    logger.info(f"🔧 [1-1模式] 使用 OneToOneContextBuilder")
                    context_builder = OneToOneContextBuilder(
                        memory_service=memory_service,
                        foreshadow_service=foreshadow_service
                    )
                    chapter_context = await context_builder.build(
                        chapter=current_chapter,
                        project=project,
                        outline=outline,
                        user_id=current_user_id,
                        db=db_session,
                        target_word_count=target_word_count
                    )
                    
                    # 日志输出统计信息
                    logger.info(f"📊 [1-1模式] 上下文统计:")
                    logger.info(f"  - 章节序号: {current_chapter.chapter_number}")
                    logger.info(f"  - 大纲长度: {chapter_context.context_stats.get('outline_length', 0)} 字符")
                    logger.info(f"  - 上一章内容: {chapter_context.context_stats.get('previous_content_length', 0)} 字符")
                    logger.info(f"  - 角色信息: {chapter_context.context_stats.get('characters_length', 0)} 字符")
                    logger.info(f"  - 伏笔提醒: {chapter_context.context_stats.get('foreshadow_length', 0)} 字符")
                    logger.info(f"  - 相关记忆: {chapter_context.context_stats.get('memories_length', 0)} 字符")
                    logger.info(f"  - 总长度: {chapter_context.context_stats.get('total_length', 0)} 字符")
                else:
                    # ========== 1-N模式：使用独立的完整构建器 ==========
                    logger.info(f"🔧 [1-N模式] 使用 OneToManyContextBuilder")
                    context_builder = OneToManyContextBuilder(
                        memory_service=memory_service,
                        foreshadow_service=foreshadow_service
                    )
                    chapter_context = await context_builder.build(
                        chapter=current_chapter,
                        project=project,
                        outline=outline,
                        user_id=current_user_id,
                        db=db_session,
                        style_content=style_content,
                        target_word_count=target_word_count,
                        temp_narrative_perspective=temp_narrative_perspective
                    )
                    
                    # 日志输出统计信息
                    logger.info(f"📊 [1-N模式] 上下文统计:")
                    logger.info(f"  - 章节序号: {current_chapter.chapter_number}")
                    logger.info(f"  - 衔接锚点: {chapter_context.context_stats.get('continuation_length', 0)} 字符")
                    logger.info(f"  - 角色信息: {chapter_context.context_stats.get('characters_length', 0)} 字符")
                    logger.info(f"  - 相关记忆: {chapter_context.context_stats.get('memories_length', 0)} 字符")
                    logger.info(f"  - 故事骨架: {chapter_context.context_stats.get('skeleton_length', 0)} 字符")
                    logger.info(f"  - 伏笔提醒: {chapter_context.context_stats.get('foreshadow_length', 0)} 字符")
                    logger.info(f"  - 总长度: {chapter_context.context_stats.get('total_length', 0)} 字符")
            
                yield await tracker.loading("上下文构建完成", 0.8)
                
                # 🎭 确定使用的叙事人称（临时指定 > 项目默认 > 系统默认）
                chapter_perspective = (
                    temp_narrative_perspective or
                    project.narrative_perspective or
                    '第三人称'
                )
                logger.info(f"📝 使用叙事人称: {chapter_perspective}")
                
                # 🚀 根据大纲模式选择提示词模板和参数
                if outline_mode == 'one-to-one':
                    # 1-1模式
                    if chapter_context.continuation_point:
                        # 有上一章内容
                        template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE_NEXT", current_user_id, db_session)
                        base_prompt = PromptService.format_prompt(
                            template,
                            project_title=project.title,
                            chapter_number=current_chapter.chapter_number,
                            chapter_title=current_chapter.title,
                            chapter_outline=chapter_context.chapter_outline,
                            target_word_count=target_word_count,
                            genre=project.genre or '未设定',
                            narrative_perspective=chapter_perspective,
                            previous_chapter_content=chapter_context.continuation_point,
                            previous_chapter_summary=chapter_context.previous_chapter_summary or '（无上一章摘要）',
                            characters_info=chapter_context.chapter_characters or '暂无角色信息',
                            chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                            foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                            relevant_memories=chapter_context.relevant_memories or '暂无相关记忆'
                        )
                        logger.debug(f"创建第{current_chapter.chapter_number}章提示词: {base_prompt}")
                    else:
                        # 第一章
                        template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE", current_user_id, db_session)
                        base_prompt = PromptService.format_prompt(
                            template,
                            project_title=project.title,
                            chapter_number=current_chapter.chapter_number,
                            chapter_title=current_chapter.title,
                            chapter_outline=chapter_context.chapter_outline,
                            target_word_count=target_word_count,
                            genre=project.genre or '未设定',
                            narrative_perspective=chapter_perspective,
                            characters_info=chapter_context.chapter_characters or '暂无角色信息',
                            chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                            foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                            relevant_memories=chapter_context.relevant_memories or '暂无相关记忆'
                        )
                        logger.debug(f"创建第一章提示词: {base_prompt}")
                else:
                    # ========== 1-n模式：使用完整模板 ==========
                    if chapter_context.continuation_point:
                        # 有前置内容，使用 WITH_CONTEXT 模板
                        logger.info(f"📝 [1-n模式] 使用带上下文的模板（第{current_chapter.chapter_number}章）")
                        
                        # 提取上一章摘要
                        previous_summary = "（无上一章摘要，请根据锚点续写）"
                        if chapter_context.previous_chapter_summary:
                            previous_summary = chapter_context.previous_chapter_summary
                        
                        template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY_NEXT", current_user_id, db_session)
                        base_prompt = PromptService.format_prompt(
                            template,
                            project_title=project.title,
                            chapter_number=current_chapter.chapter_number,
                            chapter_title=current_chapter.title,
                            chapter_outline=chapter_context.chapter_outline,
                            target_word_count=target_word_count,
                            continuation_point=chapter_context.continuation_point,
                            genre=project.genre or '未设定',
                            narrative_perspective=chapter_perspective,
                            characters_info=chapter_context.chapter_characters or '暂无角色信息',
                            chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                            foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                            previous_chapter_summary=previous_summary,
                            recent_chapters_context=chapter_context.recent_chapters_context or '',
                            relevant_memories=chapter_context.relevant_memories or ''
                        )
                        logger.debug(f"创建第{current_chapter.chapter_number}章提示词: {base_prompt}")
                    else:
                        # 第1章，使用无前置内容模板
                        logger.info(f"📝 [1-n模式] 使用第一章模板")
                        template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY", current_user_id, db_session)
                        base_prompt = PromptService.format_prompt(
                            template,
                            project_title=project.title,
                            chapter_number=current_chapter.chapter_number,
                            chapter_title=current_chapter.title,
                            chapter_outline=chapter_context.chapter_outline,
                            target_word_count=target_word_count,
                            genre=project.genre or '未设定',
                            narrative_perspective=chapter_perspective,
                            characters_info=chapter_context.chapter_characters or '暂无角色信息',
                            chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                            foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                            relevant_memories=chapter_context.relevant_memories or '暂无相关记忆'
                        )
                        logger.debug(f"创建第一章提示词: {base_prompt}")
                
                # 应用写作风格
                if style_content:
                    prompt = WritingStyleManager.apply_style_to_prompt(base_prompt, style_content)
                else:
                    prompt = base_prompt

                # Context Agent 增强上下文（防遗忘）
                context_enhancement = await _enhance_prompt_with_context_agent(
                    db_session=db_session,
                    base_prompt=prompt,
                    project_id=project.id,
                    chapter_number=current_chapter.chapter_number,
                    chapter_outline=chapter_context.chapter_outline,
                    user_id=current_user_id,
                    top_k=5,
                )
                prompt = context_enhancement["prompt"]
                context_system_prompt = context_enhancement["system_prompt"]
                if context_enhancement.get("enabled"):
                    logger.info(
                        f"✅ Context Agent 已注入: 检索片段={context_enhancement.get('metadata', {}).get('retrieved_chunks', 0)}, "
                        f"摘要={context_enhancement.get('metadata', {}).get('summaries_count', 0)}"
                    )

                # === 反幻觉预处理：导演预规划 + 角色名单构建 ===
                director_plan_block = ""
                char_lists = {"forbidden": [], "new_characters": [], "introduced": []}
                try:
                    char_lists = await _build_character_lists(
                        db_session=db_session,
                        project=project,
                        chapter_outline=chapter_context.chapter_outline,
                        chapter_number=current_chapter.chapter_number,
                    )
                    if char_lists["forbidden"]:
                        logger.info(f"🚫 禁止角色名单: {char_lists['forbidden'][:5]}...")
                    if char_lists["new_characters"]:
                        logger.info(f"🆕 新角色名单: {char_lists['new_characters']}")
                except Exception as e:
                    logger.warning(f"⚠️ 角色名单构建失败（不影响生成）: {e}")

                if settings.enable_director_plan:
                    try:
                        # 获取上一章摘要
                        prev_summary_for_director = "无"
                        if current_chapter.chapter_number > 1:
                            ps_result = await db_session.execute(
                                select(Chapter.summary)
                                .where(Chapter.project_id == project.id)
                                .where(Chapter.chapter_number == current_chapter.chapter_number - 1)
                            )
                            prev_summary_for_director = ps_result.scalar_one_or_none() or "无"

                        director_plan = await _generate_director_plan(
                            ai_service=user_ai_service,
                            previous_summary=prev_summary_for_director,
                            chapter_outline=chapter_context.chapter_outline,
                            introduced_characters=", ".join(char_lists.get("introduced", [])) or "无",
                            new_characters=", ".join(char_lists.get("new_characters", [])) or "无",
                            narrative_perspective=chapter_perspective,
                            user_id=current_user_id,
                            db_session=db_session,
                        )
                        if director_plan:
                            director_plan_block = _format_director_plan_block(director_plan)
                    except Exception as e:
                        logger.warning(f"⚠️ 导演预规划失败（不影响生成）: {e}")

                # === 准备阶段 ===
                yield await tracker.preparing("准备AI提示词...")
                
                logger.info(f"开始AI流式创作章节 {chapter_id}")
                
                # 🎨 写作风格 + Context Agent 系统提示词拼接（最高优先级）
                system_prompt_with_style = None
                if style_content:
                    system_prompt_with_style = f"""【🎨 写作风格要求 - 最高优先级】

{style_content}

⚠️ 请严格遵循上述写作风格要求进行创作，这是最重要的指令！
确保在整个章节创作过程中始终保持风格的一致性。"""
                    logger.info(f"✅ 已将写作风格注入系统提示词（{len(style_content)}字符）")

                final_system_prompt = _merge_system_prompts(
                    director_plan_block,
                    system_prompt_with_style,
                    context_system_prompt,
                )

                # 🔢 计算 max_tokens 限制
                # 中文字符约 1.5-2 个 token，使用 3 倍系数确保有足够空间完成内容
                # 字数控制主要依靠提示词约束，max_tokens 仅作为安全上限
                calculated_max_tokens = int(target_word_count * 3)
                calculated_max_tokens = max(2000, min(calculated_max_tokens, 16000))  # 限制在 2000-16000 之间
                logger.info(f"📊 目标字数: {target_word_count}, 计算 max_tokens: {calculated_max_tokens}")
                
                # 准备生成参数
                generate_kwargs = {
                    "prompt": prompt,
                    "system_prompt": final_system_prompt,
                    "tool_choice": "required",
                    "max_tokens": calculated_max_tokens  # 添加 max_tokens 限制
                }
                if custom_model:
                    logger.info(f"  使用自定义模型: {custom_model}")
                    generate_kwargs["model"] = custom_model
                    # 注意：这里使用用户配置的AI服务，模型参数会覆盖默认模型
                    # 如果需要切换provider，需要在前端传递provider参数
                
                # === 生成阶段 ===
                full_content = ""
                chunk_count = 0

                yield await tracker.generating(
                    current_chars=0,
                    estimated_total=target_word_count
                )

                async for chunk in user_ai_service.generate_text_stream(**generate_kwargs):
                    full_content += chunk
                    chunk_count += 1

                    # 发送内容块
                    yield await tracker.generating_chunk(chunk)

                    # 每5个chunk发送一次进度更新
                    if chunk_count % 5 == 0:
                        yield await tracker.generating(
                            current_chars=len(full_content),
                            estimated_total=target_word_count,
                            message=f'正在创作中... 已生成 {len(full_content)} 字'
                        )

                    # 每20个chunk发送心跳
                    if chunk_count % 20 == 0:
                        yield await tracker.heartbeat()

                    await asyncio.sleep(0)  # 让出控制权

                # 记录流式阶段的原始内容，用于后续判断是否被重写
                streamed_content = full_content

                # === 质量门控阶段（使用 review 渠道 AI 服务）===
                try:
                    review_ai_service_for_gate = await _resolve_task_ai_service_standalone(
                        user_id=current_user_id, task_type="review", db_session=db_session
                    )
                except Exception as resolve_err:
                    logger.warning(f"⚠️ 解析 review 渠道失败，回退 writing 渠道: {resolve_err}")
                    review_ai_service_for_gate = user_ai_service

                yield await tracker.preparing("执行质量门控审查...")
                quality_gate_result = await _apply_quality_gate_with_rewrite(
                    db_session=db_session,
                    ai_service=review_ai_service_for_gate,
                    project=project,
                    chapter=current_chapter,
                    chapter_outline=chapter_context.chapter_outline,
                    current_content=full_content,
                    base_prompt=prompt,
                    system_prompt=final_system_prompt,
                    max_tokens=calculated_max_tokens,
                    model=custom_model,
                )
                full_content = quality_gate_result["content"]
                if quality_gate_result.get("rewrite_count", 0) > 0:
                    yield await tracker.warning(
                        f"质量门控触发重写 {quality_gate_result['rewrite_count']} 次，当前评分 {quality_gate_result['overall_score']:.1f}"
                    )
                elif not quality_gate_result.get("passed", True):
                    yield await tracker.warning(
                        f"质量门控未达阈值，保留当前版本（评分 {quality_gate_result['overall_score']:.1f}）"
                    )

                # === 规则护栏检查（零 AI 调用，违规时可触发一次重写）===
                if settings.enable_guardrails:
                    yield await tracker.preparing("执行规则护栏检查...")
                    full_content = await _apply_guardrails_check(
                        ai_service=review_ai_service_for_gate,
                        content=full_content,
                        forbidden_names=char_lists.get("forbidden", []),
                        new_characters=char_lists.get("new_characters", []),
                        narrative_perspective=chapter_perspective,
                        system_prompt=final_system_prompt,
                        max_tokens=calculated_max_tokens,
                        model=custom_model,
                    )

                # 如果质量门控或护栏重写了内容，通知前端替换显示
                if full_content != streamed_content:
                    logger.info(f"📝 内容被重写，发送 content_replace 到前端")
                    yield await tracker.content_replace(full_content)

                # === 保存阶段 ===
                yield await tracker.saving("正在保存章节...", 0.3)
                
                # 更新章节内容到数据库
                old_word_count = current_chapter.word_count or 0
                current_chapter.content = full_content
                new_word_count = len(full_content)
                current_chapter.word_count = new_word_count
                current_chapter.status = "completed"
                
                # 更新项目字数
                project.current_words = project.current_words - old_word_count + new_word_count
                
                # 记录生成历史
                history = GenerationHistory(
                    project_id=current_chapter.project_id,
                    chapter_id=current_chapter.id,
                    prompt=f"创作章节: 第{current_chapter.chapter_number}章 {current_chapter.title}",
                    generated_content=full_content[:500] if len(full_content) > 500 else full_content,
                    model="default"
                )
                db_session.add(history)
                
                await db_session.commit()
                db_committed = True
                await db_session.refresh(current_chapter)

                logger.info(f"成功创作章节 {chapter_id}，共 {new_word_count} 字")

                # Data Agent：章节切片向量化 + 状态提取 + 摘要钩子同步
                data_agent_result = await _run_data_agent_pipeline(
                    db_session=db_session,
                    ai_service=user_ai_service,
                    chapter=current_chapter,
                    content=full_content,
                )
                logger.info(
                    f"📦 Data Agent 完成: chunks={data_agent_result.get('stored_count', 0)}, "
                    f"state_change={'yes' if data_agent_result.get('state_change_log') else 'no'}"
                )
                
                # 🔮 章节生成后自动标记计划在本章埋入的伏笔
                try:
                    plant_result = await foreshadow_service.auto_plant_pending_foreshadows(
                        db=db_session,
                        project_id=project.id,
                        chapter_id=chapter_id,
                        chapter_number=current_chapter.chapter_number,
                        chapter_content=full_content
                    )
                    if plant_result.get('planted_count', 0) > 0:
                        logger.info(f"🔮 自动标记伏笔已埋入: {plant_result['planted_count']}个")
                except Exception as plant_error:
                    logger.warning(f"⚠️ 自动标记伏笔埋入失败: {str(plant_error)}")
                
                # 创建分析任务
                analysis_task = AnalysisTask(
                    chapter_id=chapter_id,
                    user_id=current_user_id,
                    project_id=project.id,
                    status='pending',
                    progress=0
                )
                db_session.add(analysis_task)
                await db_session.commit()
                await db_session.refresh(analysis_task)
                
                task_id = analysis_task.id
                logger.info(f"📋 已创建分析任务: {task_id}")
                
                # 短暂延迟确保SQLite WAL完成写入
                await asyncio.sleep(0.05)
                
                # 直接启动后台分析（并发执行，使用 review 渠道的 AI 服务）
                try:
                    review_ai_service = await _resolve_task_ai_service_standalone(
                        user_id=current_user_id, task_type="review", db_session=db_session
                    )
                except Exception as resolve_err:
                    logger.warning(f"⚠️ 解析 review 渠道失败，回退默认: {resolve_err}")
                    review_ai_service = user_ai_service

                background_tasks.add_task(
                    analyze_chapter_background,
                    chapter_id=chapter_id,
                    user_id=current_user_id,
                    project_id=project.id,
                    task_id=task_id,
                    ai_service=review_ai_service
                )
                
                yield await tracker.saving("章节保存完成", 0.8)
                
                # === 完成阶段 ===
                yield await tracker.complete("创作完成！")
                
                # 发送结果数据
                yield await tracker.result({
                    'word_count': new_word_count,
                    'analysis_task_id': task_id
                })
                
                # 发送分析开始事件（使用自定义事件）
                yield await SSEResponse.send_event(
                    event='analysis_started',
                    data={
                        'task_id': task_id,
                        'message': '章节分析已开始'
                    }
                )
                
                # 发送完成信号
                yield await tracker.done()
                
                break  # 退出async for db_session循环
        
        except GeneratorExit:
            # SSE连接断开
            logger.warning("章节生成器被提前关闭（SSE断开）")
        except Exception as e:
            logger.error(f"流式创作章节失败: {str(e)}")
            yield await tracker.error(str(e))
    return create_sse_response(event_generator())


def _build_analysis_task_status_payload(
    chapter_id: str,
    task: Optional[AnalysisTask],
    auto_recovered: bool = False
) -> dict:
    """统一构建分析任务状态响应"""
    if not task:
        return {
            "has_task": False,
            "chapter_id": chapter_id,
            "status": "none",
            "progress": 0,
            "error_message": None,
            "auto_recovered": False,
            "task_id": None,
            "created_at": None,
            "started_at": None,
            "completed_at": None
        }

    return {
        "has_task": True,
        "task_id": task.id,
        "chapter_id": task.chapter_id,
        "status": task.status,
        "progress": task.progress,
        "error_message": task.error_message,
        "auto_recovered": auto_recovered,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None
    }


@router.get("/{chapter_id}/analysis/status", summary="查询章节分析任务状态", response_model=AnalysisTaskStatusResponse)
async def get_analysis_task_status(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    查询指定章节的最新分析任务状态
    
    自动恢复机制：
    - 如果任务状态为running且超过1分钟未更新，自动标记为failed
    - 如果任务状态为pending且超过2分钟未启动，自动标记为failed
    
    返回:
    - has_task: 是否存在分析任务
    - task_id: 任务ID（如果存在）
    - status: pending/running/completed/failed/none（如果不存在则为none）
    - progress: 0-100
    - error_message: 错误信息(如果失败)
    - auto_recovered: 是否被自动恢复
    - created_at: 创建时间
    - completed_at: 完成时间
    
    注意：当章节不存在或无权访问时返回404，当没有分析任务时返回has_task=false
    """
    from datetime import timedelta
    
    # 先获取章节以验证存在性和权限
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取该章节最新的分析任务
    result = await db.execute(
        select(AnalysisTask)
        .where(AnalysisTask.chapter_id == chapter_id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(1)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        # 返回无任务状态，而不是抛出404错误
        return _build_analysis_task_status_payload(chapter_id, None)
    
    auto_recovered = False
    current_time = datetime.now()
    
    # 自动恢复卡住的任务
    # 注意：后端分析有3次重试机制，每次重试会重置 started_at
    # 所以超时时间需要足够长以支持完整的重试周期（约5分钟）
    if task.status == 'running':
        # 检查是否正在重试（error_message 包含"重试"信息）
        is_retrying = task.error_message and '重试' in task.error_message
        # 如果正在重试，给予更长的超时时间（5分钟），否则3分钟
        timeout_minutes = 5 if is_retrying else 3
        
        # 如果任务在running状态超过超时时间，标记为失败
        if task.started_at and (current_time - task.started_at) > timedelta(minutes=timeout_minutes):
            task.status = 'failed'
            task.error_message = f'任务超时（超过{timeout_minutes}分钟未完成，已自动恢复）'
            task.completed_at = current_time
            task.progress = 0
            auto_recovered = True
            await db.commit()
            await db.refresh(task)
            logger.warning(f"🔄 自动恢复卡住的任务: {task.id}, 章节: {chapter_id}")
    
    elif task.status == 'pending':
        # 如果任务在pending状态超过3分钟仍未开始，标记为失败
        if task.created_at and (current_time - task.created_at) > timedelta(minutes=3):
            task.status = 'failed'
            task.error_message = '任务启动超时（超过3分钟未启动，已自动恢复）'
            task.completed_at = current_time
            task.progress = 0
            auto_recovered = True
            await db.commit()
            await db.refresh(task)
            logger.warning(f"🔄 自动恢复未启动的任务: {task.id}, 章节: {chapter_id}")
    
    return _build_analysis_task_status_payload(chapter_id, task, auto_recovered)


@router.post("/project/{project_id}/analysis/statuses", summary="批量查询章节分析任务状态", response_model=BatchAnalysisStatusResponse)
async def get_project_analysis_task_statuses(
    project_id: str,
    payload: BatchAnalysisStatusRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """批量查询项目章节分析状态，避免前端逐章节请求造成请求风暴"""
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(project_id, user_id, db)

    # 先取项目章节列表
    chapter_query = select(Chapter.id).where(Chapter.project_id == project_id)
    if payload.chapter_ids and len(payload.chapter_ids) > 0:
        chapter_query = chapter_query.where(Chapter.id.in_(payload.chapter_ids))

    chapter_result = await db.execute(chapter_query)
    chapter_ids = [row[0] for row in chapter_result.all()]

    if not chapter_ids:
        return {
            "project_id": project_id,
            "total": 0,
            "items": {}
        }

    # 批量查询这些章节对应的所有分析任务，随后在内存中取最新一条
    tasks_result = await db.execute(
        select(AnalysisTask)
        .where(AnalysisTask.chapter_id.in_(chapter_ids))
        .order_by(AnalysisTask.chapter_id, AnalysisTask.created_at.desc())
    )
    all_tasks = tasks_result.scalars().all()

    latest_task_map: dict[str, AnalysisTask] = {}
    for task in all_tasks:
        if task.chapter_id not in latest_task_map:
            latest_task_map[task.chapter_id] = task

    items: dict[str, dict] = {}
    for chapter_id in chapter_ids:
        task = latest_task_map.get(chapter_id)
        items[chapter_id] = _build_analysis_task_status_payload(chapter_id, task)

    return {
        "project_id": project_id,
        "total": len(chapter_ids),
        "items": items
    }


async def _run_batch_analysis_in_sequence(
    tasks_queue: list[dict[str, int | str]],
    user_id: str,
    project_id: str,
    ai_service: AIService
) -> None:
    """按章节顺序逐个执行分析任务。"""
    for index, task_item in enumerate(tasks_queue, start=1):
        chapter_id = str(task_item["chapter_id"])
        chapter_number = int(task_item["chapter_number"])
        task_id = str(task_item["task_id"])

        logger.info(f"🔁 一键分析顺序执行中 [{index}/{len(tasks_queue)}]：第{chapter_number}章")
        try:
            success = await analyze_chapter_background(
                chapter_id=chapter_id,
                user_id=user_id,
                project_id=project_id,
                task_id=task_id,
                ai_service=ai_service
            )
            if not success:
                logger.warning(f"⚠️ 一键顺序分析返回失败: chapter_id={chapter_id}, task_id={task_id}")
        except Exception as e:
            # analyze_chapter_background 内部已处理任务失败状态，这里仅保护顺序队列不中断
            logger.error(
                f"❌ 一键顺序分析异常（已继续后续章节） chapter_id={chapter_id}, task_id={task_id}: {str(e)}",
                exc_info=True
            )


@router.post(
    "/project/{project_id}/analysis/analyze-unanalyzed",
    summary="一键按章节顺序分析未分析章节",
    response_model=BatchAnalyzeUnanalyzedResponse
)
async def batch_analyze_unanalyzed_chapters(
    project_id: str,
    payload: BatchAnalyzeUnanalyzedRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_user_ai_service)
):
    """自动识别项目中未完成分析的章节，并按章节顺序逐个启动分析。"""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    # 验证项目权限
    await verify_project_access(project_id, user_id, db)

    # 查询目标章节（可选限制 chapter_ids）
    chapter_query = select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_number)
    if payload.chapter_ids and len(payload.chapter_ids) > 0:
        chapter_query = chapter_query.where(Chapter.id.in_(payload.chapter_ids))

    chapter_result = await db.execute(chapter_query)
    chapters = chapter_result.scalars().all()

    if not chapters:
        return {
            "project_id": project_id,
            "total_candidates": 0,
            "total_started": 0,
            "total_skipped_no_content": 0,
            "total_skipped_running": 0,
            "total_already_completed": 0,
            "started_tasks": {}
        }

    chapter_ids = [chapter.id for chapter in chapters]

    # 查询每个章节最新分析任务
    tasks_result = await db.execute(
        select(AnalysisTask)
        .where(AnalysisTask.chapter_id.in_(chapter_ids))
        .order_by(AnalysisTask.chapter_id, AnalysisTask.created_at.desc())
    )
    all_tasks = tasks_result.scalars().all()

    latest_task_map: dict[str, AnalysisTask] = {}
    for task in all_tasks:
        if task.chapter_id not in latest_task_map:
            latest_task_map[task.chapter_id] = task

    total_candidates = 0
    total_skipped_no_content = 0
    total_skipped_running = 0
    total_already_completed = 0
    started_tasks: dict[str, dict] = {}
    tasks_to_start: list[tuple[Chapter, AnalysisTask]] = []

    for chapter in chapters:
        # 无内容章节直接跳过
        if not chapter.content or chapter.content.strip() == "":
            total_skipped_no_content += 1
            continue

        total_candidates += 1
        latest_task = latest_task_map.get(chapter.id)

        # 已在队列/分析中，跳过
        if latest_task and latest_task.status in ("pending", "running"):
            total_skipped_running += 1
            continue

        # 已分析完成，跳过
        if latest_task and latest_task.status == "completed":
            total_already_completed += 1
            continue

        # 无任务/失败/未知状态，重新发起分析
        analysis_task = AnalysisTask(
            chapter_id=chapter.id,
            user_id=user_id,
            project_id=project_id,
            status='pending',
            progress=0
        )
        db.add(analysis_task)
        tasks_to_start.append((chapter, analysis_task))

    if tasks_to_start:
        try:
            await db.flush()

            for chapter, analysis_task in tasks_to_start:
                started_tasks[chapter.id] = _build_analysis_task_status_payload(chapter.id, analysis_task)

            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ 一键分析创建任务失败: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"一键分析创建任务失败: {str(e)}")

        # 提交后立即按章节顺序调度后台分析（逐章执行）
        tasks_queue = [
            {
                "chapter_id": chapter.id,
                "chapter_number": chapter.chapter_number,
                "task_id": analysis_task.id
            }
            for chapter, analysis_task in tasks_to_start
        ]
        asyncio.create_task(
            _run_batch_analysis_in_sequence(
                tasks_queue=tasks_queue,
                user_id=user_id,
                project_id=project_id,
                ai_service=user_ai_service
            )
        )

    return {
        "project_id": project_id,
        "total_candidates": total_candidates,
        "total_started": len(tasks_to_start),
        "total_skipped_no_content": total_skipped_no_content,
        "total_skipped_running": total_skipped_running,
        "total_already_completed": total_already_completed,
        "started_tasks": started_tasks
    }


@router.get("/{chapter_id}/analysis", summary="获取章节分析结果")
async def get_chapter_analysis(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取章节的完整分析结果
    
    返回:
    - analysis_data: 完整的分析数据(JSON)
    - summary: 分析摘要文本
    - memories: 提取的记忆列表
    - created_at: 分析时间
    """
    # 先获取章节以验证权限
    chapter_result_check = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter_check = chapter_result_check.scalar_one_or_none()
    if chapter_check:
        # 验证用户权限
        user_id = getattr(request.state, 'user_id', None)
        await verify_project_access(chapter_check.project_id, user_id, db)
    
    # 获取分析结果
    analysis_result = await db.execute(
        select(PlotAnalysis)
        .where(PlotAnalysis.chapter_id == chapter_id)
        .order_by(PlotAnalysis.created_at.desc())
        .limit(1)
    )
    analysis = analysis_result.scalar_one_or_none()
    
    if not analysis:
        raise HTTPException(status_code=404, detail="该章节暂无分析结果")
    
    # 获取相关记忆
    memories_result = await db.execute(
        select(StoryMemory)
        .where(StoryMemory.chapter_id == chapter_id)
        .order_by(StoryMemory.importance_score.desc())
    )
    memories = memories_result.scalars().all()
    
    return {
        "chapter_id": chapter_id,
        "analysis": analysis.to_dict(),  # 使用to_dict()方法
        "memories": [
            {
                "id": mem.id,
                "type": mem.memory_type,
                "title": mem.title,
                "content": mem.content,
                "importance": mem.importance_score,
                "tags": mem.tags,
                "is_foreshadow": mem.is_foreshadow,
                "position": mem.chapter_position,
                "related_characters": mem.related_characters
            }
            for mem in memories
        ],
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None
    }


@router.get("/{chapter_id}/annotations", summary="获取章节标注数据")
async def get_chapter_annotations(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取章节的标注数据（用于前端展示标注）
    
    返回格式化的标注列表，包含精确位置信息
    适用于章节内容的可视化标注展示
    """
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    
    # 获取章节
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    # 验证项目访问权限
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取分析结果
    analysis_result = await db.execute(
        select(PlotAnalysis)
        .where(PlotAnalysis.chapter_id == chapter_id)
        .order_by(PlotAnalysis.created_at.desc())
        .limit(1)
    )
    analysis = analysis_result.scalar_one_or_none()
    
    # 获取记忆
    memories_result = await db.execute(
        select(StoryMemory)
        .where(StoryMemory.chapter_id == chapter_id)
        .order_by(StoryMemory.importance_score.desc())
    )
    memories = memories_result.scalars().all()
    
    # 构建标注数据
    annotations = []
    
    for mem in memories:
        # 优先从数据库读取位置信息
        position = mem.chapter_position if mem.chapter_position is not None else -1
        length = mem.text_length if hasattr(mem, 'text_length') and mem.text_length is not None else 0
        metadata_extra = {}
        
        # 如果数据库中没有位置信息，尝试从分析数据中重新计算
        if position == -1 and analysis and chapter.content:
            # 根据记忆类型从分析数据中查找对应项
            if mem.memory_type == 'hook' and analysis.hooks:
                for hook in analysis.hooks:
                    # 通过标题或内容匹配
                    if mem.title and hook.get('type') in mem.title:
                        keyword = hook.get('keyword', '')
                        if keyword:
                            pos = chapter.content.find(keyword)
                            if pos != -1:
                                position = pos
                                length = len(keyword)
                        metadata_extra["strength"] = hook.get('strength', 5)
                        metadata_extra["position_desc"] = hook.get('position', '')
                        break
            
            elif mem.memory_type == 'foreshadow' and analysis.foreshadows:
                for foreshadow in analysis.foreshadows:
                    if foreshadow.get('content') in mem.content:
                        keyword = foreshadow.get('keyword', '')
                        if keyword:
                            pos = chapter.content.find(keyword)
                            if pos != -1:
                                position = pos
                                length = len(keyword)
                        metadata_extra["foreshadow_type"] = foreshadow.get('type', 'planted')
                        metadata_extra["strength"] = foreshadow.get('strength', 5)
                        break
            
            elif mem.memory_type == 'plot_point' and analysis.plot_points:
                for plot_point in analysis.plot_points:
                    if plot_point.get('content') in mem.content:
                        keyword = plot_point.get('keyword', '')
                        if keyword:
                            pos = chapter.content.find(keyword)
                            if pos != -1:
                                position = pos
                                length = len(keyword)
                        break
        else:
            # 如果数据库有位置，也从分析数据中提取额外的元数据
            if analysis:
                if mem.memory_type == 'hook' and analysis.hooks:
                    for hook in analysis.hooks:
                        if mem.title and hook.get('type') in mem.title:
                            metadata_extra["strength"] = hook.get('strength', 5)
                            metadata_extra["position_desc"] = hook.get('position', '')
                            break
                
                elif mem.memory_type == 'foreshadow' and analysis.foreshadows:
                    for foreshadow in analysis.foreshadows:
                        if foreshadow.get('content') in mem.content:
                            metadata_extra["foreshadow_type"] = foreshadow.get('type', 'planted')
                            metadata_extra["strength"] = foreshadow.get('strength', 5)
                            break
        
        annotation = {
            "id": mem.id,
            "type": mem.memory_type,
            "title": mem.title,
            "content": mem.content,
            "importance": mem.importance_score or 0.5,
            "position": position,
            "length": length,
            "tags": mem.tags or [],
            "metadata": {
                "is_foreshadow": mem.is_foreshadow,
                "related_characters": mem.related_characters or [],
                "related_locations": mem.related_locations or [],
                **metadata_extra
            }
        }
        
        annotations.append(annotation)
    
    return {
        "chapter_id": chapter_id,
        "chapter_number": chapter.chapter_number,
        "title": chapter.title,
        "word_count": chapter.word_count or 0,
        "annotations": annotations,
        "has_analysis": analysis is not None,
        "summary": {
            "total_annotations": len(annotations),
            "hooks": len([a for a in annotations if a["type"] == "hook"]),
            "foreshadows": len([a for a in annotations if a["type"] == "foreshadow"]),
            "plot_points": len([a for a in annotations if a["type"] == "plot_point"]),
            "character_events": len([a for a in annotations if a["type"] == "character_event"])
        }
    }


@router.post("/{chapter_id}/analyze", summary="手动触发章节分析")
async def trigger_chapter_analysis(
    chapter_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_task_ai_service("review"))
):
    """
    手动触发章节分析(用于重新分析或分析旧章节)
    """
    # 从请求中获取用户ID
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    # 验证章节存在
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    if not chapter.content or chapter.content.strip() == "":
        raise HTTPException(status_code=400, detail="章节内容为空，无法分析")
    
    # 获取项目信息
    project_result = await db.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = project_result.scalar_one_or_none()
    
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    
    # 创建分析任务
    analysis_task = AnalysisTask(
        chapter_id=chapter_id,
        user_id=user_id,
        project_id=project.id,
        status='pending',
        progress=0
    )
    db.add(analysis_task)
    await db.commit()
    
    task_id = analysis_task.id
    logger.info(f"📋 创建分析任务: {task_id}, 章节: {chapter_id}")
    
    # 刷新数据库会话，确保其他会话可以看到新任务
    await db.refresh(analysis_task)
    
    # 短暂延迟确保SQLite WAL完成写入（让其他会话可见）
    await asyncio.sleep(3)
    
    # 直接启动后台分析（并发执行）
    background_tasks.add_task(
        analyze_chapter_background,
        chapter_id=chapter_id,
        user_id=user_id,
        project_id=project.id,
        task_id=task_id,
        ai_service=user_ai_service
    )
    
    return {
        "task_id": task_id,
        "chapter_id": chapter_id,
        "status": "pending",
        "message": "分析任务已创建并开始执行"
    }



def calculate_estimated_time(
    chapter_count: int,
    target_word_count: int,
    enable_analysis: bool
) -> int:
    """
    计算预估耗时（分钟）
    
    基准：
    - 生成3000字约需2分钟
    - 分析约需1分钟
    """
    generation_time_per_chapter = (target_word_count / 3000) * 2
    analysis_time_per_chapter = 1 if enable_analysis else 0
    
    total_time = chapter_count * (generation_time_per_chapter + analysis_time_per_chapter)
    
    return max(1, int(total_time))


@router.post("/project/{project_id}/batch-generate", response_model=BatchGenerateResponse, summary="批量顺序生成章节内容")
async def batch_generate_chapters_in_order(
    project_id: str,
    batch_request: BatchGenerateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_task_ai_service("writing"))
):
    """
    从指定章节开始，按顺序批量生成指定数量的章节
    
    特性：
    1. 严格按章节序号顺序生成（不可跳过）
    2. 自动检测起始章节是否可生成
    3. 可选同步分析（影响耗时和质量）
    4. 失败后终止，不继续后续章节
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    # 验证项目存在和用户权限
    project = await verify_project_access(project_id, user_id, db)
    
    # 获取项目的所有章节，按序号排序
    result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == project_id)
        .order_by(Chapter.chapter_number)
    )
    all_chapters = result.scalars().all()
    
    if not all_chapters:
        raise HTTPException(status_code=404, detail="项目没有章节")
    
    # 计算要生成的章节范围
    start_number = batch_request.start_chapter_number
    end_number = start_number + batch_request.count - 1
    
    # 筛选出要生成的章节
    chapters_to_generate = [
        ch for ch in all_chapters
        if start_number <= ch.chapter_number <= end_number
    ]
    
    if not chapters_to_generate:
        raise HTTPException(status_code=404, detail="指定范围内没有章节")
    
    # 验证起始章节的前置条件
    first_chapter = chapters_to_generate[0]
    can_generate, error_msg, _ = await check_prerequisites(db, first_chapter)
    if not can_generate:
        raise HTTPException(status_code=400, detail=f"起始章节无法生成：{error_msg}")
    
    # 创建批量生成任务
    batch_task = BatchGenerationTask(
        project_id=project_id,
        user_id=user_id,
        start_chapter_number=start_number,
        chapter_count=len(chapters_to_generate),
        chapter_ids=[ch.id for ch in chapters_to_generate],
        style_id=batch_request.style_id,
        target_word_count=batch_request.target_word_count,
        enable_analysis=batch_request.enable_analysis,
        max_retries=batch_request.max_retries,
        status='pending',
        total_chapters=len(chapters_to_generate),
        completed_chapters=0,
        failed_chapters=[],
        current_retry_count=0
    )
    db.add(batch_task)
    await db.commit()
    await db.refresh(batch_task)
    
    batch_id = batch_task.id
    
    # 计算预估耗时
    estimated_time = calculate_estimated_time(
        chapter_count=len(chapters_to_generate),
        target_word_count=batch_request.target_word_count,
        enable_analysis=batch_request.enable_analysis
    )
    
    logger.info(f"📦 创建批量生成任务: {batch_id}, 章节: 第{start_number}-{end_number}章, 预估耗时: {estimated_time}分钟")
    
    # 启动后台批量生成任务，传递model参数
    background_tasks.add_task(
        execute_batch_generation_in_order,
        batch_id=batch_id,
        user_id=user_id,
        ai_service=user_ai_service,
        custom_model=batch_request.model
    )
    
    return BatchGenerateResponse(
        batch_id=batch_id,
        message=f"批量生成任务已创建，将生成 {len(chapters_to_generate)} 个章节",
        chapters_to_generate=[
            {
                "id": ch.id,
                "chapter_number": ch.chapter_number,
                "title": ch.title
            }
            for ch in chapters_to_generate
        ],
        estimated_time_minutes=estimated_time
    )


@router.get("/batch-generate/{batch_id}/status", response_model=BatchGenerateStatusResponse, summary="查询批量生成任务状态")
async def get_batch_generation_status(
    batch_id: str,
    db: AsyncSession = Depends(get_db)
):
    """查询批量生成任务的状态和进度"""
    result = await db.execute(
        select(BatchGenerationTask).where(BatchGenerationTask.id == batch_id)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="批量生成任务不存在")
    
    return BatchGenerateStatusResponse(
        batch_id=task.id,
        status=task.status,
        total=task.total_chapters,
        completed=task.completed_chapters,
        current_chapter_id=task.current_chapter_id,
        current_chapter_number=task.current_chapter_number,
        current_retry_count=task.current_retry_count,
        max_retries=task.max_retries,
        failed_chapters=task.failed_chapters or [],
        created_at=task.created_at.isoformat() if task.created_at else None,
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        error_message=task.error_message
    )


@router.get("/project/{project_id}/batch-generate/active", summary="获取项目当前运行中的批量生成任务")
async def get_active_batch_generation(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    获取项目当前运行中的批量生成任务
    用于页面刷新后恢复任务状态
    """
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(project_id, user_id, db)
    
    result = await db.execute(
        select(BatchGenerationTask)
        .where(BatchGenerationTask.project_id == project_id)
        .where(BatchGenerationTask.status.in_(['pending', 'running']))
        .order_by(BatchGenerationTask.created_at.desc())
        .limit(1)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        return {
            "has_active_task": False,
            "task": None
        }
    
    return {
        "has_active_task": True,
        "task": {
            "batch_id": task.id,
            "status": task.status,
            "total": task.total_chapters,
            "completed": task.completed_chapters,
            "current_chapter_id": task.current_chapter_id,
            "current_chapter_number": task.current_chapter_number,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None
        }
    }


@router.post("/batch-generate/{batch_id}/cancel", summary="取消批量生成任务")
async def cancel_batch_generation(
    batch_id: str,
    db: AsyncSession = Depends(get_db)
):
    """取消正在进行的批量生成任务"""
    result = await db.execute(
        select(BatchGenerationTask).where(BatchGenerationTask.id == batch_id)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="批量生成任务不存在")
    
    if task.status in ['completed', 'failed', 'cancelled']:
        raise HTTPException(status_code=400, detail=f"任务已处于 {task.status} 状态，无法取消")
    
    task.status = 'cancelled'
    task.completed_at = datetime.now()
    await db.commit()
    
    logger.info(f"🛑 批量生成任务已取消: {batch_id}")
    
    return {
        "message": "批量生成任务已取消",
        "batch_id": batch_id,
        "completed_chapters": task.completed_chapters,
        "total_chapters": task.total_chapters
    }


async def execute_batch_generation_in_order(
    batch_id: str,
    user_id: str,
    ai_service: AIService,
    custom_model: Optional[str] = None
):
    """
    按顺序执行批量生成任务（后台任务）
    - 严格按章节序号顺序
    - 任一章节失败则终止后续生成
    - 可选同步分析
    """
    db_session = None
    write_lock = await get_db_write_lock(user_id)
    
    try:
        logger.info(f"📦 开始执行顺序批量生成任务: {batch_id}")
        
        # 创建独立数据库会话
        from app.database import get_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        
        engine = await get_engine(user_id)
        AsyncSessionLocal = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
        db_session = AsyncSessionLocal()
        
        # 获取任务
        task_result = await db_session.execute(
            select(BatchGenerationTask).where(BatchGenerationTask.id == batch_id)
        )
        task = task_result.scalar_one_or_none()
        
        if not task:
            logger.error(f"❌ 批量生成任务不存在: {batch_id}")
            return
        
        # 更新任务状态为运行中
        async with write_lock:
            task.status = 'running'
            task.started_at = datetime.now()
            await db_session.commit()
        
        # 维护上一章的摘要，用于传递给下一章（防重复上下文）
        last_generated_summary = None

        # 按顺序生成每个章节
        for idx, chapter_id in enumerate(task.chapter_ids, 1):
            # 检查任务是否被取消
            await db_session.refresh(task)
            if task.status == 'cancelled':
                logger.info(f"🛑 批量生成任务已被取消: {batch_id}")
                return
            
            # 更新当前章节
            async with write_lock:
                task.current_chapter_id = chapter_id
                task.current_retry_count = 0  # 重置重试计数
                await db_session.commit()
            
            # 重试循环
            retry_count = 0
            chapter_success = False
            chapter = None
            last_error = None
            
            while retry_count <= task.max_retries and not chapter_success:
                try:
                    # 获取章节信息
                    chapter_result = await db_session.execute(
                        select(Chapter).where(Chapter.id == chapter_id)
                    )
                    chapter = chapter_result.scalar_one_or_none()
                    
                    if not chapter:
                        raise Exception(f"章节 {chapter_id} 不存在")
                    
                    # 更新当前章节序号和重试次数
                    async with write_lock:
                        task.current_chapter_number = chapter.chapter_number
                        task.current_retry_count = retry_count
                        await db_session.commit()
                    
                    if retry_count > 0:
                        logger.info(f"🔄 [{idx}/{task.total_chapters}] 重试生成章节 (第{retry_count}次): 第{chapter.chapter_number}章 《{chapter.title}》")
                    else:
                        logger.info(f"📝 [{idx}/{task.total_chapters}] 开始生成章节: 第{chapter.chapter_number}章 《{chapter.title}》")
                    
                    # 检查前置条件（每次都检查，确保顺序性）
                    can_generate, error_msg, _ = await check_prerequisites(db_session, chapter)
                    if not can_generate:
                        raise Exception(f"前置条件不满足: {error_msg}")
                    
                    # 生成章节内容（复用现有流式生成逻辑的核心部分），传递model参数
                    # 并获取生成后的摘要（如果生成函数支持返回）
                    generated_summary = await generate_single_chapter_for_batch(
                        db_session=db_session,
                        chapter=chapter,
                        user_id=user_id,
                        style_id=task.style_id,
                        target_word_count=task.target_word_count,
                        ai_service=ai_service,
                        write_lock=write_lock,
                        custom_model=custom_model,
                        previous_summary_context=last_generated_summary
                    )
                    
                    # 更新上一章摘要，供下一章使用
                    if generated_summary:
                        last_generated_summary = f"第{chapter.chapter_number}章《{chapter.title}》：{generated_summary}"
                        logger.info(f"📝 已更新上一章摘要上下文: {last_generated_summary[:50]}...")
                    
                    logger.info(f"✅ 章节生成完成: 第{chapter.chapter_number}章")
                    
                    # 如果启用同步分析
                    if task.enable_analysis:
                        logger.info(f"🔍 开始同步分析章节: 第{chapter.chapter_number}章")
                        
                        # 分析重试机制（最多3次）
                        analysis_retry_count = 0
                        analysis_success = False
                        last_analysis_error = None
                        
                        while analysis_retry_count < 3 and not analysis_success:
                            try:
                                if analysis_retry_count > 0:
                                    logger.info(f"🔄 重试分析章节 (第{analysis_retry_count}次): 第{chapter.chapter_number}章")
                                
                                async with write_lock:
                                    analysis_task = AnalysisTask(
                                        chapter_id=chapter_id,
                                        user_id=user_id,
                                        project_id=task.project_id,
                                        status='pending',
                                        progress=0
                                    )
                                    db_session.add(analysis_task)
                                    await db_session.commit()
                                    await db_session.refresh(analysis_task)
                                
                                # 同步执行分析，使用 review 渠道的 AI 服务
                                try:
                                    review_svc = await _resolve_task_ai_service_standalone(
                                        user_id=user_id, task_type="review", db_session=db_session
                                    )
                                except Exception:
                                    review_svc = ai_service
                                analysis_result = await analyze_chapter_background(
                                    chapter_id=chapter_id,
                                    user_id=user_id,
                                    project_id=task.project_id,
                                    task_id=analysis_task.id,
                                    ai_service=review_svc
                                )
                                
                                # 直接根据返回值判断
                                if not analysis_result:
                                    last_analysis_error = "分析函数返回失败"
                                    logger.error(f"❌ 章节分析失败: 第{chapter.chapter_number}章")
                                    raise Exception(f"章节分析失败")
                                
                                # 分析成功
                                analysis_success = True
                                logger.info(f"✅ 章节分析成功: 第{chapter.chapter_number}章")
                                
                            except Exception as analysis_error:
                                last_analysis_error = str(analysis_error)
                                analysis_retry_count += 1
                                
                                if analysis_retry_count < 3:
                                    # 还有重试机会，等待后重试
                                    wait_time = min(2 ** analysis_retry_count, 10)
                                    logger.warning(f"⏳ 分析失败，等待 {wait_time} 秒后重试...")
                                    await asyncio.sleep(wait_time)
                                else:
                                    # 达到最大重试次数，必须终止整个批量任务
                                    logger.error(f"❌ 章节分析失败，已达最大重试次数(3次): 第{chapter.chapter_number}章")
                                    
                                    # 记录失败信息
                                    failed_info = {
                                        'chapter_id': chapter_id,
                                        'chapter_number': chapter.chapter_number,
                                        'title': chapter.title,
                                        'error': f"分析失败(重试3次): {last_analysis_error}",
                                        'retry_count': 3
                                    }
                                    
                                    async with write_lock:
                                        if task.failed_chapters is None:
                                            task.failed_chapters = []
                                        task.failed_chapters.append(failed_info)
                                        
                                        # 标记任务失败并终止
                                        task.status = 'failed'
                                        task.error_message = f"第{chapter.chapter_number}章分析失败(重试3次): {last_analysis_error}"[:500]
                                        task.completed_at = datetime.now()
                                        task.current_retry_count = 0
                                        await db_session.commit()
                                    
                                    logger.error(f"🛑 批量生成中断: 第{chapter.chapter_number}章分析失败")
                                    return  # 立即终止整个批量生成任务
                    
                    # 标记成功
                    chapter_success = True
                    
                    # 更新完成数
                    async with write_lock:
                        task.completed_chapters += 1
                        task.current_retry_count = 0  # 重置重试计数
                        await db_session.commit()
                    
                    logger.info(f"✅ 进度: {task.completed_chapters}/{task.total_chapters}")
                    
                except Exception as e:
                    last_error = str(e)
                    error_msg = f"第{chapter.chapter_number if chapter else '?'}章出错: {last_error}"
                    logger.error(f"❌ {error_msg}")
                    
                    retry_count += 1
                    
                    # 如果还有重试机会，等待一小段时间后重试
                    if retry_count <= task.max_retries:
                        wait_time = min(2 ** retry_count, 10)  # 指数退避，最多等待10秒
                        logger.info(f"⏳ 等待 {wait_time} 秒后重试...")
                        await asyncio.sleep(wait_time)
                    else:
                        # 达到最大重试次数，记录失败信息
                        logger.error(f"❌ 章节生成失败，已达最大重试次数({task.max_retries}): 第{chapter.chapter_number if chapter else '?'}章")
                        
                        failed_info = {
                            'chapter_id': chapter_id,
                            'chapter_number': chapter.chapter_number if chapter else -1,
                            'title': chapter.title if chapter else '未知',
                            'error': last_error,
                            'retry_count': retry_count - 1
                        }
                        
                        async with write_lock:
                            if task.failed_chapters is None:
                                task.failed_chapters = []
                            task.failed_chapters.append(failed_info)
                            
                            # 标记任务失败并终止
                            task.status = 'failed'
                            task.error_message = f"第{chapter.chapter_number}章生成失败(重试{retry_count-1}次): {last_error}"[:500]
                            task.completed_at = datetime.now()
                            task.current_retry_count = 0
                            await db_session.commit()
                        
                        # ⚠️ 如果启用了同步分析，任何错误都应该中断任务
                        # 因为章节生成或分析失败会影响后续章节的职业更新和剧情连贯性
                        if task.enable_analysis:
                            logger.error(f"🛑 批量生成中断: 因启用同步分析，任何错误都会中断任务以确保职业信息和剧情连贯性")
                        else:
                            logger.error(f"🛑 批量生成终止于第{chapter.chapter_number}章")
                        
                        return
        
        # 全部完成
        async with write_lock:
            task.status = 'completed'
            task.completed_at = datetime.now()
            task.current_chapter_id = None
            task.current_chapter_number = None
            await db_session.commit()
        
        logger.info(f"✅ 批量生成任务全部完成: {batch_id}, 成功生成 {task.completed_chapters} 章")
        
    except Exception as e:
        logger.error(f"❌ 批量生成任务异常: {str(e)}", exc_info=True)
        if db_session and task:
            try:
                async with write_lock:
                    task.status = 'failed'
                    task.error_message = str(e)[:500]
                    task.completed_at = datetime.now()
                    await db_session.commit()
            except Exception as commit_error:
                logger.error(f"❌ 更新任务失败状态失败: {str(commit_error)}")
    finally:
        if db_session:
            await db_session.close()


async def generate_single_chapter_for_batch(
    db_session: AsyncSession,
    chapter: Chapter,
    user_id: str,
    style_id: Optional[int],
    target_word_count: int,
    ai_service: AIService,
    write_lock: Lock,
    custom_model: Optional[str] = None,
    previous_summary_context: Optional[str] = None
) -> Optional[str]:
    """
    为批量生成执行单个章节的生成（非流式）
    复用现有生成逻辑的核心部分
    
    Returns:
        生成章节的摘要（前200字）
    """
    # 获取项目信息
    project_result = await db_session.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = project_result.scalar_one_or_none()
    if not project:
        raise Exception("项目不存在")
    
    # 获取项目的大纲模式
    outline_mode = project.outline_mode if project else 'one-to-many'
    logger.info(f"📋 批量生成 - 项目大纲模式: {outline_mode}")
    
    # 获取对应的大纲（优先使用 chapter.outline_id 直接关联）
    if chapter.outline_id:
        outline_result = await db_session.execute(
            select(Outline).where(Outline.id == chapter.outline_id)
        )
    else:
        # 回退到按序号查找
        outline_result = await db_session.execute(
            select(Outline)
            .where(Outline.project_id == chapter.project_id)
            .where(Outline.order_index == chapter.chapter_number)
        )
    outline = outline_result.scalar_one_or_none()
    
    # 获取写作风格
    style_content = ""
    if style_id:
        style_result = await db_session.execute(
            select(WritingStyle).where(WritingStyle.id == style_id)
        )
        style = style_result.scalar_one_or_none()
        if style:
            if style.user_id is None or style.user_id == user_id:
                style_content = style.prompt_content or ""
    
    # 🚀 根据大纲模式选择独立的上下文构建器（批量生成）
    if outline_mode == 'one-to-one':
        # 1-1模式
        logger.info(f"🔧 批量生成 - [1-1模式] 使用 OneToOneContextBuilder")
        context_builder = OneToOneContextBuilder(
            memory_service=memory_service,
            foreshadow_service=foreshadow_service
        )
        chapter_context = await context_builder.build(
            chapter=chapter,
            project=project,
            outline=outline,
            user_id=user_id,
            db=db_session,
            target_word_count=target_word_count
        )
    else:
        # 1-N模式：使用独立的完整构建器
        logger.info(f"🔧 批量生成 - [1-N模式] 使用 OneToManyContextBuilder")
        context_builder = OneToManyContextBuilder(
            memory_service=memory_service,
            foreshadow_service=foreshadow_service
        )
        chapter_context = await context_builder.build(
            chapter=chapter,
            project=project,
            outline=outline,
            user_id=user_id,
            db=db_session,
            style_content=style_content,
            target_word_count=target_word_count
        )
    
    # 日志输出统计信息
    logger.info(f"📊 批量生成 - 优化上下文统计:")
    logger.info(f"  - 章节序号: {chapter.chapter_number}")
    logger.info(f"  - 衔接锚点长度: {len(chapter_context.continuation_point or '')} 字符")
    logger.info(f"  - 相关记忆: {chapter_context.context_stats.get('memory_count', 0)} 条")
    logger.info(f"  - 总上下文长度: {chapter_context.context_stats.get('total_length', 0)} 字符")
    
    # 🚀 根据大纲模式选择提示词模板（批量生成）
    # 统一使用 context_builder 构建的 chapter_context 结果，与单章生成保持一致
    if outline_mode == 'one-to-one':
        # 1-1模式
        if chapter_context.continuation_point:
            # 有上一章内容
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE_NEXT", user_id, db_session)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=chapter.chapter_number,
                chapter_title=chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                genre=project.genre or '未设定',
                narrative_perspective=project.narrative_perspective or '第三人称',
                previous_chapter_content=chapter_context.continuation_point,
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                relevant_memories=chapter_context.relevant_memories or '暂无相关记忆',
                previous_chapter_summary=chapter_context.previous_chapter_summary or ''
            )
        else:
            # 第一章
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_ONE", user_id, db_session)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=chapter.chapter_number,
                chapter_title=chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                genre=project.genre or '未设定',
                narrative_perspective=project.narrative_perspective or '第三人称',
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                relevant_memories=chapter_context.relevant_memories or '暂无相关记忆'
            )
    else:
        # 1-n模式：使用 context_builder 构建的结果，与单章生成保持一致
        if chapter_context.continuation_point:
            # 有前置内容，使用 WITH_CONTEXT 模板
            # 优先使用 context_builder 的摘要，其次使用传入的 previous_summary_context
            final_prev_summary = "（无上一章摘要，请根据锚点续写）"
            
            if chapter_context.previous_chapter_summary:
                final_prev_summary = chapter_context.previous_chapter_summary
            elif previous_summary_context:
                final_prev_summary = previous_summary_context
                    
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY_NEXT", user_id, db_session)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=chapter.chapter_number,
                chapter_title=chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                continuation_point=chapter_context.continuation_point,
                genre=project.genre or '未设定',
                narrative_perspective=project.narrative_perspective or '第三人称',
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                previous_chapter_summary=final_prev_summary,
                recent_chapters_context=chapter_context.recent_chapters_context or '',
                relevant_memories=chapter_context.relevant_memories or ''
            )
        else:
            # 第一章，使用无前置内容模板
            template = await PromptService.get_template("CHAPTER_GENERATION_ONE_TO_MANY", user_id, db_session)
            base_prompt = PromptService.format_prompt(
                template,
                project_title=project.title,
                chapter_number=chapter.chapter_number,
                chapter_title=chapter.title,
                chapter_outline=chapter_context.chapter_outline,
                target_word_count=target_word_count,
                genre=project.genre or '未设定',
                narrative_perspective=project.narrative_perspective or '第三人称',
                characters_info=chapter_context.chapter_characters or '暂无角色信息',
                chapter_careers=chapter_context.chapter_careers or '暂无职业信息',
                foreshadow_reminders=chapter_context.foreshadow_reminders or '暂无需要关注的伏笔',
                relevant_memories=chapter_context.relevant_memories or '暂无相关记忆'
            )
    
    # 应用写作风格
    if style_content:
        prompt = WritingStyleManager.apply_style_to_prompt(base_prompt, style_content)
    else:
        prompt = base_prompt

    # Context Agent 增强上下文（防遗忘）
    context_enhancement = await _enhance_prompt_with_context_agent(
        db_session=db_session,
        base_prompt=prompt,
        project_id=project.id,
        chapter_number=chapter.chapter_number,
        chapter_outline=chapter_context.chapter_outline,
        user_id=user_id,
        top_k=5,
    )
    prompt = context_enhancement["prompt"]
    context_system_prompt = context_enhancement["system_prompt"]
    if context_enhancement.get("enabled"):
        logger.info(
            f"✅ 批量生成 Context Agent 已注入: 检索片段={context_enhancement.get('metadata', {}).get('retrieved_chunks', 0)}, "
            f"摘要={context_enhancement.get('metadata', {}).get('summaries_count', 0)}"
        )

    # 反幻觉预处理：导演预规划 + 角色名单构建（批量生成）
    batch_narrative_perspective = project.narrative_perspective or '第三人称'
    director_plan_block = ""
    char_lists = {"forbidden": [], "new_characters": [], "introduced": []}
    try:
        char_lists = await _build_character_lists(
            db_session=db_session,
            project=project,
            chapter_outline=chapter_context.chapter_outline,
            chapter_number=chapter.chapter_number,
        )
    except Exception as e:
        logger.warning(f"⚠️ 批量生成 - 角色名单构建失败: {e}")

    if settings.enable_director_plan:
        try:
            prev_summary_for_director = "无"
            if chapter.chapter_number > 1:
                ps_result = await db_session.execute(
                    select(Chapter.summary)
                    .where(Chapter.project_id == project.id)
                    .where(Chapter.chapter_number == chapter.chapter_number - 1)
                )
                prev_summary_for_director = ps_result.scalar_one_or_none() or "无"

            director_plan = await _generate_director_plan(
                ai_service=ai_service,
                previous_summary=prev_summary_for_director,
                chapter_outline=chapter_context.chapter_outline,
                introduced_characters=", ".join(char_lists.get("introduced", [])) or "无",
                new_characters=", ".join(char_lists.get("new_characters", [])) or "无",
                narrative_perspective=batch_narrative_perspective,
                user_id=user_id,
                db_session=db_session,
            )
            if director_plan:
                director_plan_block = _format_director_plan_block(director_plan)
        except Exception as e:
            logger.warning(f"⚠️ 批量生成 - 导演预规划失败: {e}")

    # 🎨 方案一：将写作风格注入到系统提示词（批量生成）
    system_prompt_with_style = None
    if style_content:
        system_prompt_with_style = f"""【🎨 写作风格要求 - 最高优先级】

{style_content}

⚠️ 请严格遵循上述写作风格要求进行创作，这是最重要的指令！
确保在整个章节创作过程中始终保持风格的一致性。"""
        logger.info(f"✅ 批量生成 - 已将写作风格注入系统提示词（{len(style_content)}字符）")

    final_system_prompt = _merge_system_prompts(
        director_plan_block,
        system_prompt_with_style,
        context_system_prompt,
    )

    # 🔢 计算 max_tokens 限制（批量生成）
    # 中文字符约 1.5-2 个 token，使用 3 倍系数确保有足够空间完成内容
    # 字数控制主要依靠提示词约束，max_tokens 仅作为安全上限
    calculated_max_tokens = int(target_word_count * 3)
    calculated_max_tokens = max(2000, min(calculated_max_tokens, 16000))  # 限制在 2000-16000 之间
    logger.info(f"📊 批量生成 - 目标字数: {target_word_count}, 计算 max_tokens: {calculated_max_tokens}")
    
    # 非流式生成内容
    full_content = ""
    # 准备生成参数
    generate_kwargs = {
        "prompt": prompt,
        "system_prompt": final_system_prompt,
        "tool_choice": "required",
        "max_tokens": calculated_max_tokens  # 添加 max_tokens 限制
    }
    # 如果传入了自定义模型，使用指定的模型
    if custom_model:
        generate_kwargs["model"] = custom_model
        logger.info(f"  批量生成使用自定义模型: {custom_model}")
    
    # 批量生成中的流式生成（非SSE，不需要修改进度显示）
    async for chunk in ai_service.generate_text_stream(**generate_kwargs):
        full_content += chunk
    
    # 生成后质量门控（使用 review 渠道 AI 服务）
    try:
        review_ai_service_for_gate = await _resolve_task_ai_service_standalone(
            user_id=user_id, task_type="review", db_session=db_session
        )
    except Exception as resolve_err:
        logger.warning(f"⚠️ 批量生成：解析 review 渠道失败，回退 writing 渠道: {resolve_err}")
        review_ai_service_for_gate = ai_service

    quality_gate_result = await _apply_quality_gate_with_rewrite(
        db_session=db_session,
        ai_service=review_ai_service_for_gate,
        project=project,
        chapter=chapter,
        chapter_outline=chapter_context.chapter_outline,
        current_content=full_content,
        base_prompt=prompt,
        system_prompt=final_system_prompt,
        max_tokens=calculated_max_tokens,
        model=custom_model,
    )
    full_content = quality_gate_result["content"]
    if quality_gate_result.get("rewrite_count", 0) > 0:
        logger.info(
            f"🔁 批量生成质量门控触发重写 {quality_gate_result['rewrite_count']} 次，"
            f"当前评分 {quality_gate_result['overall_score']:.1f}"
        )

    # 规则护栏检查（批量生成）
    if settings.enable_guardrails:
        full_content = await _apply_guardrails_check(
            ai_service=review_ai_service_for_gate,
            content=full_content,
            forbidden_names=char_lists.get("forbidden", []),
            new_characters=char_lists.get("new_characters", []),
            narrative_perspective=batch_narrative_perspective,
            system_prompt=final_system_prompt,
            max_tokens=calculated_max_tokens,
            model=custom_model,
        )

    # 更新章节内容到数据库（使用锁保护）
    async with write_lock:
        old_word_count = chapter.word_count or 0
        chapter.content = full_content
        new_word_count = len(full_content)
        chapter.word_count = new_word_count
        chapter.status = "completed"
        
        # 更新项目字数
        project.current_words = project.current_words - old_word_count + new_word_count
        
        # 记录生成历史
        history = GenerationHistory(
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            prompt=f"批量生成: 第{chapter.chapter_number}章 {chapter.title}",
            generated_content=full_content[:500] if len(full_content) > 500 else full_content,
            model="default"
        )
        db_session.add(history)
        
        await db_session.commit()
        await db_session.refresh(chapter)
    
    logger.info(f"✅ 单章节生成完成: 第{chapter.chapter_number}章，共 {new_word_count} 字")

    service = ConsistencyEvaluationService(db_session)
    if await service.should_auto_trigger(project_id=chapter.project_id, chapter_number=chapter.chapter_number):
        project_result = await db_session.execute(select(Project).where(Project.id == chapter.project_id))
        auto_project = project_result.scalar_one_or_none()
        if auto_project:
            auto_eval = await service.create_evaluation(
                project=auto_project,
                user_id=user_id,
                scope="latest_10",
                start_chapter=max(1, chapter.chapter_number - 9),
                end_chapter=chapter.chapter_number,
                snapshot_mode="latest",
                trigger_type="auto",
                trigger_chapter_number=chapter.chapter_number,
            )
            asyncio.create_task(service.run_evaluation(auto_eval.id, user_id))

    # Data Agent：章节切片向量化 + 状态提取 + 摘要钩子同步
    data_agent_result = await _run_data_agent_pipeline(
        db_session=db_session,
        ai_service=ai_service,
        chapter=chapter,
        content=full_content,
    )
    logger.info(
        f"📦 批量生成 Data Agent 完成: chunks={data_agent_result.get('stored_count', 0)}, "
        f"state_change={'yes' if data_agent_result.get('state_change_log') else 'no'}"
    )
    
    # 生成简短摘要返回
    summary_preview = full_content[:300].replace('\n', ' ') if full_content else ""
    
    # 🔮 批量生成后自动标记计划在本章埋入的伏笔
    try:
        async with write_lock:
            plant_result = await foreshadow_service.auto_plant_pending_foreshadows(
                db=db_session,
                project_id=chapter.project_id,
                chapter_id=chapter.id,
                chapter_number=chapter.chapter_number,
                chapter_content=full_content
            )
        if plant_result.get('planted_count', 0) > 0:
            logger.info(f"🔮 批量生成 - 自动标记伏笔已埋入: {plant_result['planted_count']}个")
    except Exception as plant_error:
        logger.warning(f"⚠️ 批量生成 - 自动标记伏笔埋入失败: {str(plant_error)}")
        
    return summary_preview




# ==================== 章节重新生成相关API ====================

@router.post("/{chapter_id}/regenerate-stream", summary="流式重新生成章节内容")
async def regenerate_chapter_stream(
    chapter_id: str,
    request: Request,
    regenerate_request: ChapterRegenerateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_task_ai_service("writing"))
):
    """
    根据分析建议或自定义指令重新生成章节内容（流式返回）

    工作流程：
    1. 验证章节和分析结果
    2. 创建重新生成任务
    3. 构建修改指令
    4. 流式生成新内容
    5. 保存为版本历史
    6. 可选自动应用
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    # 验证章节存在
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    if not chapter.content or chapter.content.strip() == "":
        raise HTTPException(status_code=400, detail="章节内容为空，无法重新生成")
    
    # 验证用户权限
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取分析结果（如果使用分析建议）
    analysis = None
    if regenerate_request.modification_source in ['analysis_suggestions', 'mixed']:
        analysis_result = await db.execute(
            select(PlotAnalysis)
            .where(PlotAnalysis.chapter_id == chapter_id)
            .order_by(PlotAnalysis.created_at.desc())
            .limit(1)
        )
        analysis = analysis_result.scalar_one_or_none()
        
        if not analysis:
            raise HTTPException(status_code=404, detail="该章节暂无分析结果")
    
    # 预先获取项目上下文数据和写作风格
    async for temp_db in get_db(request):
        try:
            # 获取项目信息
            project_result = await temp_db.execute(
                select(Project).where(Project.id == chapter.project_id)
            )
            project = project_result.scalar_one_or_none()
            
            # 获取角色信息（包含职业信息）
            characters_result = await temp_db.execute(
                select(Character).where(Character.project_id == chapter.project_id)
            )
            characters = characters_result.scalars().all()
            
            # 📝 根据大纲模式智能筛选相关角色（重新生成）
            outline_mode_result = await temp_db.execute(
                select(Project.outline_mode).where(Project.id == chapter.project_id)
            )
            outline_mode = outline_mode_result.scalar_one_or_none() or 'one-to-many'
            
            filter_character_names = None
            if outline_mode == 'one-to-one':
                # 1-1模式：从outline.structure中提取characters字段（优先使用 outline_id）
                if chapter.outline_id:
                    outline_result_temp = await temp_db.execute(
                        select(Outline.structure)
                        .where(Outline.id == chapter.outline_id)
                    )
                else:
                    outline_result_temp = await temp_db.execute(
                        select(Outline.structure)
                        .where(Outline.project_id == chapter.project_id)
                        .where(Outline.order_index == chapter.chapter_number)
                    )
                outline_structure = outline_result_temp.scalar_one_or_none()
                if outline_structure:
                    try:
                        structure = json.loads(outline_structure)
                        filter_character_names = structure.get('characters', [])
                        if filter_character_names:
                            logger.info(f"📋 重新生成 - 1-1模式：从structure提取角色列表 {filter_character_names}")
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ 重新生成 - outline.structure解析失败，使用全部角色")
            else:
                # 1-n模式：从chapter.expansion_plan中提取character_focus字段
                if chapter.expansion_plan:
                    try:
                        plan = json.loads(chapter.expansion_plan)
                        filter_character_names = plan.get('character_focus', [])
                        if filter_character_names:
                            logger.info(f"📋 重新生成 - 1-n模式：从expansion_plan提取角色焦点 {filter_character_names}")
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ 重新生成 - expansion_plan解析失败，使用全部角色")
            
            characters_info_with_careers = await build_characters_info_with_careers(
                db=temp_db,
                project_id=chapter.project_id,
                characters=characters,
                filter_character_names=filter_character_names
            )
            
            # 获取章节大纲（优先使用 chapter.outline_id 直接关联）
            if chapter.outline_id:
                outline_result = await temp_db.execute(
                    select(Outline).where(Outline.id == chapter.outline_id)
                )
            else:
                # 回退到按序号查找
                outline_result = await temp_db.execute(
                    select(Outline)
                    .where(Outline.project_id == chapter.project_id)
                    .where(Outline.order_index == chapter.chapter_number)
                )
            outline = outline_result.scalar_one_or_none()
            
            # 获取写作风格
            style_content = ""
            style_id = regenerate_request.style_id
            
            # 如果没有指定风格，尝试使用项目的默认风格
            if not style_id:
                from app.models.project_default_style import ProjectDefaultStyle
                default_style_result = await temp_db.execute(
                    select(ProjectDefaultStyle.style_id)
                    .where(ProjectDefaultStyle.project_id == chapter.project_id)
                )
                default_style_id = default_style_result.scalar_one_or_none()
                if default_style_id:
                    style_id = default_style_id
                    logger.info(f"📝 使用项目默认写作风格: {style_id}")
            
            # 获取风格内容
            if style_id:
                style_result = await temp_db.execute(
                    select(WritingStyle).where(WritingStyle.id == style_id)
                )
                style = style_result.scalar_one_or_none()
                if style:
                    # 验证风格是否可用：全局预设风格（user_id为NULL）或者当前用户的自定义风格
                    if style.user_id is None or style.user_id == user_id:
                        style_content = style.prompt_content or ""
                        style_type = "全局预设" if style.user_id is None else "用户自定义"
                        logger.info(f"✅ 使用写作风格: {style.name} ({style_type})")
                    else:
                        logger.warning(f"⚠️ 风格 {style_id} 不属于当前项目，跳过")
                else:
                    logger.warning(f"⚠️ 未找到风格 {style_id}")
            else:
                logger.info("ℹ️ 未指定写作风格，使用默认提示词")
            
            # 构建项目上下文
            project_context = {
                'project_title': project.title if project else '未知',
                'genre': project.genre if project else '未设定',
                'theme': project.theme if project else '未设定',
                'narrative_perspective': project.narrative_perspective if project else '第三人称',
                'time_period': project.world_time_period if project else '未设定',
                'location': project.world_location if project else '未设定',
                'atmosphere': project.world_atmosphere if project else '未设定',
                'characters_info': characters_info_with_careers,
                'chapter_outline': outline.content if outline else chapter.summary or '暂无大纲',
                'previous_context': ''  # 可以后续扩展添加前置章节上下文
            }
        finally:
            await temp_db.close()
        break
    
    async def event_generator():
        """流式生成事件生成器"""
        db_session = None
        db_committed = False
        
        # 初始化标准进度追踪器
        from app.utils.sse_response import WizardProgressTracker
        tracker = WizardProgressTracker("章节重新生成")
        
        try:
            yield await tracker.start()
            
            # 创建独立数据库会话
            async for db_session in get_db(request):
                yield await tracker.loading("加载章节信息...", 0.5)
                
                # 创建重新生成任务
                regen_task = RegenerationTask(
                    chapter_id=chapter_id,
                    analysis_id=analysis.id if analysis else None,
                    user_id=user_id,
                    project_id=chapter.project_id,
                    modification_instructions="",  # 稍后填充
                    original_suggestions=analysis.suggestions if analysis else None,
                    selected_suggestion_indices=regenerate_request.selected_suggestion_indices,
                    custom_instructions=regenerate_request.custom_instructions,
                    style_id=regenerate_request.style_id,
                    target_word_count=regenerate_request.target_word_count,
                    focus_areas=regenerate_request.focus_areas,
                    preserve_elements=regenerate_request.preserve_elements.model_dump() if regenerate_request.preserve_elements else None,
                    status='running',
                    original_content=chapter.content,
                    original_word_count=chapter.word_count or len(chapter.content),
                    version_note=regenerate_request.version_note,
                    started_at=datetime.now()
                )
                db_session.add(regen_task)
                await db_session.commit()
                await db_session.refresh(regen_task)
                
                task_id = regen_task.id
                logger.info(f"📝 创建重新生成任务: {task_id}")
                
                yield await tracker.preparing("准备重新生成...")
                
                yield await SSEResponse.send_event(
                    event='task_created',
                    data={'task_id': task_id}
                )
                
                # 初始化重新生成器
                regenerator = ChapterRegenerator(user_ai_service)
                
                # === 生成阶段 ===
                full_content = ""
                estimated_total = regenerate_request.target_word_count or len(chapter.content)
                
                yield await tracker.generating(
                    current_chars=0,
                    estimated_total=estimated_total
                )
                
                async for event in regenerator.regenerate_with_feedback(
                    chapter=chapter,
                    analysis=analysis,
                    regenerate_request=regenerate_request,
                    project_context=project_context,
                    style_content=style_content,
                    user_id=user_id,
                    db=db_session
                ):
                    # 处理不同类型的事件
                    if event['type'] == 'chunk':
                        # 内容块
                        chunk = event['content']
                        full_content += chunk
                        yield await tracker.generating_chunk(chunk)
                        
                        # 定期更新进度
                        if len(full_content) % 500 == 0:
                            yield await tracker.generating(
                                current_chars=len(full_content),
                                estimated_total=estimated_total,
                                message=f'重新生成中... 已生成 {len(full_content)} 字'
                            )
                    elif event['type'] == 'progress':
                        # 进度更新 - 映射到对应阶段
                        progress = event.get('progress', 0)
                        message = event.get('message', '')
                        if progress < 20:
                            yield await tracker.preparing(message)
                        elif progress < 85:
                            yield await tracker.generating(
                                current_chars=len(full_content),
                                estimated_total=estimated_total,
                                message=message
                            )
                        else:
                            yield await tracker.parsing(message)
                    
                    await asyncio.sleep(0)
                
                # === 保存阶段 ===
                yield await tracker.saving("保存重新生成的内容...", 0.5)
                
                # 更新任务状态
                regen_task.status = 'completed'
                regen_task.regenerated_content = full_content
                regen_task.regenerated_word_count = len(full_content)
                regen_task.completed_at = datetime.now()
                
                # 计算差异统计
                diff_stats = regenerator.calculate_content_diff(chapter.content, full_content)
                
                await db_session.commit()
                db_committed = True
                
                yield await tracker.saving("保存完成", 0.9)
                
                # === 完成阶段 ===
                yield await tracker.complete("重新生成完成！")
                
                # 发送结果数据
                yield await tracker.result({
                    'task_id': task_id,
                    'word_count': len(full_content),
                    'version_number': regen_task.version_number,
                    'auto_applied': regenerate_request.auto_apply,
                    'diff_stats': diff_stats
                })
                
                # 发送完成信号
                yield await tracker.done()
                
                logger.info(f"✅ 章节重新生成完成: {chapter_id}, 任务: {task_id}")
                
                break
        
        except Exception as e:
            logger.error(f"❌ 重新生成失败: {str(e)}", exc_info=True)
            
            # 更新任务状态为失败
            if db_session and 'regen_task' in locals():
                try:
                    regen_task.status = 'failed'
                    regen_task.error_message = str(e)[:500]
                    regen_task.completed_at = datetime.now()
                    await db_session.commit()
                except Exception as update_error:
                    logger.error(f"更新任务失败状态失败: {str(update_error)}")
            
            yield await tracker.error(str(e))
        
    return create_sse_response(event_generator())


@router.get("/{chapter_id}/regeneration/tasks", summary="获取章节的重新生成任务列表")
async def get_regeneration_tasks(
    chapter_id: str,
    request: Request,
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db)
):
    """获取指定章节的重新生成任务历史"""
    user_id = getattr(request.state, 'user_id', None)
    
    # 验证章节存在和权限
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取任务列表
    result = await db.execute(
        select(RegenerationTask)
        .where(RegenerationTask.chapter_id == chapter_id)
        .order_by(RegenerationTask.created_at.desc())
        .limit(limit)
    )
    tasks = result.scalars().all()
    
    return {
        "chapter_id": chapter_id,
        "total": len(tasks),
        "tasks": [
            {
                "task_id": task.id,
                "status": task.status,
                "version_number": task.version_number,
                "version_note": task.version_note,
                "original_word_count": task.original_word_count,
                "regenerated_word_count": task.regenerated_word_count,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None
            }
            for task in tasks
        ]
    }


@router.put("/{chapter_id}/expansion-plan", response_model=dict, summary="更新章节规划信息")
async def update_chapter_expansion_plan(
    chapter_id: str,
    expansion_plan: ExpansionPlanUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    更新章节的展开规划信息和情节概要
    
    Args:
        chapter_id: 章节ID
        expansion_plan: 规划信息更新数据(包含summary和expansion_plan字段)
    
    Returns:
        更新后的章节规划信息
    """
    # 获取章节
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    # 验证用户权限
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 准备更新数据(排除None值)
    plan_data = expansion_plan.model_dump(exclude_unset=True, exclude_none=True)
    
    # 分离summary和expansion_plan数据
    summary_value = plan_data.pop('summary', None)
    
    # 更新summary字段(如果提供)
    if summary_value is not None:
        chapter.summary = summary_value
        logger.info(f"更新章节概要: {chapter_id}")
    
    # 更新expansion_plan字段(如果有其他字段)
    if plan_data:
        if chapter.expansion_plan:
            try:
                existing_plan = json.loads(chapter.expansion_plan)
                # 合并更新
                existing_plan.update(plan_data)
                chapter.expansion_plan = json.dumps(existing_plan, ensure_ascii=False)
            except json.JSONDecodeError:
                logger.warning(f"章节 {chapter_id} 的expansion_plan格式错误,将覆盖")
                chapter.expansion_plan = json.dumps(plan_data, ensure_ascii=False)
        else:
            chapter.expansion_plan = json.dumps(plan_data, ensure_ascii=False)
    
    await db.commit()
    await db.refresh(chapter)
    
    logger.info(f"章节规划更新成功: {chapter_id}")
    
    # 返回更新后的规划数据
    updated_plan = json.loads(chapter.expansion_plan) if chapter.expansion_plan else None
    
    return {
        "id": chapter.id,
        "summary": chapter.summary,
        "expansion_plan": updated_plan,
        "message": "规划信息更新成功"
    }


# ==================== 局部重写相关API ====================

@router.post("/{chapter_id}/partial-regenerate-stream", summary="流式局部重写选中内容")
async def partial_regenerate_stream(
    chapter_id: str,
    request: Request,
    partial_request: PartialRegenerateRequest,
    db: AsyncSession = Depends(get_db),
    user_ai_service: AIService = Depends(get_task_ai_service("writing"))
):
    """
    对章节中选中的部分内容进行流式重写
    
    工作流程：
    1. 验证章节和选中内容的有效性
    2. 截取上下文（前后文）
    3. 根据用户要求构建提示词
    4. 流式生成重写内容
    5. 返回重写结果（不自动保存，由前端决定是否应用）
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    # 验证章节存在
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    if not chapter.content or chapter.content.strip() == "":
        raise HTTPException(status_code=400, detail="章节内容为空")

    # 验证用户权限
    await verify_project_access(chapter.project_id, user_id, db)

    # 如果前端传了当前编辑器内容，优先使用它（解决编辑未保存时位置不匹配的问题）
    working_content = partial_request.current_content if partial_request.current_content else chapter.content

    # 验证位置参数
    content_length = len(working_content)
    if partial_request.start_position >= content_length:
        raise HTTPException(status_code=400, detail="起始位置超出内容范围")
    if partial_request.end_position > content_length:
        raise HTTPException(status_code=400, detail="结束位置超出内容范围")
    if partial_request.start_position >= partial_request.end_position:
        raise HTTPException(status_code=400, detail="起始位置必须小于结束位置")

    # 验证选中的文本是否匹配
    actual_selected = working_content[partial_request.start_position:partial_request.end_position]
    if actual_selected != partial_request.selected_text:
        # 位置可能有偏差，尝试在附近查找
        search_start = max(0, partial_request.start_position - 200)
        search_end = min(content_length, partial_request.end_position + 200)
        search_area = working_content[search_start:search_end]

        if partial_request.selected_text in search_area:
            # 找到了，更新位置
            offset = search_area.find(partial_request.selected_text)
            partial_request.start_position = search_start + offset
            partial_request.end_position = partial_request.start_position + len(partial_request.selected_text)
            logger.info(f"⚠️ 选中文本位置校正: {partial_request.start_position}-{partial_request.end_position}")
        else:
            raise HTTPException(
                status_code=400,
                detail="选中的文本与章节内容不匹配，请刷新页面后重试"
            )
    
    # 预先获取项目信息和写作风格
    project_result = await db.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = project_result.scalar_one_or_none()
    
    # 获取写作风格
    style_content = ""
    style_id = partial_request.style_id
    
    # 如果没有指定风格，尝试使用项目的默认风格
    if not style_id:
        from app.models.project_default_style import ProjectDefaultStyle
        default_style_result = await db.execute(
            select(ProjectDefaultStyle.style_id)
            .where(ProjectDefaultStyle.project_id == chapter.project_id)
        )
        default_style_id = default_style_result.scalar_one_or_none()
        if default_style_id:
            style_id = default_style_id
            logger.info(f"📝 局部重写 - 使用项目默认写作风格: {style_id}")
    
    # 获取风格内容
    if style_id:
        style_result = await db.execute(
            select(WritingStyle).where(WritingStyle.id == style_id)
        )
        style = style_result.scalar_one_or_none()
        if style:
            if style.user_id is None or style.user_id == user_id:
                style_content = style.prompt_content or ""
                style_type = "全局预设" if style.user_id is None else "用户自定义"
                logger.info(f"✅ 局部重写 - 使用写作风格: {style.name} ({style_type})")
            else:
                logger.warning(f"⚠️ 风格 {style_id} 不属于当前用户，跳过")
    
    async def event_generator():
        """流式生成事件生成器"""
        from app.utils.sse_response import WizardProgressTracker
        tracker = WizardProgressTracker("局部重写")
        
        try:
            yield await tracker.start()
            yield await tracker.loading("准备重写上下文...", 0.3)
            
            # 截取上下文
            context_chars = partial_request.context_chars
            start_pos = partial_request.start_position
            end_pos = partial_request.end_position
            
            # 前文：从start_pos往前截取context_chars个字符
            context_before_start = max(0, start_pos - context_chars)
            context_before = working_content[context_before_start:start_pos]

            # 后文：从end_pos往后截取context_chars个字符
            context_after_end = min(content_length, end_pos + context_chars)
            context_after = working_content[end_pos:context_after_end]
            
            # 原文
            original_text = partial_request.selected_text
            original_word_count = len(original_text)
            
            logger.info(f"📝 局部重写 - 原文: {original_word_count}字, 前文: {len(context_before)}字, 后文: {len(context_after)}字")
            
            yield await tracker.loading("构建提示词...", 0.5)
            
            # 构建字数要求
            length_requirement = ""
            if partial_request.length_mode == "similar":
                min_words = int(original_word_count * 0.8)
                max_words = int(original_word_count * 1.2)
                length_requirement = f"保持与原文相近的字数（约{original_word_count}字，允许{min_words}-{max_words}字浮动）"
            elif partial_request.length_mode == "expand":
                min_words = int(original_word_count * 1.2)
                max_words = int(original_word_count * 2.0)
                length_requirement = f"适当扩展内容（目标{min_words}-{max_words}字）"
            elif partial_request.length_mode == "condense":
                min_words = int(original_word_count * 0.5)
                max_words = int(original_word_count * 0.8)
                length_requirement = f"精简压缩内容（目标{min_words}-{max_words}字）"
            elif partial_request.length_mode == "custom" and partial_request.target_word_count:
                length_requirement = f"目标字数：约{partial_request.target_word_count}字（允许±20%浮动）"
            else:
                length_requirement = f"保持与原文相近的字数（约{original_word_count}字）"
            
            # 获取提示词模板
            template = await PromptService.get_template("PARTIAL_REGENERATE", user_id, db)
            if not template:
                template = PromptService.PARTIAL_REGENERATE
            
            # 构建提示词
            prompt = PromptService.format_prompt(
                template,
                context_before=context_before if context_before else "（这是章节开头）",
                original_word_count=original_word_count,
                selected_text=original_text,
                context_after=context_after if context_after else "（这是章节结尾）",
                user_instructions=partial_request.user_instructions,
                length_requirement=length_requirement,
                style_content=style_content if style_content else "保持与原文一致的叙事风格"
            )
            
            yield await tracker.preparing("开始生成...")
            
            # 计算 max_tokens
            if partial_request.length_mode == "expand":
                target_words = int(original_word_count * 2.0)
            elif partial_request.length_mode == "custom" and partial_request.target_word_count:
                target_words = partial_request.target_word_count
            else:
                target_words = int(original_word_count * 1.5)

            # Gemini 推理模型需要更大的 max_tokens（推理token也计入限制）
            # 为推理预留额外空间：基础token * 4 + 2000（推理缓冲）
            calculated_max_tokens = max(2000, min(int(target_words * 4) + 2000, 32000))
            
            # 流式生成
            full_content = ""
            chunk_count = 0
            
            yield await tracker.generating(
                current_chars=0,
                estimated_total=target_words
            )
            
            async for chunk in user_ai_service.generate_text_stream(
                prompt=prompt,
                max_tokens=calculated_max_tokens
            ):
                full_content += chunk
                chunk_count += 1
                
                # 发送内容块
                yield await tracker.generating_chunk(chunk)
                
                # 每5个chunk发送一次进度更新
                if chunk_count % 5 == 0:
                    yield await tracker.generating(
                        current_chars=len(full_content),
                        estimated_total=target_words,
                        message=f'正在重写中... 已生成 {len(full_content)} 字'
                    )
                
                await asyncio.sleep(0)
            
            # 清理输出（移除可能的前后缀）
            full_content = full_content.strip()
            
            # 移除常见的AI输出前缀
            prefixes_to_remove = [
                "重写后：", "重写后:", "改写后：", "改写后:",
                "以下是重写后的内容：", "以下是重写后的内容:",
                "重写内容：", "重写内容:"
            ]
            for prefix in prefixes_to_remove:
                if full_content.startswith(prefix):
                    full_content = full_content[len(prefix):].strip()
                    break
            
            # 移除首尾可能的引号
            if (full_content.startswith('"') and full_content.endswith('"')) or \
               (full_content.startswith("'") and full_content.endswith("'")):
                full_content = full_content[1:-1]
            if (full_content.startswith('「') and full_content.endswith('」')) or \
               (full_content.startswith('『') and full_content.endswith('』')):
                full_content = full_content[1:-1]
            
            new_word_count = len(full_content)
            
            logger.info(f"✅ 局部重写完成: 原文{original_word_count}字 -> 新文{new_word_count}字")
            
            # 完成
            yield await tracker.complete("重写完成！")
            
            # 发送结果数据
            yield await tracker.result({
                'new_text': full_content,
                'word_count': new_word_count,
                'original_word_count': original_word_count,
                'start_position': partial_request.start_position,
                'end_position': partial_request.end_position
            })
            
            yield await tracker.done()
            
        except Exception as e:
            logger.error(f"❌ 局部重写失败: {str(e)}", exc_info=True)
            yield await tracker.error(str(e))
    
    return create_sse_response(event_generator())


@router.post("/{chapter_id}/apply-partial-regenerate", summary="应用局部重写结果")
async def apply_partial_regenerate(
    chapter_id: str,
    request: Request,
    apply_request: dict,
    db: AsyncSession = Depends(get_db)
):
    """
    将局部重写的结果应用到章节内容中
    
    请求体：
    - new_text: 重写后的新内容
    - start_position: 原文起始位置
    - end_position: 原文结束位置
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    # 验证章节存在
    chapter_result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = chapter_result.scalar_one_or_none()
    
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    
    # 验证用户权限
    await verify_project_access(chapter.project_id, user_id, db)
    
    # 获取参数
    new_text = apply_request.get('new_text', '')
    start_position = apply_request.get('start_position', 0)
    end_position = apply_request.get('end_position', 0)
    
    if not new_text:
        raise HTTPException(status_code=400, detail="新内容不能为空")
    
    # 验证位置有效性
    content_length = len(chapter.content)
    if start_position < 0 or end_position > content_length or start_position >= end_position:
        raise HTTPException(status_code=400, detail="位置参数无效")
    
    # 构建新内容
    old_word_count = chapter.word_count or 0
    new_content = chapter.content[:start_position] + new_text + chapter.content[end_position:]
    new_word_count = len(new_content)
    
    # 更新章节
    chapter.content = new_content
    chapter.word_count = new_word_count
    
    # 更新项目字数
    project_result = await db.execute(
        select(Project).where(Project.id == chapter.project_id)
    )
    project = project_result.scalar_one_or_none()
    if project:
        project.current_words = project.current_words - old_word_count + new_word_count
    
    await db.commit()
    await db.refresh(chapter)

    service = ConsistencyEvaluationService(db)
    if await service.should_auto_trigger(project_id=chapter.project_id, chapter_number=chapter.chapter_number):
        project_result = await db.execute(select(Project).where(Project.id == chapter.project_id))
        auto_project = project_result.scalar_one_or_none()
        if auto_project:
            auto_eval = await service.create_evaluation(
                project=auto_project,
                user_id=user_id,
                scope="latest_10",
                start_chapter=max(1, chapter.chapter_number - 9),
                end_chapter=chapter.chapter_number,
                snapshot_mode="latest",
                trigger_type="auto",
                trigger_chapter_number=chapter.chapter_number,
            )
            asyncio.create_task(service.run_evaluation(auto_eval.id, user_id))

    logger.info(f"✅ 局部重写已应用: 章节{chapter_id}, {old_word_count}字 -> {new_word_count}字")
    
    return {
        "success": True,
        "chapter_id": chapter_id,
        "word_count": new_word_count,
        "old_word_count": old_word_count,
        "message": "局部重写已应用"
    }


# ==================== 状态确认 API ====================

@router.get("/{chapter_id}/pending-state", summary="获取章节待确认的状态变化")
async def get_pending_state_change(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """获取章节待确认的状态变化"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()

    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    await verify_project_access(chapter.project_id, user_id, db)

    return {
        "chapter_id": chapter_id,
        "chapter_number": chapter.chapter_number,
        "chapter_title": chapter.title,
        "pending_state_change": chapter.pending_state_change,
        "has_pending": chapter.pending_state_change is not None
    }


@router.post("/{chapter_id}/confirm-state", summary="确认章节状态变化")
async def confirm_state_change(
    chapter_id: str,
    request: Request,
    confirmed_changes: Dict[str, Any],
    db: AsyncSession = Depends(get_db)
):
    """确认章节的状态变化，将确认的变化写入全局状态"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()

    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    await verify_project_access(chapter.project_id, user_id, db)

    service = ChapterMemoryService(db=db)
    success = await service.confirm_state_change(chapter_id, confirmed_changes)

    if not success:
        raise HTTPException(status_code=500, detail="确认状态变化失败")

    return {
        "success": True,
        "message": "状态变化已确认并更新到全局状态",
        "confirmed_changes": confirmed_changes
    }


@router.post("/{chapter_id}/reject-state", summary="拒绝章节状态变化")
async def reject_state_change(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """拒绝章节的状态变化，清除待确认状态"""
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()

    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    await verify_project_access(chapter.project_id, user_id, db)

    service = ChapterMemoryService(db=db)
    success = await service.reject_state_change(chapter_id)

    if not success:
        raise HTTPException(status_code=500, detail="拒绝状态变化失败")

    return {
        "success": True,
        "message": "状态变化已拒绝"
    }


@router.post("/{chapter_id}/reextract-state", summary="重新提取章节状态")
async def reextract_chapter_state(
    chapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    重新提取章节的状态变更（用于修复缺失的状态分析）

    工作流程：
    1. 验证章节存在且有内容
    2. 调用 ChapterMemoryService 重新提取状态
    3. 更新章节的 state_change_log 和 pending_state_change
    4. 重建项目的世界状态
    """
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    # 验证章节存在
    result = await db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
    )
    chapter = result.scalar_one_or_none()

    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    await verify_project_access(chapter.project_id, user_id, db)

    if not chapter.content or len(chapter.content.strip()) < 100:
        raise HTTPException(status_code=400, detail="章节内容过短，无法提取状态")

    # 获取用户设置并创建 AI 服务
    from app.models.settings import Settings
    from app.api.settings import create_user_ai_service_with_mcp
    from app.models.mcp_plugin import MCPPlugin

    settings_result = await db.execute(
        select(Settings).where(Settings.user_id == user_id)
    )
    settings = settings_result.scalar_one_or_none()

    if not settings:
        raise HTTPException(status_code=400, detail="用户未配置AI设置")

    # 查询用户的MCP插件状态
    mcp_result = await db.execute(
        select(MCPPlugin).where(MCPPlugin.user_id == user_id)
    )
    mcp_plugins = mcp_result.scalars().all()
    enable_mcp = any(plugin.enabled for plugin in mcp_plugins) if mcp_plugins else False

    ai_service = create_user_ai_service_with_mcp(
        api_provider=settings.api_provider,
        api_key=settings.api_key,
        api_base_url=settings.api_base_url,
        model_name=settings.llm_model,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        user_id=user_id,
        db_session=db,
        system_prompt=settings.system_prompt,
        enable_mcp=enable_mcp,
    )

    # 创建 ChapterMemoryService 并重新提取状态
    from app.services.chapter_memory_service import ChapterMemoryService
    service = ChapterMemoryService(db=db, ai_service=ai_service)

    try:
        # 调用内部方法提取状态
        state_change = await service._extract_state_change(
            content=chapter.content,
            project_id=chapter.project_id,
            chapter_number=chapter.chapter_number
        )

        if not state_change:
            raise HTTPException(status_code=500, detail="状态提取失败，AI 返回为空")

        # 保存状态变更
        success = await service._save_chapter_state_change(
            chapter_id,
            state_change,
            content_hash=service._build_content_hash(chapter.content or "")
        )

        if not success:
            raise HTTPException(status_code=500, detail="保存状态变更失败")

        # 重建世界状态
        await service._rebuild_world_state_from_chapters(chapter.project_id)

        await db.commit()

        return {
            "success": True,
            "message": "状态重新提取成功",
            "state_change": state_change
        }

    except HTTPException:
        # 重新抛出 HTTPException，不要包装
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"❌ 重新提取状态失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"重新提取状态失败: {str(e)}")


@router.post("/project/{project_id}/batch-vectorize", summary="批量向量化项目章节")
async def batch_vectorize_chapters(
    project_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    对项目下所有有内容的章节批量执行切片+Embedding向量化存储。
    适用于导入小说后章节未触发向量化的场景。
    任务在后台执行，立即返回。
    """
    user_id = getattr(request.state, 'user_id', None)
    await verify_project_access(project_id, user_id, db)

    # 查询所有有内容的章节
    result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == project_id)
        .where(Chapter.content.isnot(None))
        .where(func.length(Chapter.content) > 100)
        .order_by(Chapter.chapter_number)
    )
    chapters = result.scalars().all()

    if not chapters:
        return {"success": True, "message": "没有需要向量化的章节", "total": 0}

    chapter_infos = [
        {
            "id": ch.id,
            "chapter_number": ch.chapter_number,
            "content": ch.content,
            "project_id": ch.project_id,
        }
        for ch in chapters
    ]
    total = len(chapter_infos)

    async def _batch_vectorize(infos: list, bg_user_id: str):
        """后台批量向量化"""
        from app.database import get_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as BgAsyncSession

        engine = await get_engine(bg_user_id)
        BgSessionLocal = async_sessionmaker(
            engine, class_=BgAsyncSession, expire_on_commit=False
        )

        success_count = 0
        fail_count = 0
        for info in infos:
            try:
                async with BgSessionLocal() as bg_db:
                    service = ChapterMemoryService(db=bg_db)
                    res = await service.process_and_store_chapter(
                        chapter_id=info["id"],
                        content=info["content"],
                        chapter_number=info["chapter_number"],
                        project_id=info["project_id"],
                    )
                    stored = res.get("stored_count", 0)
                    logger.info(
                        f"📦 批量向量化 第{info['chapter_number']}章: "
                        f"chunks={stored}"
                    )
                    success_count += 1
            except Exception as e:
                fail_count += 1
                logger.error(
                    f"❌ 批量向量化 第{info['chapter_number']}章失败: {e}"
                )

        logger.info(
            f"✅ 批量向量化完成: 成功={success_count}, 失败={fail_count}, "
            f"总计={success_count + fail_count}"
        )

    background_tasks.add_task(_batch_vectorize, chapter_infos, user_id)

    return {
        "success": True,
        "message": f"已启动后台批量向量化，共 {total} 章",
        "total": total,
    }

