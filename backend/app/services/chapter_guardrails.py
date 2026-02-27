"""规则护栏服务 - ChapterGuardrails

纯正则/规则检测，零 AI 调用，用于章节生成后检查：
1. 禁止角色名检测 - 检查未登场角色是否被误用
2. 全知视角标记词检测 - 检查限知视角下是否出现全知叙述
3. 突兀登场检测 - 检查新角色是否缺少介绍性描写
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Violation:
    """护栏违规记录"""
    type: str           # "forbidden_name" | "omniscient_cue" | "sudden_familiarity"
    severity: str       # "high" | "medium"
    description: str
    position: int
    context: str        # 违规前后 50 字


@dataclass
class GuardrailResult:
    """护栏检查结果"""
    passed: bool
    violations: List[Violation] = field(default_factory=list)
    summary: str = ""


# ==================== 全知视角标记词 ====================
OMNISCIENT_CUES = [
    r"与此同时",
    r"另一边",
    r"殊不知",
    r"他并不知道",
    r"她并不知道",
    r"他们并不知道",
    r"在他不知道的地方",
    r"在她不知道的地方",
    r"远在.{1,10}的.{1,6}正在",
    r"而此刻.{1,10}却",
    r"此时此刻.{0,5}另一",
    r"在千里之外",
]

# ==================== 介绍性描写指示词 ====================
INTRO_INDICATORS = [
    r"看见", r"看到", r"注意到", r"发现", r"出现", r"走来", r"走进",
    r"站着", r"坐着", r"一个.{0,3}人", r"一位",
    r"陌生", r"不认识", r"第一次见", r"从未见过",
    r"身穿", r"穿着", r"长相", r"面容", r"身材", r"气质",
    r"自称", r"自我介绍", r"名叫", r"叫做",
]

# 需要启用全知视角检测的叙事视角关键词
_LIMITED_PERSPECTIVES = {"第一人称", "first_person", "限知", "限知第三人称"}


class ChapterGuardrails:
    """章节规则护栏（纯正则，零 AI 调用）"""

    def __init__(self):
        self._omniscient_pattern = re.compile("|".join(OMNISCIENT_CUES))
        self._intro_pattern = re.compile("|".join(INTRO_INDICATORS))

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def check(
        self,
        text: str,
        forbidden_characters: List[str],
        new_characters: List[str],
        narrative_perspective: str,
    ) -> GuardrailResult:
        """
        执行全部规则检查。

        Args:
            text: 生成的章节正文
            forbidden_characters: 不允许出现的角色名列表
            new_characters: 本章首次登场的角色名列表
            narrative_perspective: 叙事视角（用于判断是否启用全知检测）

        Returns:
            GuardrailResult
        """
        violations: List[Violation] = []

        # A) 禁止角色名检测
        violations.extend(self._check_forbidden_names(text, forbidden_characters))

        # B) 全知视角标记词检测（仅限知视角）
        if self._is_limited_perspective(narrative_perspective):
            violations.extend(self._check_omniscient_cues(text))

        # C) 突兀登场检测
        violations.extend(self._check_sudden_familiarity(text, new_characters))

        passed = len(violations) == 0
        high_count = sum(1 for v in violations if v.severity == "high")
        medium_count = sum(1 for v in violations if v.severity == "medium")

        summary = "护栏检查通过" if passed else (
            f"检测到 {len(violations)} 个违规"
            f"（高严重度 {high_count}，中严重度 {medium_count}）"
        )

        logger.info(f"🛡️ 规则护栏检查: {summary}")
        return GuardrailResult(passed=passed, violations=violations, summary=summary)

    def format_violations_for_rewrite(self, result: GuardrailResult) -> str:
        """将违规列表格式化为可注入重写提示词的文本。"""
        if not result.violations:
            return ""

        lines = ["以下是规则护栏检测到的违规问题：\n"]
        for i, v in enumerate(result.violations, 1):
            lines.append(
                f"{i}. [{v.severity.upper()}] {v.type}: {v.description}\n"
                f"   上下文: ...{v.context}..."
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部检查方法
    # ------------------------------------------------------------------

    def _check_forbidden_names(
        self, text: str, forbidden_characters: List[str]
    ) -> List[Violation]:
        """检查文本中是否出现禁止的角色名。"""
        violations = []
        for name in forbidden_characters:
            if not name or len(name) < 2:
                continue
            pattern = re.compile(re.escape(name))
            for match in pattern.finditer(text):
                pos = match.start()
                context = text[max(0, pos - 50): pos + len(name) + 50]
                violations.append(Violation(
                    type="forbidden_name",
                    severity="high",
                    description=f"出现了未登场角色「{name}」",
                    position=pos,
                    context=context,
                ))
        return violations

    def _check_omniscient_cues(self, text: str) -> List[Violation]:
        """检查限知视角下是否出现全知叙述标记词。"""
        violations = []
        for match in self._omniscient_pattern.finditer(text):
            pos = match.start()
            context = text[max(0, pos - 50): pos + len(match.group()) + 50]
            violations.append(Violation(
                type="omniscient_cue",
                severity="medium",
                description=f"限知视角下出现全知叙述标记「{match.group()}」",
                position=pos,
                context=context,
            ))
        return violations

    def _check_sudden_familiarity(
        self, text: str, new_characters: List[str]
    ) -> List[Violation]:
        """检查新角色首次出现前是否有介绍性描写。"""
        violations = []
        for name in new_characters:
            if not name or len(name) < 2:
                continue
            pattern = re.compile(re.escape(name))
            match = pattern.search(text)
            if not match:
                continue  # 名字未出现，跳过

            pos = match.start()
            # 取名字首次出现前 120 字
            before_text = text[max(0, pos - 120): pos]
            # 检查前文是否有介绍性描写
            if not self._intro_pattern.search(before_text):
                context = text[max(0, pos - 50): pos + len(name) + 50]
                violations.append(Violation(
                    type="sudden_familiarity",
                    severity="medium",
                    description=f"新角色「{name}」首次出现缺少介绍性描写",
                    position=pos,
                    context=context,
                ))
        return violations

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _is_limited_perspective(perspective: str) -> bool:
        """判断是否为限知视角（需要启用全知检测）。"""
        if not perspective:
            return False
        perspective_lower = perspective.lower().strip()
        for keyword in _LIMITED_PERSPECTIVES:
            if keyword in perspective_lower:
                return True
        return False
