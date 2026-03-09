"""ConStory 风格评测适配器。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from app.logger import get_logger
from app.services.ai_service import AIService


logger = get_logger(__name__)


class ConStoryAdapter:
    """整书一致性评测适配器。"""

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
