"""ConStory 真实 Checker 适配器。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.benchmark.constory_checker import (
    ConStoryChecker,
    JudgeLLMClient,
    create_logger,
    load_prompt_templates,
)
from app.logger import get_logger
from app.services.ai_service import AIService


logger = get_logger(__name__)


class ConStoryAdapter:
    """整书一致性评测适配器。

    优先调用真实 `ConStory-Bench` 的 `constory.judge` 逻辑；
    若本地源码不存在或执行失败，则回退到当前 PoC 提示词评测方案。
    """

    CONSTORY_PROMPTS_DIR = Path(__file__).resolve().parent / "constory_prompts"

    CATEGORY_HINTS = {
        "事实矛盾": ["身份", "名字", "年龄", "关系", "地点", "事实"],
        "时间矛盾": ["昨天", "今天", "明天", "时间", "先后", "时序"],
        "角色失真": ["性格", "动机", "行为", "人设", "口吻"],
        "世界规则矛盾": ["设定", "规则", "能力", "境界", "世界观"],
        "事件链断裂": ["因果", "结果", "经过", "转折", "承接"],
    }

    def __init__(self, ai_service: AIService):
        self.ai_service = ai_service

    async def evaluate(
        self,
        story_text: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        real_checker_result = await self._evaluate_with_real_checker(story_text, provider=provider, model=model)
        if real_checker_result is not None:
            return real_checker_result

        prompt = self._build_prompt(story_text)
        response = await self.ai_service.generate_text(
            prompt=prompt,
            system_prompt="你是长篇小说一致性评测器，请严格输出 JSON。",
            provider=provider,
            model=model,
            max_tokens=2600,
            auto_mcp=False,
            tool_choice="none",
        )
        response_text = response.get("content", "") if isinstance(response, dict) else str(response)
        cleaned = self.ai_service._clean_json_response(response_text)

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except Exception as error:
            logger.warning(f"⚠️ ConStory PoC 结果解析失败，使用回退解析: {error}")

        return self._fallback_parse(response_text)

    async def _evaluate_with_real_checker(
        self,
        story_text: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.CONSTORY_PROMPTS_DIR.exists():
            logger.info("ℹ️ 未检测到本地 ConStory prompts，回退 PoC 评测器")
            return None

        judge_model = model or getattr(self.ai_service, 'default_model', None)
        api_key = self._extract_api_key(provider)
        api_base = self._extract_api_base(provider)
        if not judge_model or not api_key or not api_base:
            logger.warning("⚠️ 缺少真实 ConStory judge 所需配置，回退 PoC 评测器")
            return None

        try:
            return await self._run_constory_judge(
                story_text=story_text,
                judge_model=judge_model,
                api_base=api_base,
                api_key=api_key,
            )
        except Exception as error:
            logger.warning(f"⚠️ 真实 ConStory judge 执行失败，回退 PoC 评测器: {error}")
            return None

    async def _run_constory_judge(
        self,
        story_text: str,
        judge_model: str,
        api_base: str,
        api_key: str,
    ) -> Dict[str, Any]:
        logger_obj = create_logger("novelforge_constory_judge")
        templates = load_prompt_templates(str(self.CONSTORY_PROMPTS_DIR))
        client = JudgeLLMClient(
            api_base=api_base,
            api_key=api_key,
            model=judge_model,
            max_concurrent=1,
            logger=logger_obj,
        )
        checker = ConStoryChecker(
            client=client,
            prompt_templates=templates,
            story_column="generated_story",
            logger=logger_obj,
        )

        row = await checker.run_single_story(story_text)
        if not row:
            return {
                "overall_score": 100,
                "summary": "真实 ConStory checker 未返回问题结果。",
                "score_breakdown": {},
                "issues": [],
            }

        return self._convert_checker_row_to_result(row)

    def _convert_checker_row_to_result(self, row: Dict[str, Any]) -> Dict[str, Any]:
        category_mapping = {
            "characterization": "角色失真",
            "factual_detail": "事实矛盾",
            "narrative_style": "叙事风格漂移",
            "timeline_plot": "时间与剧情链矛盾",
            "world_building": "世界规则矛盾",
        }
        issues: List[Dict[str, Any]] = []
        category_counts: Dict[str, int] = {}

        for key, value in row.items():
            if not isinstance(value, str):
                continue
            if not any(key.startswith(prefix + "_") for prefix in category_mapping):
                continue
            if value.startswith("ERROR:"):
                continue

            try:
                payload = json.loads(value)
            except Exception:
                continue
            if not isinstance(payload, list):
                continue

            prefix = key.split("_", 1)[0]
            subcategory = key[len(prefix) + 1:]
            category = category_mapping.get(prefix, prefix)

            for item in payload:
                if not isinstance(item, dict):
                    continue
                issue = {
                    "category": category,
                    "subcategory": subcategory,
                    "severity": "medium",
                    "title": item.get("error_element") or subcategory,
                    "description": item.get("context") or "",
                    "exact_quote": item.get("exact_quote") or "",
                    "location": item.get("location") or "",
                    "chapter_number": self._extract_chapter_number(item.get("location") or ""),
                    "reason": item.get("context") or "",
                    "confidence": 1.0,
                }
                issues.append(issue)
                category_counts[category] = category_counts.get(category, 0) + 1

        overall_score = max(0.0, 100.0 - len(issues) * 5.0)
        return {
            "overall_score": overall_score,
            "summary": f"真实 ConStory-Checker 检测到 {len(issues)} 个一致性问题。",
            "score_breakdown": category_counts,
            "issues": issues,
        }

    def _extract_api_key(self, provider: Optional[str]) -> Optional[str]:
        if provider == "openai" and getattr(self.ai_service, "_openai_provider", None):
            return getattr(self.ai_service._openai_provider.client, "api_key", None)
        if provider == "anthropic" and getattr(self.ai_service, "_anthropic_provider", None):
            return getattr(self.ai_service._anthropic_provider.client, "api_key", None)
        if provider == "gemini" and getattr(self.ai_service, "_gemini_provider", None):
            return getattr(self.ai_service._gemini_provider.client, "api_key", None)
        if getattr(self.ai_service, "_openai_provider", None):
            return getattr(self.ai_service._openai_provider.client, "api_key", None)
        return None

    def _extract_api_base(self, provider: Optional[str]) -> Optional[str]:
        if provider == "openai" and getattr(self.ai_service, "_openai_provider", None):
            return getattr(self.ai_service._openai_provider.client, "base_url", None)
        if provider == "anthropic" and getattr(self.ai_service, "_anthropic_provider", None):
            return getattr(self.ai_service._anthropic_provider.client, "base_url", None)
        if provider == "gemini" and getattr(self.ai_service, "_gemini_provider", None):
            return getattr(self.ai_service._gemini_provider.client, "base_url", None)
        if getattr(self.ai_service, "_openai_provider", None):
            return getattr(self.ai_service._openai_provider.client, "base_url", None)
        return None

    def _extract_chapter_number(self, location: str) -> Optional[int]:
        match = re.search(r"Chapter\s*(\d+)", location, re.IGNORECASE)
        if match:
            return int(match.group(1))
        match = re.search(r"第\s*(\d+)\s*章", location)
        if match:
            return int(match.group(1))
        return None

    def _build_prompt(self, story_text: str) -> str:
        return f"""
请你以 ConStory-Checker 风格，检查下面这段长篇故事是否存在跨章节一致性问题。

重点检查以下五类问题：
1. 事实矛盾
2. 时间矛盾
3. 角色失真
4. 世界规则矛盾
5. 事件链断裂

请输出 JSON，格式必须如下：
{{
  "overall_score": 0-100 的数字,
  "summary": "100字以内中文总结",
  "score_breakdown": {{
    "factual": 0-100,
    "temporal": 0-100,
    "character": 0-100,
    "world_rule": 0-100,
    "event_chain": 0-100
  }},
  "issues": [
    {{
      "category": "事实矛盾/时间矛盾/角色失真/世界规则矛盾/事件链断裂",
      "subcategory": "更细的问题类型",
      "severity": "low/medium/high",
      "title": "问题标题",
      "description": "问题描述",
      "exact_quote": "原文证据",
      "location": "第X章或位置说明",
      "chapter_number": 章节号整数或 null,
      "reason": "为什么这是矛盾"
    }}
  ]
}}

如果没有明显问题，也必须返回空数组 issues。

【待评测故事】
{story_text[:45000]}
""".strip()

    def _fallback_parse(self, text: str) -> Dict[str, Any]:
        issues: List[Dict[str, Any]] = []
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines[:10]:
            category = self._guess_category(line)
            chapter_match = re.search(r"第\s*(\d+)\s*章", line)
            issues.append({
                "category": category,
                "subcategory": "回退解析",
                "severity": "medium",
                "title": line[:40],
                "description": line,
                "exact_quote": "",
                "location": chapter_match.group(0) if chapter_match else "",
                "chapter_number": int(chapter_match.group(1)) if chapter_match else None,
                "reason": "模型未按 JSON 输出，已使用回退解析。",
            })

        return {
            "overall_score": max(40, 100 - len(issues) * 10),
            "summary": "评测器未返回标准 JSON，结果为回退解析，仅供参考。",
            "score_breakdown": {},
            "issues": issues,
        }

    def _guess_category(self, text: str) -> str:
        for category, keywords in self.CATEGORY_HINTS.items():
            if any(keyword in text for keyword in keywords):
                return category
        return "事实矛盾"
