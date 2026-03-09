"""章节记忆存储服务 - Data Agent

负责在章节生成后处理和存储记忆切片到 pgvector：
1. 将章节内容切分为 300-500 字的片段
2. 使用独立的 Embedding API 生成向量
3. 批量存储到 chapter_memories 表
4. 提取状态变更并更新全局状态
"""
import uuid
import re
import json
import hashlib
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, text

from openai import AsyncOpenAI

from app.models.chapter_memory import ChapterMemory
from app.models.chapter import Chapter
from app.models.project import Project
from app.config import settings, EMBEDDING_DIMENSIONS
from app.services.qdrant_service import QdrantService
from app.services.world_state_utils import (
    get_suppressed_state_keys,
    is_absolute_state_key,
    normalize_state_change_payload,
    normalize_status_changes,
    normalize_world_state_structure,
    set_suppressed_state_keys,
)
from qdrant_client.http import models as qdrant_models
from app.logger import get_logger

logger = get_logger(__name__)


class ChapterMemoryService:
    """章节记忆存储服务 - 将章节内容切片并存储到 pgvector"""

    CHUNK_SIZE = 400  # 目标切片大小（字符）
    CHUNK_OVERLAP = 50  # 切片重叠（字符）
    MIN_CHUNK_SIZE = 100  # 最小切片大小

    @staticmethod
    def _serialize_state_payload(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    STATE_META_KEY = "_meta"
    STATE_HASH_KEY = "content_hash"
    def __init__(
        self,
        db: AsyncSession,
        ai_service=None,
        user_embedding_config: Optional[Dict[str, str]] = None
    ):
        """
        初始化服务

        Args:
            db: 数据库会话
            ai_service: 主 AI 服务实例（用于状态提取）
            user_embedding_config: 用户级别的 Embedding 配置，包含：
                - embedding_api_key: API 密钥
                - embedding_base_url: API 地址
                - embedding_model: 模型名称
        """
        self.db = db
        self.ai_service = ai_service
        self.user_embedding_config = user_embedding_config or {}
        self._embedding_client: Optional[AsyncOpenAI] = None
        self._embedding_model: Optional[str] = None
        self._qdrant_service: Optional[QdrantService] = None

    def _build_content_hash(self, content: str) -> str:
        normalized_content = (content or "").replace("\r\n", "\n").strip()
        return hashlib.sha1(normalized_content.encode("utf-8")).hexdigest()

    async def _get_existing_state_change_log(self, chapter_id: str) -> Optional[Dict[str, Any]]:
        result = await self.db.execute(
            select(Chapter.state_change_log).where(Chapter.id == chapter_id)
        )
        state_change_log = result.scalar_one_or_none()
        if isinstance(state_change_log, dict):
            return state_change_log
        return None

    async def _get_project_suppressed_state_keys(self, project_id: str) -> set[str]:
        result = await self.db.execute(
            select(Project.world_state).where(Project.id == project_id)
        )
        world_state = result.scalar_one_or_none()
        return get_suppressed_state_keys(world_state)

    def _state_change_matches_content(
        self,
        state_change_log: Optional[Dict[str, Any]],
        content_hash: str
    ) -> bool:
        if not isinstance(state_change_log, dict):
            return False
        meta = state_change_log.get(self.STATE_META_KEY)
        if not isinstance(meta, dict):
            return False
        return meta.get(self.STATE_HASH_KEY) == content_hash

    def _attach_state_meta(
        self,
        state_change: Dict[str, Any],
        content_hash: Optional[str]
    ) -> Dict[str, Any]:
        normalized_state_change = dict(state_change)
        meta = normalized_state_change.get(self.STATE_META_KEY)
        normalized_meta = dict(meta) if isinstance(meta, dict) else {}
        if content_hash:
            normalized_meta[self.STATE_HASH_KEY] = content_hash
        if normalized_meta:
            normalized_state_change[self.STATE_META_KEY] = normalized_meta
        return normalized_state_change

    @property
    def embedding_model(self) -> str:
        """获取 Embedding 模型名称"""
        if self._embedding_model is None:
            # 优先使用用户配置，否则使用全局配置
            self._embedding_model = (
                self.user_embedding_config.get("embedding_model") or
                settings.embedding_model
            )
        return self._embedding_model

    @property
    def embedding_client(self) -> AsyncOpenAI:
        """延迟初始化独立的 Embedding 客户端"""
        if self._embedding_client is None:
            # 优先使用用户配置，否则使用全局配置
            api_key = (
                self.user_embedding_config.get("embedding_api_key") or
                settings.embedding_api_key or
                settings.openai_api_key
            )
            base_url = (
                self.user_embedding_config.get("embedding_base_url") or
                settings.embedding_base_url or
                settings.openai_base_url
            )

            if not api_key:
                raise ValueError("未配置 EMBEDDING_API_KEY 或 OPENAI_API_KEY")

            self._embedding_client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url
            )

            # 详细日志显示配置来源
            config_source = "用户配置" if self.user_embedding_config.get("embedding_api_key") else (
                "环境变量 EMBEDDING_API_KEY" if settings.embedding_api_key else "环境变量 OPENAI_API_KEY"
            )
            logger.info(f"✅ Embedding 客户端初始化完成")
            logger.info(f"   - 模型: {self.embedding_model}")
            logger.info(f"   - 维度: {EMBEDDING_DIMENSIONS}")
            logger.info(f"   - API地址: {base_url}")
            logger.info(f"   - 密钥来源: {config_source}")

        return self._embedding_client

    @property
    def qdrant_service(self) -> QdrantService:
        if self._qdrant_service is None:
            self._qdrant_service = QdrantService()
        return self._qdrant_service

    async def _get_embedding(self, text: str) -> List[float]:
        """
        使用独立的 Embedding API 生成向量

        Args:
            text: 输入文本

        Returns:
            向量列表（维度由 EMBEDDING_DIMENSIONS 自动识别）
        """
        try:
            # 构建请求参数
            params = {
                "model": self.embedding_model,
                "input": text,
            }
            # 仅当 OpenAI text-embedding-3 系列模型时才传 dimensions 参数
            # 其他模型（如 ada-002、国产模型）不支持此参数
            if "text-embedding-3" in self.embedding_model:
                params["dimensions"] = EMBEDDING_DIMENSIONS

            response = await self.embedding_client.embeddings.create(**params)
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"❌ Embedding API 调用失败: {e}")
            raise

    async def process_and_store_chapter(
        self,
        chapter_id: str,
        content: str,
        chapter_number: int,
        project_id: str,
        characters_mentioned: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        处理章节内容并存储记忆切片

        Args:
            chapter_id: 章节ID
            content: 章节内容
            chapter_number: 章节序号
            project_id: 项目ID
            characters_mentioned: 章节中提到的角色列表

        Returns:
            处理结果，包含存储数量和状态变更
        """
        result = {
            "stored_count": 0,
            "state_change_log": None,
            "error": None
        }

        if not content or len(content.strip()) < self.MIN_CHUNK_SIZE:
            logger.warning(f"⚠️ 章节内容过短，跳过记忆存储: chapter_id={chapter_id}")
            return result

        try:
            content_hash = self._build_content_hash(content)
            existing_state_change_log = await self._get_existing_state_change_log(chapter_id)

            # 1. 删除该章节的旧切片
            await self._delete_chapter_memories(chapter_id)

            # 2. 将内容切分为片段
            chunks = self._split_into_chunks(content)

            if not chunks:
                logger.warning(f"⚠️ 切片结果为空: chapter_id={chapter_id}")
                return result

            # 3. 生成 embedding 并存储
            stored_count = await self._store_chunks(
                chunks=chunks,
                chapter_id=chapter_id,
                chapter_number=chapter_number,
                project_id=project_id,
                characters_mentioned=characters_mentioned
            )
            result["stored_count"] = stored_count

            # 4. 提取状态变更（如果有 AI 服务）
            if self.ai_service:
                if self._state_change_matches_content(existing_state_change_log, content_hash):
                    state_change = existing_state_change_log
                    logger.info(
                        f"⏭️ 状态提取跳过：章节内容未变化 chapter_id={chapter_id[:8]}"
                    )
                else:
                    state_change = await self._extract_state_change(
                        content=content,
                        project_id=project_id,
                        chapter_number=chapter_number
                    )
                if state_change:
                    result["state_change_log"] = state_change
                    # 保存状态变更到章节表（摘要和钩子立即同步，但状态变化暂存待确认）
                    await self._save_chapter_state_change(
                        chapter_id,
                        state_change,
                        content_hash=content_hash
                    )
                    # 从所有章节重建世界状态（避免重写旧章节时状态回退）
                    world_state_updated = await self._rebuild_world_state_from_chapters(project_id)
                    if not world_state_updated:
                        logger.warning(f"⚠️ 重建世界状态失败: project_id={project_id[:8]}")

                    # 提取并保存关键事件
                    await self._extract_and_save_key_events(
                        project_id=project_id,
                        chapter_number=chapter_number,
                        content=content,
                        state_change=state_change
                    )

            logger.info(
                f"✅ 章节记忆存储完成 - chapter_id:{chapter_id[:8]}, "
                f"章节:{chapter_number}, 切片数:{stored_count}"
            )

        except Exception as e:
            logger.error(f"❌ 章节记忆存储失败: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def _save_chapter_state_change(
        self,
        chapter_id: str,
        state_change: Dict[str, Any],
        content_hash: Optional[str] = None
    ) -> bool:
        """
        将状态变更保存到章节表的 state_change_log 字段，
        同时同步 summary 和 end_hook 字段。
        状态变化直接生效，不再需要用户确认。

        Args:
            chapter_id: 章节ID
            state_change: 状态变更数据

        Returns:
            是否保存成功
        """
        try:
            result = await self.db.execute(
                select(Chapter).where(Chapter.id == chapter_id)
            )
            chapter = result.scalar_one_or_none()

            if not chapter:
                logger.warning(f"⚠️ 章节不存在: {chapter_id}")
                return False

            if not isinstance(state_change, dict):
                logger.warning(f"⚠️ 状态变更数据类型错误: {type(state_change).__name__}")
                return False

            normalized_state_change = normalize_state_change_payload(state_change)
            if not normalized_state_change:
                logger.info(f"ℹ️ 状态变更规范化后为空，跳过保存: chapter_id={chapter_id[:8]}")
                return False

            normalized_state_change = self._attach_state_meta(normalized_state_change, content_hash)

            old_payload = chapter.state_change_log if isinstance(chapter.state_change_log, dict) else None
            chapter.state_change_log = normalized_state_change

            # 同步摘要到 chapter.summary
            summary = normalized_state_change.get("summary")
            if isinstance(summary, str) and summary:
                chapter.summary = summary
                logger.info(f"📝 摘要已同步: {summary[:50]}...")
            elif summary is not None and not isinstance(summary, str):
                logger.warning(f"⚠️ summary 类型错误: {type(summary).__name__}")

            # 同步钩子到 chapter.end_hook
            end_hook = normalized_state_change.get("end_hook")
            if isinstance(end_hook, dict) and end_hook:
                chapter.end_hook = end_hook
                hook_type = end_hook.get("type", "未知")
                must_respond = end_hook.get("must_respond_next", False)
                logger.info(f"🎣 钩子已同步: 类型={hook_type}, 需响应={must_respond}")
            elif end_hook is not None and not isinstance(end_hook, dict):
                logger.warning(f"⚠️ end_hook 类型错误: {type(end_hook).__name__}")

            # 清除待确认状态（不再使用）
            chapter.pending_state_change = None

            await self.db.commit()

            if old_payload and self._serialize_state_payload(old_payload) == self._serialize_state_payload(normalized_state_change):
                logger.info(f"ℹ️ 状态变更内容未变化，已保持幂等: chapter_id={chapter_id[:8]}")
            else:
                logger.info(f"✅ 状态变更已保存并自动生效: chapter_id={chapter_id[:8]}")
            return True

        except Exception as e:
            logger.error(f"❌ 保存章节状态变更失败: {e}")
            await self.db.rollback()
            return False

    async def _delete_chapter_memories(self, chapter_id: str) -> int:
        """删除章节的所有记忆切片"""
        try:
            result = await self.db.execute(
                delete(ChapterMemory).where(ChapterMemory.chapter_id == chapter_id)
            )
            await self.db.commit()
            deleted = result.rowcount
            if deleted > 0:
                logger.info(f"🗑️ 已删除章节旧切片: chapter_id={chapter_id[:8]}, 数量={deleted}")
            if settings.vector_db_provider == "qdrant":
                self.qdrant_service.delete_by_payload(
                    collection="chapter_memories",
                    key="chapter_id",
                    value=chapter_id
                )
            return deleted
        except Exception as e:
            logger.error(f"❌ 删除旧切片失败: {e}")
            await self.db.rollback()
            return 0

    def _split_into_chunks(self, content: str) -> List[Dict[str, Any]]:
        """
        将内容切分为片段

        策略：
        1. 优先在句号处断句
        2. 保留 50 字重叠以保持上下文
        3. 目标切片大小 400 字
        """
        content = content.strip()
        if not content:
            return []

        chunks = []
        sentences = re.split(r'([。！？])', content)

        combined_sentences = []
        for i in range(0, len(sentences) - 1, 2):
            sentence = sentences[i]
            punctuation = sentences[i + 1] if i + 1 < len(sentences) else ""
            combined_sentences.append(sentence + punctuation)

        if len(sentences) % 2 == 1 and sentences[-1].strip():
            combined_sentences.append(sentences[-1])

        current_chunk = ""
        chunk_index = 0

        for sentence in combined_sentences:
            if len(current_chunk) + len(sentence) > self.CHUNK_SIZE:
                if current_chunk:
                    chunks.append({
                        "content": current_chunk.strip(),
                        "chunk_index": chunk_index,
                        "memory_type": self._detect_memory_type(current_chunk)
                    })
                    chunk_index += 1

                    if len(current_chunk) > self.CHUNK_OVERLAP:
                        current_chunk = current_chunk[-self.CHUNK_OVERLAP:] + sentence
                    else:
                        current_chunk = sentence
                else:
                    current_chunk = sentence
            else:
                current_chunk += sentence

        if current_chunk.strip() and len(current_chunk.strip()) >= self.MIN_CHUNK_SIZE:
            chunks.append({
                "content": current_chunk.strip(),
                "chunk_index": chunk_index,
                "memory_type": self._detect_memory_type(current_chunk)
            })

        return chunks

    def _detect_memory_type(self, content: str) -> str:
        """检测内容类型"""
        dialogue_markers = ['"', '"', '"', '「', '」', '『', '』', '说道', '问道', '答道', '喊道']
        dialogue_count = sum(1 for marker in dialogue_markers if marker in content)

        description_markers = ['只见', '但见', '环顾', '放眼', '映入眼帘', '景色', '风景']
        description_count = sum(1 for marker in description_markers if marker in content)

        if dialogue_count >= 3:
            return "dialogue"
        elif description_count >= 2:
            return "description"
        else:
            return "narrative"

    async def _store_chunks(
        self,
        chunks: List[Dict[str, Any]],
        chapter_id: str,
        chapter_number: int,
        project_id: str,
        characters_mentioned: Optional[List[str]] = None
    ) -> int:
        """生成 embedding 并批量存储切片"""
        stored = 0
        qdrant_points: List[qdrant_models.PointStruct] = []
        use_qdrant = settings.vector_db_provider == "qdrant"

        for chunk in chunks:
            try:
                # 使用独立 Embedding API 生成向量
                embedding = await self._get_embedding(chunk["content"])

                memory = ChapterMemory(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    chapter_id=chapter_id,
                    chunk_index=chunk["chunk_index"],
                    content=chunk["content"],
                    memory_type=chunk["memory_type"],
                    characters_mentioned=characters_mentioned,
                    importance_score=0.5,
                    story_timeline=chapter_number
                )

                self.db.add(memory)
                await self.db.flush()

                if use_qdrant:
                    qdrant_points.append(
                        qdrant_models.PointStruct(
                            id=memory.id,
                            vector=embedding,
                            payload={
                                "project_id": project_id,
                                "chapter_id": chapter_id,
                                "chunk_index": chunk["chunk_index"],
                                "memory_type": chunk["memory_type"],
                                "characters": characters_mentioned or [],
                                "importance": 0.5,
                                "story_timeline": chapter_number,
                                "content": chunk["content"],
                            }
                        )
                    )
                else:
                    # 使用原生 SQL 更新 embedding (pgvector)
                    embedding_literal = "[" + ",".join(str(v) for v in embedding) + "]"
                    async with self.db.begin_nested():
                        await self.db.execute(
                            text("""
                                UPDATE chapter_memories
                                SET embedding = CAST(:embedding AS vector)
                                WHERE id = :id
                            """),
                            {"id": memory.id, "embedding": embedding_literal}
                        )

                stored += 1

            except Exception as e:
                logger.error(f"❌ 存储切片失败: chunk_index={chunk['chunk_index']}, error={e}")
                continue

        await self.db.commit()

        if use_qdrant:
            try:
                await self._flush_qdrant_vectors(qdrant_points)
            except Exception as e:
                logger.error(f"❌ Qdrant 写入失败: {e}")

        if use_qdrant:
            logger.info("✅ 使用 Qdrant 完成向量存储")
        else:
            logger.info("✅ 使用 pgvector 完成向量存储")

        return stored

    async def _flush_qdrant_vectors(
        self,
        points: List[qdrant_models.PointStruct]
    ) -> None:
        if not points:
            return
        self.qdrant_service.ensure_collection("chapter_memories", EMBEDDING_DIMENSIONS)
        self.qdrant_service.upsert_vectors("chapter_memories", points)

    async def _extract_state_change(
        self,
        content: str,
        project_id: str,
        chapter_number: int
    ) -> Optional[Dict[str, Any]]:
        """
        使用 LLM 提取章节中的状态变更

        Returns:
            状态变更 Diff，格式如：
            {
                "location_change": {"from": "起始镇", "to": "黑暗森林"},
                "items_gained": ["屠龙刀"],
                "items_lost": [],
                "status_changes": {"hp": -20, "level": 1},
                "relationships": {"李四": "结为好友"},
                "summary": "主角离开起始镇，进入黑暗森林，获得屠龙刀"
            }
        """
        if not self.ai_service:
            return None

        try:
            prompt = f"""分析以下小说章节内容，提取所有**实质性状态变化**和**章节结尾钩子**。

【章节内容】
{content[:4000]}

请严格按以下 JSON 格式输出（只输出 JSON，不要其他内容）：
{{
    "location_change": {{"from": "原位置或null", "to": "新位置或null"}},
    "items_gained": ["获得的物品列表"],
    "items_lost": ["失去的物品列表"],
    "status_changes": {{"属性名": 变化值}},
    "relationships": {{"角色名": "关系变化描述"}},
    "time_passed": "时间流逝描述（如：一天后、三个月后）",
    "summary": "本章核心内容总结(50-100字)",
    "end_hook": {{
        "type": "悬念/冲突/情感/承诺/揭示",
        "content": "钩子内容描述(30-80字)",
        "must_respond_next": true或false
    }}
}}

注意：
- 只记录**明确发生**的变化，不要推测
- 如果某项没有变化，使用空值或空数组
- status_changes 中的数值变化用正负数表示（如 hp: -20, 经验值: +100）
- **境界类属性**（修为、境界、职位、身份等）请使用**当前绝对值**而非增量（如 "修为": "筑基期" 而非 "修为": 1）
- end_hook 描述章节结尾留下的期待/悬念
- must_respond_next=true 表示下章开头必须回应
- 如果章节结尾没有明显钩子，end_hook 可以为 null"""

            response = await self.ai_service.generate_text(
                prompt=prompt,
                system_prompt="你是一个精确的小说状态追踪器，只输出 JSON 格式的状态变更记录。",
                max_tokens=4000
            )

            # 解析 JSON - response 是字典，实际内容在 content 字段
            response_text = response.get("content", "") if isinstance(response, dict) else str(response)

            # 检查响应是否为空
            if not response_text or not response_text.strip():
                logger.warning("⚠️ AI 返回的状态变更内容为空")
                return None

            cleaned_json = self.ai_service._clean_json_response(response_text)

            # 再次检查清洗后的内容
            if not cleaned_json or not cleaned_json.strip():
                logger.warning("⚠️ 清洗后的 JSON 内容为空")
                return None

            state_change = json.loads(cleaned_json)
            suppressed_keys = await self._get_project_suppressed_state_keys(project_id)
            state_change = normalize_state_change_payload(state_change, suppressed_keys=suppressed_keys)
            if not state_change:
                logger.info(f"ℹ️ 状态变更规范化后为空: 第{chapter_number}章")
                return None
            logger.info(f"✅ 状态变更提取完成: {state_change.get('summary', '')[:50]}")
            return state_change

        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ 状态变更 JSON 解析失败: {e}")
            logger.debug(f"   原始响应: {response_text[:200] if response_text else 'None'}")
            return None
        except Exception as e:
            logger.error(f"❌ 状态变更提取失败: {e}")
            return None

    async def _patch_world_state(
        self,
        project_id: str,
        state_change: Dict[str, Any]
    ) -> bool:
        """
        将状态变更合并到项目的全局状态

        Args:
            project_id: 项目ID
            state_change: 状态变更 Diff

        Returns:
            是否更新成功
        """
        try:
            # 获取项目
            result = await self.db.execute(
                select(Project).where(Project.id == project_id)
            )
            project = result.scalar_one_or_none()

            if not project:
                logger.warning(f"⚠️ 项目不存在: {project_id}")
                return False

            # 深拷贝当前状态，避免 SQLAlchemy JSON 列就地修改不触发 dirty 检测
            import copy
            current_state = normalize_world_state_structure(project.world_state)
            suppressed_keys = get_suppressed_state_keys(current_state)
            normalized_state_change = normalize_state_change_payload(
                state_change,
                suppressed_keys=suppressed_keys,
            )
            if not normalized_state_change:
                logger.info(f"ℹ️ 状态变更被过滤，跳过全局状态更新: project_id={project_id[:8]}")
                return True

            # 合并位置变更
            if normalized_state_change.get("location_change", {}).get("to"):
                current_state["current_location"] = normalized_state_change["location_change"]["to"]

            # 合并物品变更
            inventory = current_state.get("inventory", [])
            for item in normalized_state_change.get("items_gained", []):
                if item not in inventory:
                    inventory.append(item)
            for item in normalized_state_change.get("items_lost", []):
                if item in inventory:
                    inventory.remove(item)
            current_state["inventory"] = inventory

            # 合并状态变更
            status_changes = normalized_state_change.get("status_changes", {})
            if status_changes:
                existing_status_changes = current_state.get("status_changes", {})
                if not isinstance(existing_status_changes, dict):
                    existing_status_changes = {}

                for key, value in status_changes.items():
                    if isinstance(value, (int, float)) and not is_absolute_state_key(key):
                        existing_status_changes[key] = existing_status_changes.get(key, 0) + value
                    else:
                        existing_status_changes[key] = value

                current_state["status_changes"] = existing_status_changes

            # 合并关系变更
            relationships = current_state.get("relationships", {})
            relationships.update(normalized_state_change.get("relationships", {}))
            current_state["relationships"] = relationships

            # 更新时间
            if normalized_state_change.get("time_passed"):
                current_state["last_time_reference"] = normalized_state_change["time_passed"]

            # 赋值新对象，确保 SQLAlchemy 检测到变化
            project.world_state = normalize_world_state_structure(current_state)
            await self.db.commit()

            logger.info(f"✅ 全局状态已更新: project_id={project_id[:8]}")
            return True

        except Exception as e:
            logger.error(f"❌ 全局状态更新失败: {e}")
            await self.db.rollback()
            return False

    async def _rebuild_world_state_from_chapters(
        self,
        project_id: str
    ) -> bool:
        """
        从所有章节的 state_change_log 按章节序号重建全局世界状态。

        与 _patch_world_state 的增量合并不同，此方法从空 dict 开始，
        按 chapter_number 升序 replay 每章的 state_change_log，
        确保重写/修改旧章节后世界状态不会回退。

        **重要**：重建时会保留用户手动添加的自定义字段（非标准字段）。

        Args:
            project_id: 项目ID

        Returns:
            是否重建成功
        """
        try:
            # 获取项目
            result = await self.db.execute(
                select(Project).where(Project.id == project_id)
            )
            project = result.scalar_one_or_none()
            if not project:
                logger.warning(f"⚠️ 项目不存在: {project_id}")
                return False

            old_state = normalize_world_state_structure(project.world_state)
            suppressed_keys = get_suppressed_state_keys(project.world_state)

            # 收集所有章节中出现过的 status_changes 键（这些会被重建）
            chapter_status_keys = set()
            result_temp = await self.db.execute(
                select(Chapter)
                .where(
                    Chapter.project_id == project_id,
                    Chapter.state_change_log.isnot(None)
                )
            )
            for ch in result_temp.scalars().all():
                if isinstance(ch.state_change_log, dict):
                    status_changes = normalize_status_changes(
                        ch.state_change_log.get("status_changes", {}),
                        suppressed_keys=suppressed_keys,
                    )
                    if isinstance(status_changes, dict):
                        chapter_status_keys.update(status_changes.keys())

            # 保存用户自定义字段：
            # 1. 顶层非标准字段
            # 2. 不在章节 status_changes 中出现过的字段（用户手动添加的状态）
            custom_fields = {}
            for k, v in old_state.items():
                if k == "_state_hidden_keys":
                    continue
                if k not in {"current_location", "inventory", "relationships", "status_changes", "last_time_reference"} and k not in chapter_status_keys:
                    custom_fields[k] = v

            # 保存 status_changes 中的自定义字段
            old_status_changes = old_state.get("status_changes", {})
            custom_status_fields = {}
            if isinstance(old_status_changes, dict):
                custom_status_fields = {
                    k: v for k, v in old_status_changes.items()
                    if k not in chapter_status_keys and k not in suppressed_keys
                }

            # 查询所有有 state_change_log 的章节，按 chapter_number 升序
            result = await self.db.execute(
                select(Chapter)
                .where(
                    Chapter.project_id == project_id,
                    Chapter.state_change_log.isnot(None)
                )
                .order_by(Chapter.chapter_number.asc())
            )
            chapters = result.scalars().all()

            # 从空状态开始 replay
            current_state: Dict[str, Any] = {}

            for chapter in chapters:
                state_change = normalize_state_change_payload(
                    chapter.state_change_log,
                    suppressed_keys=suppressed_keys,
                )
                if not isinstance(state_change, dict):
                    continue

                # 合并位置变更
                if state_change.get("location_change", {}).get("to"):
                    current_state["current_location"] = state_change["location_change"]["to"]

                # 合并物品变更
                inventory = current_state.get("inventory", [])
                for item in state_change.get("items_gained", []):
                    if item not in inventory:
                        inventory.append(item)
                for item in state_change.get("items_lost", []):
                    if item in inventory:
                        inventory.remove(item)
                current_state["inventory"] = inventory

                status_changes_dict = {}
                for key, value in normalize_status_changes(
                    state_change.get("status_changes", {}),
                    suppressed_keys=suppressed_keys,
                ).items():
                    if isinstance(value, (int, float)) and not is_absolute_state_key(key):
                        # 数值型且非绝对状态：累加（如 hp、经验值等）
                        previous_status = current_state.get("status_changes", {}).get(key, 0)
                        if not isinstance(previous_status, (int, float)):
                            previous_status = 0
                        status_changes_dict[key] = previous_status + value
                    else:
                        # 字符串型或绝对状态：直接替换（如修为境界、职位等）
                        status_changes_dict[key] = value

                # 更新 status_changes 字段（合并而非替换）
                if status_changes_dict:
                    if "status_changes" not in current_state:
                        current_state["status_changes"] = {}
                    current_state["status_changes"].update(status_changes_dict)

                # 合并关系变更
                relationships = current_state.get("relationships", {})
                relationships.update(state_change.get("relationships", {}))
                current_state["relationships"] = relationships

                # 更新时间
                if state_change.get("time_passed"):
                    current_state["last_time_reference"] = state_change["time_passed"]

            # 合并用户自定义字段（保留用户手动编辑的内容）
            # 1. 顶层自定义字段
            current_state.update(custom_fields)

            # 2. status_changes 中的自定义字段
            if custom_status_fields:
                if "status_changes" not in current_state:
                    current_state["status_changes"] = {}
                elif not isinstance(current_state["status_changes"], dict):
                    current_state["status_changes"] = {}
                current_state["status_changes"].update(custom_status_fields)

            # 赋值新对象，确保 SQLAlchemy 检测到变化
            current_state = set_suppressed_state_keys(current_state, suppressed_keys)
            project.world_state = normalize_world_state_structure(current_state)
            await self.db.commit()

            custom_count = len(custom_fields) + len(custom_status_fields)
            logger.info(
                f"✅ 全局状态已重建: project_id={project_id[:8]}, "
                f"replay {len(chapters)} 章, 保留 {custom_count} 个自定义字段"
            )
            return True

        except Exception as e:
            logger.error(f"❌ 全局状态重建失败: {e}")
            await self.db.rollback()
            return False

    async def get_chapter_chunks(
        self,
        chapter_id: str,
        memory_types: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """获取章节的所有记忆切片"""
        try:
            query = select(ChapterMemory).where(ChapterMemory.chapter_id == chapter_id)

            if memory_types:
                query = query.where(ChapterMemory.memory_type.in_(memory_types))

            query = query.order_by(ChapterMemory.chunk_index)

            result = await self.db.execute(query)
            memories = result.scalars().all()

            return [
                {
                    "id": m.id,
                    "content": m.content,
                    "chunk_index": m.chunk_index,
                    "memory_type": m.memory_type,
                    "importance_score": m.importance_score
                }
                for m in memories
            ]

        except Exception as e:
            logger.error(f"❌ 获取章节切片失败: {e}")
            return []

    async def _extract_and_save_key_events(
        self,
        project_id: str,
        chapter_number: int,
        content: str,
        state_change: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        从章节内容和状态变化中提取关键事件并保存

        Args:
            project_id: 项目ID
            chapter_number: 章节号
            content: 章节内容
            state_change: 状态变化

        Returns:
            提取的关键事件列表
        """
        from app.models.key_event import KeyEvent

        try:
            events = []

            # 1. 从状态变化中自动提取关键事件
            # 物品获得
            for item in state_change.get("items_gained", []):
                events.append({
                    "event_type": "item_acquire",
                    "title": f"获得{item}",
                    "description": f"在第{chapter_number}章获得了{item}",
                    "related_entities": {"items": [item]},
                    "importance": 3
                })

            # 物品失去
            for item in state_change.get("items_lost", []):
                events.append({
                    "event_type": "item_lose",
                    "title": f"失去{item}",
                    "description": f"在第{chapter_number}章失去了{item}",
                    "related_entities": {"items": [item]},
                    "importance": 3
                })

            # 位置变化
            loc_change = state_change.get("location_change", {})
            if loc_change.get("to"):
                events.append({
                    "event_type": "location_change",
                    "title": f"前往{loc_change['to']}",
                    "description": f"从{loc_change.get('from', '?')}前往{loc_change['to']}",
                    "related_entities": {"locations": [loc_change['to']]},
                    "importance": 2
                })

            # 关系变化
            for char, change in state_change.get("relationships", {}).items():
                events.append({
                    "event_type": "relationship_change",
                    "title": f"与{char}关系变化",
                    "description": change,
                    "related_entities": {"characters": [char]},
                    "importance": 3
                })

            # 2. 使用 AI 提取更高级的关键事件（重大转折、伏笔等）
            if self.ai_service and len(content) > 500:
                ai_events = await self._ai_extract_key_events(content, chapter_number)
                events.extend(ai_events)

            # 3. 保存到数据库
            saved_count = 0
            for event_data in events:
                event = KeyEvent(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_type=event_data["event_type"],
                    title=event_data["title"],
                    description=event_data.get("description"),
                    related_entities=event_data.get("related_entities"),
                    importance=event_data.get("importance", 3)
                )
                self.db.add(event)
                saved_count += 1

            if saved_count > 0:
                await self.db.commit()
                logger.info(f"📌 关键事件已保存: {saved_count} 个 (chapter={chapter_number})")

            return events

        except Exception as e:
            logger.error(f"❌ 关键事件提取失败: {e}")
            await self.db.rollback()
            return []

    async def _ai_extract_key_events(
        self,
        content: str,
        chapter_number: int
    ) -> List[Dict[str, Any]]:
        """
        使用 AI 提取高级关键事件（重大转折、伏笔等）

        Args:
            content: 章节内容
            chapter_number: 章节号

        Returns:
            关键事件列表
        """
        try:
            prompt = f"""分析以下章节内容，提取**重要的关键事件**（只提取真正重要的，不要琐碎的）。

【章节内容】
{content[:3000]}

请识别以下类型的事件（如果存在）：
- major_turn: 重大剧情转折
- foreshadow_plant: 埋下的伏笔（暗示未来发展）
- character_intro: 重要角色首次登场
- secret_reveal: 重要秘密揭示
- power_up: 主角实力重大提升

请按 JSON 数组格式输出，每个事件包含：
- event_type: 事件类型
- title: 事件标题（10字以内）
- description: 事件描述（30字以内）
- importance: 重要程度（1-5，5最重要）

只输出 JSON 数组，不要其他内容。如果没有重要事件，输出空数组 []
示例：[{{"event_type": "major_turn", "title": "身世揭秘", "description": "主角发现自己是皇室血脉", "importance": 5}}]"""

            response = await self.ai_service.generate_text(
                prompt=prompt,
                system_prompt="你是一个小说关键事件提取器，只输出JSON数组。",
                max_tokens=2000
            )

            response_text = response.get("content", "") if isinstance(response, dict) else str(response)

            # 提取 JSON
            if "[" in response_text and "]" in response_text:
                start = response_text.index("[")
                end = response_text.rindex("]") + 1
                events = json.loads(response_text[start:end])
                if isinstance(events, list):
                    # 只保留重要程度 >= 4 的事件
                    important_events = [e for e in events if e.get("importance", 0) >= 4]
                    logger.info(f"🔍 AI 提取关键事件: {len(important_events)} 个")
                    return important_events

            return []

        except Exception as e:
            logger.warning(f"⚠️ AI 关键事件提取失败: {e}")
            return []

    @staticmethod
    def _is_subset_of_pending(
        confirmed_changes: Dict[str, Any],
        pending_changes: Dict[str, Any]
    ) -> bool:
        for key, confirmed_value in confirmed_changes.items():
            if key not in pending_changes:
                return False

            pending_value = pending_changes[key]

            if isinstance(confirmed_value, list):
                if not isinstance(pending_value, list):
                    return False
                if any(item not in pending_value for item in confirmed_value):
                    return False
            elif isinstance(confirmed_value, dict):
                if not isinstance(pending_value, dict):
                    return False
                for sub_key, sub_value in confirmed_value.items():
                    if sub_key not in pending_value or pending_value[sub_key] != sub_value:
                        return False
            else:
                if confirmed_value != pending_value:
                    return False

        return True

    async def confirm_state_change(
        self,
        chapter_id: str,
        confirmed_changes: Dict[str, Any]
    ) -> bool:
        """
        确认章节的状态变化，将确认的变化写入全局状态

        Args:
            chapter_id: 章节ID
            confirmed_changes: 用户确认的状态变化
                {
                    "items_gained": ["玄铁剑"],  # 确认获得的物品
                    "items_lost": [],  # 确认失去的物品（空表示取消）
                    "location_change": {"from": "京城", "to": "边关"},
                    ...
                }

        Returns:
            是否成功
        """
        try:
            # 获取章节
            result = await self.db.execute(
                select(Chapter).where(Chapter.id == chapter_id)
            )
            chapter = result.scalar_one_or_none()

            if not chapter:
                logger.warning(f"⚠️ 章节不存在: {chapter_id}")
                return False

            pending_changes = chapter.pending_state_change
            if not pending_changes:
                logger.warning("⚠️ 当前无待确认状态")
                return False
            if not isinstance(pending_changes, dict):
                logger.warning(f"⚠️ pending_state_change 类型错误: {type(pending_changes).__name__}")
                return False

            if not isinstance(confirmed_changes, dict):
                logger.warning(f"⚠️ confirmed_changes 类型错误: {type(confirmed_changes).__name__}")
                return False
            if not confirmed_changes:
                logger.warning("⚠️ confirmed_changes 为空")
                return False

            if not self._is_subset_of_pending(confirmed_changes, pending_changes):
                logger.warning("⚠️ confirmed_changes 不是 pending_state_change 子集")
                return False

            # 将用户确认的变更回写到 state_change_log，
            # 确保后续 rebuild 只 replay 用户实际确认的状态
            if confirmed_changes:
                import copy
                log = copy.deepcopy(chapter.state_change_log) if isinstance(chapter.state_change_log, dict) else {}

                # 用 confirmed_changes 覆盖对应字段，未确认的字段清除
                state_fields = ["location_change", "items_gained", "items_lost",
                                "status_changes", "relationships", "time_passed"]
                for field in state_fields:
                    if field in confirmed_changes:
                        log[field] = confirmed_changes[field]
                    else:
                        log.pop(field, None)

                chapter.state_change_log = log

                patched = await self._rebuild_world_state_from_chapters(chapter.project_id)
                if not patched:
                    logger.warning("⚠️ 全局状态重建失败，保留待确认状态")
                    return False

            # 清除待确认状态
            chapter.pending_state_change = None
            await self.db.commit()

            logger.info(f"✅ 状态变化已确认: chapter_id={chapter_id[:8]}")
            return True

        except Exception as e:
            logger.error(f"❌ 确认状态变化失败: {e}")
            await self.db.rollback()
            return False

    async def reject_state_change(self, chapter_id: str) -> bool:
        """
        拒绝章节的状态变化，清除待确认状态

        Args:
            chapter_id: 章节ID

        Returns:
            是否成功
        """
        try:
            result = await self.db.execute(
                select(Chapter).where(Chapter.id == chapter_id)
            )
            chapter = result.scalar_one_or_none()

            if not chapter:
                return False

            pending_changes = chapter.pending_state_change
            if not pending_changes:
                logger.warning("⚠️ 当前无待确认状态")
                return False
            if not isinstance(pending_changes, dict):
                logger.warning(f"⚠️ pending_state_change 类型错误: {type(pending_changes).__name__}")
                return False

            # 拒绝后清除 state_change_log 中的状态字段，保留 summary/end_hook
            import copy
            log = copy.deepcopy(chapter.state_change_log) if isinstance(chapter.state_change_log, dict) else {}
            for field in ["location_change", "items_gained", "items_lost",
                          "status_changes", "relationships", "time_passed"]:
                log.pop(field, None)
            chapter.state_change_log = log

            chapter.pending_state_change = None

            # 重建世界状态（排除被拒绝章节的状态影响）
            await self._rebuild_world_state_from_chapters(chapter.project_id)

            await self.db.commit()

            logger.info(f"🚫 状态变化已拒绝: chapter_id={chapter_id[:8]}")
            return True

        except Exception as e:
            logger.error(f"❌ 拒绝状态变化失败: {e}")
            await self.db.rollback()
            return False


async def create_chapter_memory_service(
    db: AsyncSession,
    ai_service=None,
    user_embedding_config: Optional[Dict[str, str]] = None
) -> ChapterMemoryService:
    """创建 ChapterMemoryService 实例

    Args:
        db: 数据库会话
        ai_service: AI 服务实例
        user_embedding_config: 用户级别的 Embedding 配置
    """
    return ChapterMemoryService(
        db=db,
        ai_service=ai_service,
        user_embedding_config=user_embedding_config
    )

