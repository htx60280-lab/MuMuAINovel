"""ConStory 风格评测结果解析器。"""
from __future__ import annotations

from typing import Any, Dict, List


def normalize_constory_result(raw_result: Dict[str, Any]) -> Dict[str, Any]:
    """将外部或 LLM 输出规整为统一结构。"""

    issues = raw_result.get("issues") or []
    if not isinstance(issues, list):
        issues = []

    normalized_issues: List[Dict[str, Any]] = []
    category_counts: Dict[str, int] = {}

    for item in issues:
        if not isinstance(item, dict):
            continue

        category = str(item.get("category") or "other")
        normalized = {
            "category": category,
            "subcategory": item.get("subcategory"),
            "severity": item.get("severity") or "medium",
            "title": item.get("title") or "一致性问题",
            "description": item.get("description") or "",
            "exact_quote": item.get("exact_quote") or "",
            "location_text": item.get("location") or item.get("location_text") or "",
            "chapter_number": item.get("chapter_number"),
            "evidence_json": {
                "reason": item.get("reason"),
                "confidence": item.get("confidence"),
            }
        }
        normalized_issues.append(normalized)
        category_counts[category] = category_counts.get(category, 0) + 1

    score = raw_result.get("overall_score")
    if score is None:
        score = max(0.0, 100.0 - len(normalized_issues) * 8.0)

    return {
        "overall_score": float(score),
        "issue_count": len(normalized_issues),
        "summary": {
            "category_counts": category_counts,
            "score_breakdown": raw_result.get("score_breakdown") or {},
            "summary_text": raw_result.get("summary") or "",
        },
        "issues": normalized_issues,
        "raw_result": raw_result,
    }
