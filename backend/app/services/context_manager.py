"""Context Agent - 双Agent架构的上下文管理服务

负责在章节生成前构建增强上下文，包括：
1. pgvector 向量检索相关记忆片段
2. 实体识别预检索（精确匹配）
3. 加载世界观设定
4. 分层摘要（最近3章详细摘要 + 关键事件时间轴）
5. 加载活跃伏笔上下文
6. 上章钩子强制响应
7. 构建强制遵守历史设定的 System Prompt
"""
import json
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, desc
from openai import AsyncOpenAI

from app.models.project import Project
from app.models.chapter import Chapter
from app.models.chapter_memory import ChapterMemory
from app.models.memory import StoryMemory
from app.models.foreshadow import Foreshadow
from app.models.key_event import KeyEvent
from app.logger import get_logger
from app.config import settings, EMBEDDING_DIMENSIONS

logger = get_logger(__name__)


@dataclass
class ContextResult:
    """Context Agent 返回的上下文结果"""
    system_prompt: str = ""  # 强制遵守历史设定的指令
    context_string: str = ""  # 结构化上下文
    metadata: Dict[str, Any] = field(default_factory=dict)  # 统计信息


class ContextAgent:
    """Context Agent - 负责构建章节生成的增强上下文"""

    def __init__(self, db: AsyncSession, embedding_model=None, use_pgvector: bool = True):
        """
        初始化 Context Agent

        Args:
            db: 数据库会话
            embedding_model: 可选 Embedding API 客户端（AsyncOpenAI）
            use_pgvector: 是否使用 pgvector 进行向量检索
        """
        self.db = db
        self._embedding_client = embedding_model if isinstance(embedding_model, AsyncOpenAI) else None
        if embedding_model is not None and self._embedding_client is None:
            logger.warning("⚠️ embedding_model 参数已弃用，仅支持 AsyncOpenAI 客户端")
        self.use_pgvector = use_pgvector

    @property
    def embedding_client(self) -> Optional[AsyncOpenAI]:
        """延迟初始化 Embedding API 客户端"""
        if self._embedding_client is None:
            try:
                api_key = settings.embedding_api_key or settings.openai_api_key
                base_url = settings.embedding_base_url or settings.openai_base_url
                if not api_key:
                    logger.warning("⚠️ 未配置 EMBEDDING_API_KEY 或 OPENAI_API_KEY，跳过向量检索")
                    return None

                self._embedding_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
                config_source = "环境变量 EMBEDDING_API_KEY" if settings.embedding_api_key else "环境变量 OPENAI_API_KEY"
                logger.info(f"✅ Context Agent 初始化 Embedding 客户端: {config_source}")
            except Exception as e:
                logger.warning(f"⚠️ Embedding 客户端初始化失败: {e}")
                return None
        return self._embedding_client

    async def _get_query_embedding(self, text: str) -> Optional[List[float]]:
        """使用 Embedding API 生成查询向量，失败时安全降级"""
        if not text:
            return None

        client = self.embedding_client
        if not client:
            return None

        try:
            params = {
                "model": settings.embedding_model,
                "input": text,
            }
            if "text-embedding-3" in settings.embedding_model:
                params["dimensions"] = EMBEDDING_DIMENSIONS

            response = await client.embeddings.create(**params)
            embedding = response.data[0].embedding
            return embedding or None
        except Exception as e:
            logger.warning(f"⚠️ Embedding API 调用失败，跳过向量检索: {e}")
            return None

    async def build_generation_context(
        self,
        project_id: str,
        chapter_number: int,
        chapter_outline: str,
        user_id: str,
        character_names: Optional[List[str]] = None,
        top_k: int = 5
    ) -> ContextResult:
        """
        构建章节生成的增强上下文

        Args:
            project_id: 项目ID
            chapter_number: 当前章节号
            chapter_outline: 当前章节大纲
            user_id: 用户ID
            character_names: 本章涉及的角色名列表
            top_k: 向量检索返回的最大片段数

        Returns:
            ContextResult: 包含 system_prompt, context_string, metadata
        """
        result = ContextResult()
        result.metadata = {
            "project_id": project_id,
            "chapter_number": chapter_number,
            "retrieved_chunks": 0,
            "worldbuilding_loaded": False,
            "summaries_count": 0,
            "foreshadows_count": 0,
            "previous_hook": None
        }

        try:
            # 0. 加载上章钩子（需要强制响应）
            previous_hook = await self._load_previous_hook(project_id, chapter_number)
            result.metadata["previous_hook"] = previous_hook

            # 1. 向量检索相关记忆片段
            relevant_chunks = await self._search_pgvector(
                project_id=project_id,
                query_text=chapter_outline,
                top_k=top_k,
                chapter_number=chapter_number
            )
            result.metadata["retrieved_chunks"] = len(relevant_chunks)

            # 2. 加载世界观设定
            worldbuilding = await self._load_worldbuilding(project_id)
            result.metadata["worldbuilding_loaded"] = bool(worldbuilding)

            # 3. 获取最近章节摘要
            recent_summaries = await self._load_recent_summaries(
                project_id=project_id,
                current_chapter=chapter_number,
                count=3
            )
            result.metadata["summaries_count"] = len(recent_summaries)

            # 4. 加载活跃伏笔上下文
            foreshadow_context = await self._load_foreshadow_context(
                project_id=project_id,
                chapter_number=chapter_number
            )
            result.metadata["foreshadows_count"] = len(foreshadow_context)

            # 5. 加载关键事件时间轴（分层摘要第二层）
            key_events_timeline = await self._load_key_events_timeline(
                project_id=project_id,
                current_chapter=chapter_number,
                min_importance=4  # 只加载重要度>=4的事件
            )
            result.metadata["key_events_count"] = len(key_events_timeline)

            # 6. 格式化上下文字符串
            result.context_string = self._format_context(
                relevant_chunks=relevant_chunks,
                worldbuilding=worldbuilding,
                recent_summaries=recent_summaries,
                foreshadow_context=foreshadow_context,
                character_names=character_names,
                previous_hook=previous_hook,
                key_events_timeline=key_events_timeline
            )

            # 7. 构建 System Prompt
            result.system_prompt = self._build_system_prompt(
                worldbuilding=worldbuilding,
                character_names=character_names
            )

            logger.info(
                f"✅ Context Agent 构建完成 - 项目:{project_id[:8]}, 章节:{chapter_number}, "
                f"检索片段:{len(relevant_chunks)}, 摘要:{len(recent_summaries)}, "
                f"伏笔:{len(foreshadow_context)}, 关键事件:{len(key_events_timeline)}"
            )

        except Exception as e:
            logger.error(f"❌ Context Agent 构建失败: {e}", exc_info=True)
            result.metadata["error"] = str(e)

        return result

    async def _search_pgvector(
        self,
        project_id: str,
        query_text: str,
        top_k: int = 5,
        chapter_number: int = None
    ) -> List[Dict[str, Any]]:
        """
        使用 pgvector 进行向量相似度检索

        Args:
            project_id: 项目ID
            query_text: 查询文本
            top_k: 返回结果数量
            chapter_number: 当前章节号（用于过滤，只检索之前章节的内容）

        Returns:
            相关记忆片段列表
        """
        if not self.use_pgvector:
            logger.warning("⚠️ pgvector 未启用，跳过向量检索")
            return []

        try:
            # 生成查询向量
            query_embedding = await self._get_query_embedding(query_text)
            if not query_embedding:
                logger.warning("⚠️ 向量生成失败或未配置 Embedding，跳过向量检索")
                return []

            # 构建 SQL 查询（使用余弦相似度）
            # 只检索当前章节之前的内容
            chapter_filter = ""
            if chapter_number and chapter_number > 1:
                chapter_filter = f"AND story_timeline < {chapter_number}"

            sql = text(f"""
                SELECT
                    id,
                    content,
                    memory_type,
                    characters_mentioned,
                    story_timeline,
                    importance_score,
                    1 - (embedding <=> :query_embedding::vector) as similarity
                FROM chapter_memories
                WHERE project_id = :project_id
                    AND embedding IS NOT NULL
                    {chapter_filter}
                ORDER BY embedding <=> :query_embedding::vector
                LIMIT :top_k
            """)

            result = await self.db.execute(
                sql,
                {
                    "project_id": project_id,
                    "query_embedding": str(query_embedding),
                    "top_k": top_k
                }
            )

            chunks = []
            for row in result.fetchall():
                chunks.append({
                    "id": row.id,
                    "content": row.content,
                    "memory_type": row.memory_type,
                    "characters": row.characters_mentioned or [],
                    "chapter": row.story_timeline,
                    "importance": row.importance_score or 0.5,
                    "similarity": float(row.similarity) if row.similarity else 0.0
                })

            logger.info(f"🔍 pgvector 检索完成: 找到 {len(chunks)} 个相关片段")
            return chunks

        except Exception as e:
            logger.error(f"❌ pgvector 检索失败: {e}", exc_info=True)
            return []

    async def _load_worldbuilding(self, project_id: str) -> Dict[str, Any]:
        """加载项目的世界观设定和全局状态"""
        try:
            result = await self.db.execute(
                select(Project).where(Project.id == project_id)
            )
            project = result.scalar_one_or_none()

            if not project:
                return {}

            return {
                "title": project.title,
                "theme": project.theme,
                "genre": project.genre,
                "time_period": getattr(project, 'world_time_period', None),
                "location": getattr(project, 'world_location', None),
                "atmosphere": getattr(project, 'world_atmosphere', None),
                "rules": getattr(project, 'world_rules', None),
                "narrative_perspective": getattr(project, 'narrative_perspective', '第三人称'),
                "world_state": getattr(project, 'world_state', None)  # 全局状态容器
            }
        except Exception as e:
            logger.error(f"❌ 加载世界观失败: {e}")
            return {}

    async def _load_recent_summaries(
        self,
        project_id: str,
        current_chapter: int,
        count: int = 3
    ) -> List[Dict[str, Any]]:
        """加载最近章节的摘要"""
        try:
            # 获取当前章节之前的最近 N 章
            start_chapter = max(1, current_chapter - count)

            result = await self.db.execute(
                select(Chapter)
                .where(Chapter.project_id == project_id)
                .where(Chapter.chapter_number >= start_chapter)
                .where(Chapter.chapter_number < current_chapter)
                .order_by(desc(Chapter.chapter_number))
                .limit(count)
            )

            chapters = result.scalars().all()
            summaries = []

            for chapter in chapters:
                summaries.append({
                    "chapter_number": chapter.chapter_number,
                    "title": chapter.title,
                    "summary": chapter.summary or "(无摘要)"
                })

            # 按章节号正序排列
            summaries.sort(key=lambda x: x["chapter_number"])
            return summaries

        except Exception as e:
            logger.error(f"❌ 加载章节摘要失败: {e}")
            return []

    async def extract_entities_from_outline(
        self,
        outline: str,
        ai_service: Any
    ) -> List[str]:
        """
        从大纲中提取实体（人名、地名、物品名）用于精确检索

        Args:
            outline: 章节大纲
            ai_service: AI 服务实例

        Returns:
            实体名称列表
        """
        if not outline or len(outline) < 10:
            return []

        try:
            import json
            prompt = f"""请从以下章节大纲中提取所有实体名称（人名、地名、物品名、组织名等专有名词）。

【章节大纲】
{outline[:2000]}

请直接输出 JSON 数组格式，例如：["张三", "青云山", "玄铁剑"]
只输出 JSON，不要其他内容。如果没有实体，输出空数组 []"""

            response = await ai_service.generate_text(
                prompt=prompt,
                system_prompt="你是一个实体识别助手，只输出JSON数组。",
                max_tokens=500
            )

            # 解析响应
            response_text = response.get("content", "") if isinstance(response, dict) else str(response)

            # 尝试提取 JSON
            if "[" in response_text and "]" in response_text:
                start = response_text.index("[")
                end = response_text.rindex("]") + 1
                entities = json.loads(response_text[start:end])
                if isinstance(entities, list):
                    logger.info(f"🔍 实体识别完成: {entities}")
                    return entities[:20]  # 限制数量

            return []

        except Exception as e:
            logger.warning(f"⚠️ 实体识别失败: {e}")
            return []

    async def precise_entity_search(
        self,
        project_id: str,
        entities: List[str],
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        基于实体名称进行精确匹配检索

        Args:
            project_id: 项目ID
            entities: 实体名称列表
            limit: 返回数量限制

        Returns:
            匹配的记忆片段列表
        """
        if not entities:
            return []

        try:
            from app.models.chapter_memory import ChapterMemory

            results = []
            for entity in entities[:10]:  # 限制实体数量
                # 使用 LIKE 进行精确匹配
                query = select(ChapterMemory).where(
                    ChapterMemory.project_id == project_id,
                    ChapterMemory.content.ilike(f"%{entity}%")
                ).order_by(desc(ChapterMemory.chapter_number)).limit(3)

                result = await self.db.execute(query)
                memories = result.scalars().all()

                for mem in memories:
                    results.append({
                        "content": mem.content,
                        "chapter_number": mem.chapter_number,
                        "entity_matched": entity,
                        "source": "precise_search"
                    })

            # 去重并限制数量
            seen = set()
            unique_results = []
            for r in results:
                key = r["content"][:100]
                if key not in seen:
                    seen.add(key)
                    unique_results.append(r)
                    if len(unique_results) >= limit:
                        break

            logger.info(f"🎯 精确检索完成: {len(unique_results)} 条匹配")
            return unique_results

        except Exception as e:
            logger.warning(f"⚠️ 精确检索失败: {e}")
            return []

    async def _load_previous_hook(
        self,
        project_id: str,
        current_chapter: int
    ) -> Optional[Dict[str, Any]]:
        """
        加载上一章的结尾钩子

        Args:
            project_id: 项目ID
            current_chapter: 当前章节号

        Returns:
            如果上章有 must_respond_next=True 的钩子，返回钩子信息，否则返回 None
        """
        if current_chapter <= 1:
            return None

        try:
            result = await self.db.execute(
                select(Chapter.end_hook, Chapter.title, Chapter.chapter_number)
                .where(Chapter.project_id == project_id)
                .where(Chapter.chapter_number == current_chapter - 1)
            )
            row = result.first()

            if row and row.end_hook and row.end_hook.get("must_respond_next"):
                hook_info = {
                    "chapter_number": row.chapter_number,
                    "chapter_title": row.title,
                    "hook_type": row.end_hook.get("type"),
                    "hook_content": row.end_hook.get("content")
                }
                logger.info(f"🎣 加载上章钩子: 第{row.chapter_number}章 - {row.end_hook.get('type')}")
                return hook_info
            return None

        except Exception as e:
            logger.error(f"❌ 加载上章钩子失败: {e}")
            return None

    async def _load_key_events_timeline(
        self,
        project_id: str,
        current_chapter: int,
        min_importance: int = 4
    ) -> List[Dict[str, Any]]:
        """
        加载关键事件时间轴（分层摘要的第二层）

        Args:
            project_id: 项目ID
            current_chapter: 当前章节号
            min_importance: 最小重要程度（1-5）

        Returns:
            关键事件列表
        """
        try:
            result = await self.db.execute(
                select(KeyEvent)
                .where(KeyEvent.project_id == project_id)
                .where(KeyEvent.chapter_number < current_chapter)
                .where(KeyEvent.importance >= min_importance)
                .order_by(KeyEvent.chapter_number)
                .limit(30)  # 限制数量
            )
            events = result.scalars().all()

            timeline = []
            for event in events:
                timeline.append({
                    "chapter_number": event.chapter_number,
                    "event_type": event.event_type,
                    "title": event.title,
                    "description": event.description,
                    "importance": event.importance,
                    "is_resolved": event.is_resolved
                })

            logger.info(f"📅 加载关键事件时间轴: {len(timeline)} 个事件")
            return timeline

        except Exception as e:
            logger.warning(f"⚠️ 加载关键事件时间轴失败: {e}")
            return []

    async def _load_foreshadow_context(
        self,
        project_id: str,
        chapter_number: int
    ) -> List[Dict[str, Any]]:
        """加载活跃伏笔上下文"""
        try:
            # 查询已埋入但未回收的伏笔
            result = await self.db.execute(
                select(Foreshadow)
                .where(Foreshadow.project_id == project_id)
                .where(Foreshadow.status.in_(['planted', 'pending']))
                .where(Foreshadow.include_in_context == True)
            )

            foreshadows = result.scalars().all()
            context = []

            for fs in foreshadows:
                # 计算是否需要提醒回收
                should_remind = False
                if fs.target_resolve_chapter_number:
                    remind_threshold = fs.target_resolve_chapter_number - (fs.remind_before_chapters or 5)
                    should_remind = chapter_number >= remind_threshold

                context.append({
                    "id": fs.id,
                    "title": fs.title,
                    "content": fs.content,
                    "status": fs.status,
                    "plant_chapter": fs.plant_chapter_number,
                    "target_resolve_chapter": fs.target_resolve_chapter_number,
                    "importance": fs.importance or 0.5,
                    "should_remind": should_remind,
                    "related_characters": fs.related_characters or []
                })

            return context

        except Exception as e:
            logger.error(f"❌ 加载伏笔上下文失败: {e}")
            return []

    def _format_context(
        self,
        relevant_chunks: List[Dict[str, Any]],
        worldbuilding: Dict[str, Any],
        recent_summaries: List[Dict[str, Any]],
        foreshadow_context: List[Dict[str, Any]],
        character_names: Optional[List[str]] = None,
        previous_hook: Optional[Dict[str, Any]] = None,
        key_events_timeline: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """格式化上下文字符串"""
        parts = []

        # 上章钩子强制响应提示（放在最前面）
        if previous_hook:
            parts.append(f"""【⚠️ 必须响应的上章钩子】
上一章《{previous_hook['chapter_title']}》结尾留下的{previous_hook['hook_type']}：
"{previous_hook['hook_content']}"
>>> 本章开头必须对此进行回应，不得忽略！""")

        # 世界观设定
        if worldbuilding:
            wb_parts = []
            if worldbuilding.get("time_period"):
                wb_parts.append(f"时间背景: {worldbuilding['time_period']}")
            if worldbuilding.get("location"):
                wb_parts.append(f"地理位置: {worldbuilding['location']}")
            if worldbuilding.get("atmosphere"):
                wb_parts.append(f"氛围基调: {worldbuilding['atmosphere']}")
            if worldbuilding.get("rules"):
                wb_parts.append(f"世界规则: {worldbuilding['rules']}")

            if wb_parts:
                parts.append("【世界观设定】\n" + "\n".join(wb_parts))

        # 全局状态容器（State Machine）
        world_state = worldbuilding.get("world_state") if worldbuilding else None
        if world_state:
            state_parts = []
            if world_state.get("current_location"):
                state_parts.append(f"当前位置: {world_state['current_location']}")
            if world_state.get("inventory"):
                items = ", ".join(world_state["inventory"][:10])  # 最多显示10个物品
                state_parts.append(f"持有物品: {items}")
            if world_state.get("relationships"):
                rel_lines = [f"  - {k}: {v}" for k, v in list(world_state["relationships"].items())[:5]]
                state_parts.append("人物关系:\n" + "\n".join(rel_lines))
            if world_state.get("last_time_reference"):
                state_parts.append(f"时间线: {world_state['last_time_reference']}")
            # 其他状态属性
            for key, value in world_state.items():
                if key not in ["current_location", "inventory", "relationships", "last_time_reference"]:
                    if isinstance(value, (int, float)):
                        state_parts.append(f"{key}: {value}")

            if state_parts:
                parts.append("【当前全局状态】\n" + "\n".join(state_parts))

        # 最近章节摘要
        if recent_summaries:
            summary_lines = []
            for s in recent_summaries:
                summary_lines.append(f"第{s['chapter_number']}章《{s['title']}》: {s['summary']}")
            parts.append("【最近剧情回顾】\n" + "\n".join(summary_lines))

        # 相关记忆片段
        if relevant_chunks:
            chunk_lines = []
            for i, chunk in enumerate(relevant_chunks, 1):
                chunk_lines.append(
                    f"[片段{i}] (第{chunk['chapter']}章, 相似度:{chunk['similarity']:.2f})\n{chunk['content'][:200]}..."
                )
            parts.append("【相关历史片段】\n" + "\n\n".join(chunk_lines))

        # 活跃伏笔
        if foreshadow_context:
            fs_lines = []
            for fs in foreshadow_context:
                status_emoji = "🔔" if fs["should_remind"] else "📌"
                fs_lines.append(
                    f"{status_emoji} {fs['title']}: {fs['content'][:100]}..."
                    + (f" (计划在第{fs['target_resolve_chapter']}章回收)" if fs['target_resolve_chapter'] else "")
                )
            parts.append("【活跃伏笔提醒】\n" + "\n".join(fs_lines))

        # 关键事件时间轴（分层摘要第二层）
        if key_events_timeline:
            event_lines = []
            for event in key_events_timeline:
                importance_stars = "★" * event.get("importance", 3)
                resolved_mark = "✓" if event.get("is_resolved") else ""
                event_lines.append(
                    f"第{event['chapter_number']}章 [{event['event_type']}] {importance_stars} "
                    f"{event['title']}: {event.get('description', '')[:50]} {resolved_mark}"
                )
            parts.append("【📅 关键事件时间轴】\n" + "\n".join(event_lines))

        return "\n\n".join(parts) if parts else ""

    def _build_system_prompt(
        self,
        worldbuilding: Dict[str, Any],
        character_names: Optional[List[str]] = None
    ) -> str:
        """构建强制遵守历史设定的 System Prompt"""
        prompt_parts = [
            "【Context Agent 增强指令】",
            "你必须严格遵守以下历史设定和上下文约束："
        ]

        if worldbuilding:
            if worldbuilding.get("genre"):
                prompt_parts.append(f"- 类型风格: 保持 {worldbuilding['genre']} 类型的叙事风格")
            if worldbuilding.get("narrative_perspective"):
                prompt_parts.append(f"- 叙事视角: 使用 {worldbuilding['narrative_perspective']}")
            if worldbuilding.get("atmosphere"):
                prompt_parts.append(f"- 氛围基调: 维持 {worldbuilding['atmosphere']} 的整体氛围")

        prompt_parts.extend([
            "",
            "【一致性要求】",
            "- 角色行为必须与已建立的性格特征一致",
            "- 不得与前文已确立的事实产生矛盾",
            "- 适当回应和推进已埋下的伏笔",
            "- 保持叙事节奏和风格的连贯性"
        ])

        return "\n".join(prompt_parts)
