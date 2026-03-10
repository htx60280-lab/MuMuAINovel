"""内置版 ConStory-Checker 最小实现。

基于官方 `ConStory-Bench` 的 judge 核心流程整理，
仅保留 NovelForge 接入所需的最小能力：
- 加载 prompts
- 通过 OpenAI-compatible API 并发评测 5 个大类
- 解析结构化 JSON 输出
- 产出与官方 CSV 兼容的列结构
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp


MAX_TOKENS = 10000
TEMPERATURE = 0.5
MAX_RETRIES = 3
RETRY_DELAY_BASE = 5
REQUEST_TIMEOUT = 600
CONNECT_TIMEOUT = 30
BATCH_DELAY_SECONDS = 1

FATAL_ERROR_CODES = {
    "Arrearage", "InvalidApiKey", "Unauthorized",
    "AccountDisabled", "InsufficientBalance",
}

PROMPT_FILE_MAPPING = {
    "characterization": "characterization.md",
    "factual_detail": "factual_detail.md",
    "narrative_style": "narrative_style.md",
    "timeline_plot": "timeline_plot.md",
    "world_building": "world_building.md",
}

EVALUATION_CRITERIA = {
    "characterization": {
        "sub_criteria": [
            "memory_contradictions",
            "knowledge_contradictions",
            "skill_power_fluctuations",
            "forgotten_abilities",
        ],
    },
    "factual_detail": {
        "sub_criteria": [
            "appearance_mismatches",
            "nomenclature_confusions",
            "quantitative_mismatches",
        ],
    },
    "narrative_style": {
        "sub_criteria": [
            "perspective_confusions",
            "tone_inconsistencies",
            "style_shifts",
        ],
    },
    "timeline_plot": {
        "sub_criteria": [
            "absolute_time_contradictions",
            "duration_timeline_contradictions",
            "simultaneity_contradictions",
            "causeless_effects",
            "causal_logic_violations",
            "abandoned_plot_elements",
        ],
    },
    "world_building": {
        "sub_criteria": [
            "core_rules_violations",
            "social_norms_violations",
            "geographical_contradictions",
        ],
    },
}


class FatalAPIError(Exception):
    """不可恢复 API 错误。"""


def create_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    return logger


def load_prompt_templates(prompts_dir: str) -> Dict[str, str]:
    templates: Dict[str, str] = {}
    for criteria, filename in PROMPT_FILE_MAPPING.items():
        filepath = os.path.join(prompts_dir, filename)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Prompt template not found: {filepath}")
        with open(filepath, "r", encoding="utf-8") as f:
            templates[criteria] = f.read()
    return templates


class JudgeLLMClient:
    """基于 OpenAI-compatible API 的异步裁判客户端。"""

    def __init__(self, api_base: str, api_key: str, model: str, max_concurrent: int, logger: logging.Logger):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.logger = logger

    async def evaluate_criteria(
        self,
        session: aiohttp.ClientSession,
        prompt_template: str,
        story_content: str,
        criteria_name: str,
    ) -> Dict[str, Any]:
        async with self.semaphore:
            prompt = prompt_template.replace("{{ Content }}", story_content)
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": TEMPERATURE,
                "max_tokens": MAX_TOKENS,
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            for retry in range(MAX_RETRIES):
                try:
                    async with session.post(
                        f"{self.api_base}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            msg = result["choices"][0]["message"]
                            content = msg.get("content", "")
                            return {"success": True, "content": content}

                        error_text = await resp.text()
                        try:
                            error_json = json.loads(error_text)
                            code = error_json.get("error", {}).get("code", "")
                            if code in FATAL_ERROR_CODES:
                                raise FatalAPIError(code)
                        except json.JSONDecodeError:
                            pass

                        if retry < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY_BASE * (retry + 1))
                        else:
                            return {"success": False, "error": f"HTTP {resp.status}"}
                except FatalAPIError:
                    raise
                except Exception as error:
                    self.logger.warning(f"[{criteria_name}] retry {retry + 1}/{MAX_RETRIES} error: {error}")
                    if retry < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY_BASE * (retry + 1))
                    else:
                        return {"success": False, "error": str(error)}

            return {"success": False, "error": "Max retries exceeded"}


def parse_criteria_response(response_content: str, sub_criteria_list: List[str]) -> Dict[str, str]:
    try:
        text = response_content.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                return _extract_subcriteria(parsed, sub_criteria_list)
            except json.JSONDecodeError:
                pass

        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response_content, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                return _extract_subcriteria(parsed, sub_criteria_list)
            except json.JSONDecodeError:
                pass

        results: Dict[str, str] = {}
        lower = response_content.lower()
        for sc in sub_criteria_list:
            variations = [sc, sc.replace("_", " "), sc.replace("_", "-")]
            found = False
            for var in variations:
                pattern = rf"{re.escape(var.lower())}[:\s]*(\[.*?\])"
                matched = re.search(pattern, lower, re.DOTALL)
                if matched:
                    try:
                        json.loads(matched.group(1))
                        results[sc] = matched.group(1)
                        found = True
                        break
                    except json.JSONDecodeError:
                        continue
            if not found:
                results[sc] = "[]"
        return results
    except Exception:
        return {sc: "[]" for sc in sub_criteria_list}


def _extract_subcriteria(parsed: dict, sub_criteria_list: List[str]) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for sc in sub_criteria_list:
        value = parsed.get(sc)
        if isinstance(value, (list, dict)):
            results[sc] = json.dumps(value, ensure_ascii=False)
        else:
            results[sc] = "[]"
    return results


class ConStoryChecker:
    """内置版 ConStory-Checker。"""

    def __init__(self, client: JudgeLLMClient, prompt_templates: Dict[str, str], story_column: str, logger: logging.Logger):
        self.client = client
        self.templates = prompt_templates
        self.story_column = story_column
        self.logger = logger

    async def evaluate_single(self, session: aiohttp.ClientSession, story_data: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(story_data)
        result["evaluation_timestamp"] = datetime.now().isoformat()
        result["evaluation_status"] = "in_progress"
        result["judge_model"] = self.client.model

        for cat, cfg in EVALUATION_CRITERIA.items():
            for sc in cfg["sub_criteria"]:
                result[f"{cat}_{sc}"] = "[]"

        story_text = story_data.get(self.story_column, "")
        tasks = {
            cat: asyncio.create_task(
                self.client.evaluate_criteria(session, self.templates[cat], story_text, cat)
            )
            for cat in EVALUATION_CRITERIA
        }

        completed_count = 0
        for cat, task in tasks.items():
            cfg = EVALUATION_CRITERIA[cat]
            try:
                response = await task
            except Exception as error:
                response = {"success": False, "error": str(error)}

            if response.get("success"):
                completed_count += 1
                parsed = parse_criteria_response(response.get("content", ""), cfg["sub_criteria"])
                for sc in cfg["sub_criteria"]:
                    result[f"{cat}_{sc}"] = parsed.get(sc, "[]")
            else:
                error_message = response.get("error", "Unknown")
                for sc in cfg["sub_criteria"]:
                    result[f"{cat}_{sc}"] = f"ERROR: {error_message}"

        result["evaluation_status"] = "completed" if completed_count == 5 else f"partial_{completed_count}_5"
        return result

    async def run_single_story(self, story_text: str) -> Dict[str, Any]:
        connector = aiohttp.TCPConnector(limit=10)
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT + 60)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            return await self.evaluate_single(session, {"id": 1, self.story_column: story_text})
