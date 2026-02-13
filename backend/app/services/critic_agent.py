"""五维审查服务 - CriticAgent

提供四个维度的章节审查功能：
1. OOC Check - 角色一致性检查
2. Pacing - 节奏分析
3. High Point - 爽点密度分析
4. Consistency - 设定一致性检查
"""
import asyncio
import json
import re
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.models.memory import StoryMemory
from app.services.ai_service import AIService
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DimensionResult:
    """单个维度的审查结果"""
    dimension: str
    score: float  # 0-100
    analysis: str
    suggestions: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewResult:
    """完整审查结果"""
    chapter_id: str
    overall_score: float
    dimensions: Dict[str, DimensionResult] = field(default_factory=dict)
    reviewed_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class CriticAgent:
    """五维审查 Agent - 提供章节质量评估"""

    # 三个审查维度的 Prompt 模板
    DIMENSION_PROMPTS = {
        "ooc": """你是一位专业的小说编辑，专注于角色一致性分析。

请分析以下章节内容，检查角色行为是否与其设定一致（OOC检测）。

【角色设定】
{character_settings}

【章节内容】
{chapter_content}

请按以下格式输出JSON：
{{
    "score": 0-100的整数,
    "analysis": "整体分析（100-200字）",
    "inconsistencies": [
        {{"character": "角色名", "issue": "不一致描述", "severity": "high/medium/low"}}
    ],
    "suggestions": ["改进建议1", "改进建议2"]
}}

评分标准：
- 90-100: 角色行为完全符合设定
- 70-89: 有轻微不一致但可接受
- 50-69: 存在明显OOC问题
- 0-49: 严重OOC，角色行为与设定矛盾""",

        "pacing": """你是一位专业的小说编辑，专注于叙事节奏分析。

请分析以下章节的叙事节奏，评估动作/对话/描写的比例和张力曲线。

【章节内容】
{chapter_content}

【前文摘要】
{previous_summary}

请按以下格式输出JSON：
{{
    "score": 0-100的整数,
    "analysis": "节奏分析（100-200字）",
    "composition": {{
        "action_percent": 动作场景占比,
        "dialogue_percent": 对话占比,
        "description_percent": 描写占比
    }},
    "tension_curve": "rising/falling/flat/波动",
    "suggestions": ["改进建议1", "改进建议2"]
}}

评分标准：
- 90-100: 节奏张弛有度，引人入胜
- 70-89: 节奏基本流畅
- 50-69: 节奏有些拖沓或过于紧凑
- 0-49: 节奏混乱，影响阅读体验""",

        "high_point": """你是一位专业的网文编辑，专注于"爽点"分析。

请分析以下章节的爽点密度，识别期待感和释放感的设置。

【章节内容】
{chapter_content}

【章节大纲】
{chapter_outline}

请按以下格式输出JSON：
{{
    "score": 0-100的整数,
    "analysis": "爽点分析（100-200字）",
    "hooks": [
        {{"type": "悬念/反转/成就/情感", "description": "描述", "position": "开头/中间/结尾"}}
    ],
    "expectation_buildup": "期待感构建评价",
    "payoff_delivery": "释放感交付评价",
    "suggestions": ["改进建议1", "改进建议2"]
}}

评分标准：
- 90-100: 爽点密集，节奏感强，让人欲罢不能
- 70-89: 有明确爽点，阅读体验良好
- 50-69: 爽点不足，平淡无奇
- 0-49: 缺乏吸引力，难以持续阅读""",

        "three_line_rhythm": """你是一位专业的小说结构分析师，专注于"三线节奏"量化分析。

三线节奏理论：优秀的小说章节应该在三条叙事线上保持平衡：
1. 情节线 (Plot Line): 推动故事发展的事件、冲突、转折
2. 人物线 (Character Line): 角色的情感变化、内心活动、关系发展
3. 世界线 (World Line): 环境描写、背景设定、氛围营造

请分析以下章节内容，量化三线密度并评估平衡性。

【章节内容】
{chapter_content}

【前文摘要】
{previous_summary}

请按以下格式输出JSON：
{{
    "score": 0-100的整数,
    "analysis": "三线节奏整体分析（150-250字）",
    "line_density": {{
        "plot": {{
            "percentage": 情节线占比(0-100),
            "key_events": ["关键事件1", "关键事件2"],
            "momentum": "上升/下降/平稳"
        }},
        "character": {{
            "percentage": 人物线占比(0-100),
            "emotional_beats": ["情感节点1", "情感节点2"],
            "depth": "深入/适中/浅显"
        }},
        "world": {{
            "percentage": 世界线占比(0-100),
            "elements": ["世界元素1", "世界元素2"],
            "immersion": "沉浸/适度/稀薄"
        }}
    }},
    "balance_assessment": {{
        "is_balanced": true/false,
        "dominant_line": "plot/character/world/balanced",
        "weak_line": "plot/character/world/none",
        "recommendation": "平衡性建议"
    }},
    "rhythm_pattern": "快节奏/中节奏/慢节奏/变奏",
    "suggestions": ["改进建议1", "改进建议2", "改进建议3"]
}}

评分标准：
- 90-100: 三线完美平衡，节奏流畅，层次丰富
- 70-89: 三线基本平衡，有一定层次感
- 50-69: 三线失衡明显，某一线过于薄弱或过于突出
- 0-49: 三线严重失衡，叙事单调或混乱

理想比例参考（可根据类型调整）：
- 动作/悬疑类: 情节50% + 人物30% + 世界20%
- 言情/成长类: 情节30% + 人物50% + 世界20%
- 奇幻/科幻类: 情节40% + 人物30% + 世界30%""",

        "consistency": """你是一位专业的小说编辑，专注于设定一致性检查。

请分析以下章节内容，检查是否与已建立的世界观/角色设定产生矛盾。

【世界观设定】
时间背景: {world_time_period}
地理位置: {world_location}
世界规则: {world_rules}

【当前全局状态】
当前位置: {current_location}
持有物品: {inventory}
人物关系: {relationships}

【角色设定】
{character_settings}

【最近章节摘要】
{previous_summaries}

【本章内容】
{chapter_content}

请按以下格式输出JSON：
{{
    "score": 0-100的整数,
    "analysis": "一致性分析（150-250字）",
    "inconsistencies": [
        {{
            "type": "location/timeline/character/item/relationship",
            "issue": "具体矛盾描述",
            "evidence": "章节中的相关原文",
            "expected": "根据设定应该是什么",
            "severity": "high/medium/low"
        }}
    ],
    "suggestions": ["修复建议1", "修复建议2"]
}}

评分标准：
- 90-100: 完全一致，无任何矛盾
- 70-89: 存在轻微不一致但不影响阅读
- 50-69: 存在明显矛盾，需要修正
- 0-49: 严重矛盾，破坏故事可信度"""
    }

    def __init__(self, db: AsyncSession, ai_service: AIService):
        """
        初始化 CriticAgent

        Args:
            db: 数据库会话
            ai_service: AI 服务实例
        """
        self.db = db
        self.ai_service = ai_service

    _NUMERIC_FIELDS = (
        "score",
        "action_percent",
        "dialogue_percent",
        "description_percent",
        "percentage",
    )

    @staticmethod
    def _extract_first_number(text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        match = re.search(r"-?\d+(?:[.,]\d+)?", text)
        if not match:
            return None
        raw = match.group(0)
        if "," in raw and "." not in raw:
            candidate = raw.replace(",", ".")
            try:
                num = float(candidate)
            except ValueError:
                num = None
            if num is not None and 0 <= num <= 100:
                return num
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(",", "")
        try:
            return float(raw)
        except ValueError:
            return None

    @classmethod
    def _extract_score_from_text(cls, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        match = re.search(r'"score"\s*:\s*([^,}\n]+)', text)
        if not match:
            return None
        return cls._extract_first_number(match.group(1))

    @classmethod
    def _coerce_score(cls, value: Any, fallback_text: str, default: float = 50.0) -> float:
        num = None
        if isinstance(value, (int, float)):
            num = float(value)
        elif isinstance(value, str):
            num = cls._extract_first_number(value)
        if num is None:
            num = cls._extract_score_from_text(fallback_text)
        if num is None:
            return default
        return max(0.0, min(100.0, num))

    @classmethod
    def _coerce_percentage(cls, value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            num = float(value)
        else:
            num = cls._extract_first_number(str(value) if value is not None else "")
        if num is None:
            return None
        return max(0.0, min(100.0, num))

    @classmethod
    def _repair_numeric_fields(cls, text: str) -> str:
        if not text:
            return text

        def replace(match: re.Match) -> str:
            prefix = match.group(1)
            raw_value = match.group(2)
            number = cls._extract_first_number(raw_value)
            if number is None:
                return match.group(0)
            if float(number).is_integer():
                number_str = str(int(number))
            else:
                number_str = str(number)
            return f"{prefix}{number_str}"

        repaired = text
        for field in cls._NUMERIC_FIELDS:
            pattern = rf'("{re.escape(field)}"\s*:\s*)([^,}}\n]+)'
            repaired = re.sub(pattern, replace, repaired)
        return repaired

    @classmethod
    def _extract_analysis_text(cls, text: str) -> str:
        if not text:
            return ""

        cleaned = text.strip()
        cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace("```", "").strip()

        analysis_match = re.search(r'"analysis"\s*:\s*"([\s\S]*?)"\s*(,|})', cleaned)
        if analysis_match:
            return analysis_match.group(1).strip()

        analysis_match = re.search(r'"analysis"\s*:\s*([^\n,}]+)', cleaned)
        if analysis_match:
            return analysis_match.group(1).strip().strip('"')

        analysis_match = re.search(r"分析[:：]\s*([^\n]+)", cleaned)
        if analysis_match:
            return analysis_match.group(1).strip()

        cleaned = cleaned.strip()
        if len(cleaned) > 400:
            return f"{cleaned[:400].rstrip()}..."
        return cleaned

    @classmethod
    def _normalize_line_density(cls, line_density: Any) -> Dict[str, Any]:
        if not isinstance(line_density, dict):
            return {}

        def pick(keys: List[str]) -> Dict[str, Any]:
            for key in keys:
                if key in line_density:
                    value = line_density.get(key)
                    if isinstance(value, dict):
                        return value
                    if value is not None:
                        return {"percentage": value}
            return {}

        return {
            "plot": pick(["plot", "plot_line", "plotLine", "story", "story_line", "情节线", "剧情线", "主线"]),
            "character": pick(["character", "character_line", "characterLine", "人物线", "角色线"]),
            "world": pick(["world", "world_line", "worldLine", "世界线", "环境线"]),
        }

    @classmethod
    def _extract_line_density_from_list(cls, line_density: Any) -> Dict[str, Any]:
        if not isinstance(line_density, list):
            return {}

        result: Dict[str, Any] = {"plot": {}, "character": {}, "world": {}}
        for item in line_density:
            if not isinstance(item, dict):
                continue
            name = str(item.get("line") or item.get("name") or item.get("type") or "").lower()
            target: Optional[str] = None
            if "plot" in name or "story" in name or "情节" in name or "剧情" in name or "主线" in name:
                target = "plot"
            elif "character" in name or "人物" in name or "角色" in name:
                target = "character"
            elif "world" in name or "世界" in name or "环境" in name:
                target = "world"
            if not target:
                continue
            result[target] = {
                **result.get(target, {}),
                **item,
            }
        return result

    @classmethod
    def _extract_line_density_from_text(cls, text: str) -> Dict[str, Any]:
        if not text:
            return {}

        def find_percent(keywords: List[str]) -> Optional[float]:
            for keyword in keywords:
                pattern = rf"{keyword}\s*(?:占比|比例|密度|比重)?\s*[:：]?\s*([0-9]+(?:[.,][0-9]+)?)\s*%?"
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    return cls._coerce_percentage(match.group(1))
                pattern = rf"{keyword}\s*.*?([0-9]+(?:[.,][0-9]+)?)\s*%"
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    return cls._coerce_percentage(match.group(1))
            return None

        plot_percent = find_percent(["情节线", "剧情线", "主线", "plot", "story"])
        character_percent = find_percent(["人物线", "角色线", "character"])
        world_percent = find_percent(["世界线", "环境线", "world"])

        result: Dict[str, Any] = {"plot": {}, "character": {}, "world": {}}
        if plot_percent is not None:
            result["plot"]["percentage"] = plot_percent
        if character_percent is not None:
            result["character"]["percentage"] = character_percent
        if world_percent is not None:
            result["world"]["percentage"] = world_percent
        return result

    @classmethod
    def _merge_line_density(cls, base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
        merged = {
            "plot": {**base.get("plot", {})},
            "character": {**base.get("character", {})},
            "world": {**base.get("world", {})},
        }
        for key in ("plot", "character", "world"):
            extra_data = extra.get(key, {})
            if not isinstance(extra_data, dict):
                continue
            merged[key] = {**merged.get(key, {}), **extra_data}
        return merged

    @classmethod
    def _fallback_parse_dimension(cls, text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        score = cls._extract_score_from_text(text)
        if score is not None:
            result["score"] = score

        analysis_text = cls._extract_analysis_text(text)
        if analysis_text:
            result["analysis"] = analysis_text

        suggestions_match = re.search(r'"suggestions"\s*:\s*(\[[\s\S]*?\])', text)
        if suggestions_match:
            try:
                result["suggestions"] = json.loads(suggestions_match.group(1))
            except json.JSONDecodeError:
                pass

        return result

    async def quick_review_content(
        self,
        content: str,
        chapter_data: Dict[str, Any],
        pass_threshold: float = 60.0
    ) -> Dict[str, Any]:
        """
        快速审查内容（用于生成时校验，不需要 chapter_id）

        Args:
            content: 章节内容
            chapter_data: 章节相关数据（角色设定、前文摘要等）
            pass_threshold: 通过阈值，低于此分数则不通过

        Returns:
            {
                "passed": bool,
                "overall_score": float,
                "feedback": str,  # 用于重写的反馈（包含正确答案）
                "details": dict   # 各维度详情
            }
        """
        try:
            # 使用精简的审查维度（OOC + consistency）以加快速度
            quick_dimensions = ["ooc", "consistency"]

            # 构建临时数据
            temp_data = {
                **chapter_data,
                "content": content
            }

            # 并行执行审查
            tasks = []
            for dim in quick_dimensions:
                if dim in self.DIMENSION_PROMPTS:
                    tasks.append(self._review_dimension(dim, temp_data))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 汇总结果
            scores = []
            feedback_parts = []
            details = {}

            for i, result in enumerate(results):
                dim_name = quick_dimensions[i]
                if isinstance(result, Exception):
                    logger.warning(f"⚠️ 快速审查维度 {dim_name} 失败: {result}")
                    continue
                elif isinstance(result, DimensionResult):
                    scores.append(result.score)
                    details[dim_name] = {
                        "score": result.score,
                        "analysis": result.analysis,
                        "suggestions": result.suggestions,
                        "details": result.details
                    }
                    # 如果分数低于阈值，收集反馈
                    if result.score < pass_threshold:
                        feedback_part = (
                            f"【{dim_name}问题 - {result.score:.0f}分】\n"
                            f"{result.analysis}\n"
                            f"建议: {'; '.join(result.suggestions[:2])}"
                        )

                        # 如果是 consistency 问题，尝试查询正确答案
                        if dim_name == "consistency" and result.details.get("inconsistencies"):
                            correct_info = await self._fetch_correct_facts(
                                chapter_data.get("project_id"),
                                result.details.get("inconsistencies", [])
                            )
                            if correct_info:
                                feedback_part += f"\n\n【📚 正确设定参考】\n{correct_info}"

                        feedback_parts.append(feedback_part)

            overall_score = sum(scores) / len(scores) if scores else 0
            passed = overall_score >= pass_threshold and all(s >= pass_threshold * 0.8 for s in scores)

            feedback = ""
            if not passed and feedback_parts:
                feedback = "请根据以下问题修正内容：\n\n" + "\n\n".join(feedback_parts)

            logger.info(
                f"🔍 快速审查完成: 综合={overall_score:.1f}, "
                f"通过={passed}, 维度数={len(scores)}"
            )

            return {
                "passed": passed,
                "overall_score": overall_score,
                "feedback": feedback,
                "details": details
            }

        except Exception as e:
            logger.error(f"❌ 快速审查失败: {e}", exc_info=True)
            # 审查失败时默认通过，避免阻塞生成
            return {
                "passed": True,
                "overall_score": 0,
                "feedback": "",
                "details": {"error": str(e)}
            }

    async def _fetch_correct_facts(
        self,
        project_id: Optional[str],
        inconsistencies: List[Dict[str, Any]]
    ) -> str:
        """
        根据发现的矛盾点，从数据库查询正确的设定信息

        Args:
            project_id: 项目ID
            inconsistencies: 矛盾点列表

        Returns:
            正确设定的描述字符串
        """
        if not project_id or not inconsistencies:
            return ""

        try:
            correct_facts = []

            for inc in inconsistencies[:3]:  # 限制查询数量
                inc_type = inc.get("type", "")
                issue = inc.get("issue", "")

                if inc_type == "item":
                    # 查询物品相关的历史记录
                    facts = await self._search_item_history(project_id, issue)
                    if facts:
                        correct_facts.append(f"【物品】{facts}")

                elif inc_type == "location":
                    # 查询位置相关的历史记录
                    facts = await self._search_location_history(project_id, issue)
                    if facts:
                        correct_facts.append(f"【位置】{facts}")

                elif inc_type == "character":
                    # 查询角色相关的设定
                    facts = await self._search_character_facts(project_id, issue)
                    if facts:
                        correct_facts.append(f"【角色】{facts}")

                elif inc_type == "timeline":
                    # 查询时间线相关的历史
                    facts = await self._search_timeline_facts(project_id, issue)
                    if facts:
                        correct_facts.append(f"【时间线】{facts}")

            return "\n".join(correct_facts) if correct_facts else ""

        except Exception as e:
            logger.warning(f"⚠️ 查询正确设定失败: {e}")
            return ""

    async def _search_item_history(self, project_id: str, issue: str) -> str:
        """搜索物品相关的历史记录"""
        try:
            # 从 state_change_log 中搜索物品变化
            result = await self.db.execute(
                select(Chapter.chapter_number, Chapter.title, Chapter.state_change_log)
                .where(Chapter.project_id == project_id)
                .where(Chapter.state_change_log.isnot(None))
                .order_by(desc(Chapter.chapter_number))
                .limit(20)
            )
            rows = result.all()

            item_events = []
            for row in rows:
                log = row.state_change_log or {}
                gained = log.get("items_gained", [])
                lost = log.get("items_lost", [])

                if gained:
                    item_events.append(f"第{row.chapter_number}章获得: {', '.join(gained)}")
                if lost:
                    item_events.append(f"第{row.chapter_number}章失去: {', '.join(lost)}")

            return "; ".join(item_events[:5]) if item_events else ""

        except Exception as e:
            logger.warning(f"⚠️ 搜索物品历史失败: {e}")
            return ""

    async def _search_location_history(self, project_id: str, issue: str) -> str:
        """搜索位置相关的历史记录"""
        try:
            result = await self.db.execute(
                select(Chapter.chapter_number, Chapter.title, Chapter.state_change_log)
                .where(Chapter.project_id == project_id)
                .where(Chapter.state_change_log.isnot(None))
                .order_by(desc(Chapter.chapter_number))
                .limit(10)
            )
            rows = result.all()

            location_events = []
            for row in rows:
                log = row.state_change_log or {}
                loc_change = log.get("location_change", {})
                if loc_change and loc_change.get("to"):
                    location_events.append(
                        f"第{row.chapter_number}章: 从{loc_change.get('from', '?')}到{loc_change.get('to')}"
                    )

            return "; ".join(location_events[:5]) if location_events else ""

        except Exception as e:
            logger.warning(f"⚠️ 搜索位置历史失败: {e}")
            return ""

    async def _search_character_facts(self, project_id: str, issue: str) -> str:
        """搜索角色相关的设定"""
        try:
            result = await self.db.execute(
                select(Character)
                .where(Character.project_id == project_id)
            )
            characters = result.scalars().all()

            facts = []
            for char in characters:
                facts.append(f"{char.name}: {char.personality or '无描述'}")

            return "; ".join(facts[:5]) if facts else ""

        except Exception as e:
            logger.warning(f"⚠️ 搜索角色设定失败: {e}")
            return ""

    async def _search_timeline_facts(self, project_id: str, issue: str) -> str:
        """搜索时间线相关的历史"""
        try:
            result = await self.db.execute(
                select(Chapter.chapter_number, Chapter.title, Chapter.state_change_log)
                .where(Chapter.project_id == project_id)
                .where(Chapter.state_change_log.isnot(None))
                .order_by(Chapter.chapter_number)
                .limit(20)
            )
            rows = result.all()

            timeline_events = []
            for row in rows:
                log = row.state_change_log or {}
                time_passed = log.get("time_passed")
                if time_passed:
                    timeline_events.append(f"第{row.chapter_number}章: {time_passed}")

            return "; ".join(timeline_events[:5]) if timeline_events else ""

        except Exception as e:
            logger.warning(f"⚠️ 搜索时间线失败: {e}")
            return ""

    async def review_chapter(
        self,
        chapter_id: str,
        dimensions: Optional[List[str]] = None
    ) -> ReviewResult:
        """
        审查章节

        Args:
            chapter_id: 章节ID
            dimensions: 要审查的维度列表，默认全部三个维度

        Returns:
            ReviewResult: 审查结果
        """
        if dimensions is None:
            dimensions = ["ooc", "pacing", "high_point", "three_line_rhythm"]

        result = ReviewResult(
            chapter_id=chapter_id,
            overall_score=0,
            reviewed_at=datetime.now().isoformat()
        )

        try:
            # 1. 加载章节内容和相关数据
            chapter_data = await self._load_chapter_data(chapter_id)
            if not chapter_data:
                result.metadata["error"] = "章节不存在或无内容"
                return result

            result.metadata["chapter_number"] = chapter_data["chapter_number"]
            result.metadata["word_count"] = len(chapter_data["content"])

            # 2. 并行执行各维度审查
            tasks = []
            for dim in dimensions:
                if dim in self.DIMENSION_PROMPTS:
                    tasks.append(self._review_dimension(dim, chapter_data))

            dimension_results = await asyncio.gather(*tasks, return_exceptions=True)

            # 3. 汇总结果
            valid_scores = []
            for i, dim_result in enumerate(dimension_results):
                dim_name = dimensions[i]
                if isinstance(dim_result, Exception):
                    logger.error(f"❌ 维度 {dim_name} 审查失败: {dim_result}")
                    result.dimensions[dim_name] = DimensionResult(
                        dimension=dim_name,
                        score=0,
                        analysis=f"审查失败: {str(dim_result)}"
                    )
                elif isinstance(dim_result, DimensionResult):
                    result.dimensions[dim_name] = dim_result
                    valid_scores.append(dim_result.score)

            # 4. 计算综合评分
            if valid_scores:
                result.overall_score = sum(valid_scores) / len(valid_scores)

            logger.info(
                f"✅ 章节审查完成 - chapter_id:{chapter_id[:8]}, "
                f"综合评分:{result.overall_score:.1f}, 维度数:{len(result.dimensions)}"
            )

        except Exception as e:
            logger.error(f"❌ 章节审查失败: {e}", exc_info=True)
            result.metadata["error"] = str(e)

        return result

    async def _load_chapter_data(self, chapter_id: str) -> Optional[Dict[str, Any]]:
        """加载章节相关数据"""
        try:
            # 获取章节
            chapter_result = await self.db.execute(
                select(Chapter).where(Chapter.id == chapter_id)
            )
            chapter = chapter_result.scalar_one_or_none()

            if not chapter or not chapter.content:
                return None

            # 获取项目
            project_result = await self.db.execute(
                select(Project).where(Project.id == chapter.project_id)
            )
            project = project_result.scalar_one_or_none()

            # 获取角色设定
            characters_result = await self.db.execute(
                select(Character).where(Character.project_id == chapter.project_id)
            )
            characters = characters_result.scalars().all()

            character_settings = "\n".join([
                f"- {c.name}: {c.personality or '无性格描述'}"
                for c in characters
            ]) if characters else "暂无角色设定"

            # 获取前文摘要
            previous_summary = ""
            if chapter.chapter_number > 1:
                prev_result = await self.db.execute(
                    select(Chapter.summary)
                    .where(Chapter.project_id == chapter.project_id)
                    .where(Chapter.chapter_number == chapter.chapter_number - 1)
                )
                prev_summary = prev_result.scalar_one_or_none()
                previous_summary = prev_summary or "无前文摘要"

            # 加载最近章节摘要（用于 consistency 维度）
            previous_summaries = await self._load_recent_summaries(
                chapter.project_id, chapter.chapter_number, limit=3
            )

            # 加载世界状态（用于 consistency 维度）
            world_state = {}
            world_time_period = "未设定"
            world_location = "未设定"
            world_rules = "未设定"
            current_location = "未知"
            inventory = "无"
            relationships = "无"

            if project:
                world_state = project.world_state or {}
                world_time_period = getattr(project, 'world_time_period', None) or "未设定"
                world_location = getattr(project, 'world_location', None) or "未设定"
                world_rules = getattr(project, 'world_rules', None) or "未设定"
                current_location = world_state.get("current_location", "未知")
                inventory = ", ".join(world_state.get("inventory", [])) or "无"
                relationships = json.dumps(
                    world_state.get("relationships", {}),
                    ensure_ascii=False
                ) if world_state.get("relationships") else "无"

            return {
                "chapter_id": chapter_id,
                "chapter_number": chapter.chapter_number,
                "title": chapter.title,
                "content": chapter.content,
                "summary": chapter.summary or "",
                "project_id": chapter.project_id,
                "project_title": project.title if project else "",
                "genre": project.genre if project else "",
                "character_settings": character_settings,
                "previous_summary": previous_summary,
                "previous_summaries": previous_summaries,
                # consistency 维度需要的字段
                "world_time_period": world_time_period,
                "world_location": world_location,
                "world_rules": world_rules,
                "current_location": current_location,
                "inventory": inventory,
                "relationships": relationships
            }

        except Exception as e:
            logger.error(f"❌ 加载章节数据失败: {e}")
            return None

    async def _load_recent_summaries(
        self,
        project_id: str,
        current_chapter: int,
        limit: int = 3
    ) -> str:
        """
        加载最近章节摘要

        Args:
            project_id: 项目ID
            current_chapter: 当前章节号
            limit: 加载数量

        Returns:
            格式化的摘要字符串
        """
        try:
            result = await self.db.execute(
                select(Chapter.chapter_number, Chapter.title, Chapter.summary)
                .where(Chapter.project_id == project_id)
                .where(Chapter.chapter_number < current_chapter)
                .where(Chapter.summary.isnot(None))
                .order_by(desc(Chapter.chapter_number))
                .limit(limit)
            )
            rows = result.all()

            if not rows:
                return "无前文摘要"

            summaries = []
            for row in reversed(rows):
                summaries.append(f"第{row.chapter_number}章《{row.title}》: {row.summary}")

            return "\n".join(summaries)

        except Exception as e:
            logger.error(f"❌ 加载章节摘要失败: {e}")
            return "无前文摘要"

    async def _review_dimension(
        self,
        dimension: str,
        chapter_data: Dict[str, Any]
    ) -> DimensionResult:
        """执行单个维度的审查"""
        try:
            prompt_template = self.DIMENSION_PROMPTS.get(dimension)
            if not prompt_template:
                raise ValueError(f"未知维度: {dimension}")

            # 构建 prompt（支持 consistency 维度的额外字段）
            prompt = prompt_template.format(
                chapter_content=chapter_data["content"][:8000],  # 限制长度
                character_settings=chapter_data.get("character_settings", ""),
                previous_summary=chapter_data.get("previous_summary", ""),
                chapter_outline=chapter_data.get("summary", ""),
                # consistency 维度需要的字段
                world_time_period=chapter_data.get("world_time_period", "未设定"),
                world_location=chapter_data.get("world_location", "未设定"),
                world_rules=chapter_data.get("world_rules", "未设定"),
                current_location=chapter_data.get("current_location", "未知"),
                inventory=chapter_data.get("inventory", "无"),
                relationships=chapter_data.get("relationships", "无"),
                previous_summaries=chapter_data.get("previous_summaries", "无前文摘要")
            )

            # 调用 AI
            response = await self.ai_service.generate_text(
                prompt=prompt,
                system_prompt="你是专业的小说编辑，请严格按照JSON格式输出分析结果。",
                max_tokens=2000
            )

            # 解析响应 - response 是字典，实际内容在 content 字段
            response_text = response.get("content", "") if isinstance(response, dict) else str(response)

            cleaned_text = self.ai_service._clean_json_response(response_text)
            data: Dict[str, Any]
            try:
                data = json.loads(cleaned_text)
            except json.JSONDecodeError:
                repaired_text = self._repair_numeric_fields(cleaned_text)
                try:
                    data = json.loads(repaired_text)
                except json.JSONDecodeError:
                    data = self._fallback_parse_dimension(cleaned_text or response_text)

            if not isinstance(data, dict):
                data = {"analysis": str(data)}

            analysis_text = data.get("analysis")
            if isinstance(analysis_text, str):
                analysis_text = analysis_text.strip()
            else:
                analysis_text = ""
            if not analysis_text:
                analysis_text = self._extract_analysis_text(cleaned_text or response_text)
            data["analysis"] = analysis_text

            score = self._coerce_score(data.get("score"), cleaned_text or response_text)
            data["score"] = score

            suggestions = data.get("suggestions", [])
            if not isinstance(suggestions, list):
                suggestions = []
            data["suggestions"] = suggestions

            composition = data.get("composition")
            if isinstance(composition, dict):
                for key in ("action_percent", "dialogue_percent", "description_percent"):
                    if key in composition:
                        percentage = self._coerce_percentage(composition.get(key))
                        if percentage is not None:
                            composition[key] = percentage

            if dimension == "three_line_rhythm":
                normalized_line_density = self._normalize_line_density(data.get("line_density"))
                list_line_density = self._extract_line_density_from_list(data.get("line_density"))
                normalized_line_density = self._merge_line_density(normalized_line_density, list_line_density)
                if not normalized_line_density:
                    normalized_line_density = {"plot": {}, "character": {}, "world": {}}

                extracted_from_text = self._extract_line_density_from_text(analysis_text)
                normalized_line_density = self._merge_line_density(normalized_line_density, extracted_from_text)
                data["line_density"] = normalized_line_density

            line_density = data.get("line_density")
            if isinstance(line_density, dict):
                for line_key in ("plot", "character", "world"):
                    line_data = line_density.get(line_key)
                    if isinstance(line_data, dict) and "percentage" in line_data:
                        percentage = self._coerce_percentage(line_data.get("percentage"))
                        if percentage is not None:
                            line_data["percentage"] = percentage

            return DimensionResult(
                dimension=dimension,
                score=score,
                analysis=data.get("analysis", ""),
                suggestions=data.get("suggestions", []),
                details=data
            )

        except json.JSONDecodeError as e:
            logger.error(f"❌ 解析 {dimension} 响应失败: {e}")
            return DimensionResult(
                dimension=dimension,
                score=50,
                analysis=f"解析失败，原始响应: {response_text[:200]}..."
            )
        except Exception as e:
            logger.error(f"❌ {dimension} 审查失败: {e}")
            raise
