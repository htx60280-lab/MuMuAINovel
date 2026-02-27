"""JSON 处理工具类"""
import json
import re
from typing import Any, Dict, List, Union
from app.logger import get_logger

logger = get_logger(__name__)


def clean_json_response(text: str) -> str:
    """清洗 AI 返回的 JSON（改进版 - 流式安全）"""
    try:
        if not text:
            logger.warning("⚠️ clean_json_response: 输入为空")
            return text
        
        original_length = len(text)
        logger.debug(f"🔍 开始清洗JSON，原始长度: {original_length}")
        
        # 去除推理模型的思考标签（如 DeepSeek-R1 的 <think>...</think>）
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        text = text.strip()

        # 去除 markdown 代码块
        text = re.sub(r'^```json\s*\n?', '', text, flags=re.MULTILINE | re.IGNORECASE)
        text = re.sub(r'^```\s*\n?', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)
        text = text.strip()
        
        if len(text) != original_length:
            logger.debug(f"   移除markdown后长度: {len(text)}")
        
        # 尝试直接解析（快速路径）
        try:
            json.loads(text)
            logger.debug(f"✅ 直接解析成功，无需清洗")
            return text
        except:
            pass
        
        # 找到第一个 { 或 [
        start = -1
        for i, c in enumerate(text):
            if c in ('{', '['):
                start = i
                break
        
        if start == -1:
            logger.warning(f"⚠️ 未找到JSON起始符号 {{ 或 [")
            logger.debug(f"   文本预览: {text[:200]}")
            return text
        
        if start > 0:
            logger.debug(f"   跳过前{start}个字符")
            text = text[start:]
        
        # 改进的括号匹配算法（更严格的字符串处理）
        stack = []
        i = 0
        end = -1
        in_string = False
        
        while i < len(text):
            c = text[i]
            
            # 处理字符串状态
            if c == '"':
                if not in_string:
                    # 进入字符串
                    in_string = True
                else:
                    # 检查是否是转义的引号
                    num_backslashes = 0
                    j = i - 1
                    while j >= 0 and text[j] == '\\':
                        num_backslashes += 1
                        j -= 1
                    
                    # 偶数个反斜杠表示引号未被转义，字符串结束
                    if num_backslashes % 2 == 0:
                        in_string = False
                
                i += 1
                continue
            
            # 在字符串内部，跳过所有字符
            if in_string:
                i += 1
                continue
            
            # 处理括号（只有在字符串外部才有效）
            if c == '{' or c == '[':
                stack.append(c)
            elif c == '}':
                if len(stack) > 0 and stack[-1] == '{':
                    stack.pop()
                    if len(stack) == 0:
                        end = i + 1
                        logger.debug(f"✅ 找到JSON结束位置: {end}")
                        break
                elif len(stack) > 0:
                    # 括号不匹配，可能是损坏的JSON，尝试继续
                    logger.warning(f"⚠️ 括号不匹配：遇到 }} 但栈顶是 {stack[-1]}")
                else:
                    # 栈为空遇到 }，忽略多余的闭合括号
                    logger.warning(f"⚠️ 遇到多余的 }}，忽略")
            elif c == ']':
                if len(stack) > 0 and stack[-1] == '[':
                    stack.pop()
                    if len(stack) == 0:
                        end = i + 1
                        logger.debug(f"✅ 找到JSON结束位置: {end}")
                        break
                elif len(stack) > 0:
                    # 括号不匹配，可能是损坏的JSON，尝试继续
                    logger.warning(f"⚠️ 括号不匹配：遇到 ] 但栈顶是 {stack[-1]}")
                else:
                    # 栈为空遇到 ]，忽略多余的闭合括号
                    logger.warning(f"⚠️ 遇到多余的 ]，忽略")
            
            i += 1
        
        # 检查未闭合的字符串
        if in_string:
            logger.warning("⚠️ 字符串未闭合，JSON可能不完整")
            fixed_text = _attempt_close_unterminated_string(text)
            if fixed_text:
                logger.info("✅ 已尝试修复未闭合字符串")
                text = fixed_text
                return _finalize_cleaned_json(text)
        
        # 提取结果并进行二次修复与校验
        if end > 0:
            result = text[:end]
            logger.debug(f"✅ JSON清洗完成，结果长度: {len(result)}")
        else:
            result = text
            logger.warning(f"⚠️ 未找到JSON结束位置，返回全部内容（长度: {len(result)}）")
            logger.debug(f"   栈状态: {stack}")

        return _finalize_cleaned_json(result)
        
    except Exception as e:
        logger.error(f"❌ clean_json_response 出错: {e}")
        logger.error(f"   文本长度: {len(text) if text else 0}")
        logger.error(f"   文本预览: {text[:200] if text else 'None'}")
        raise


def parse_json(text: str) -> Union[Dict, List]:
    """解析 JSON"""
    try:
        cleaned = clean_json_response(text)
        return json.loads(cleaned)
    except Exception as e:
        logger.error(f"❌ parse_json 出错: {e}")
        logger.error(f"   原始文本长度: {len(text) if text else 0}")
        logger.error(f"   清洗后文本长度: {len(cleaned) if cleaned else 0}")
        raise


def _finalize_cleaned_json(result: str) -> str:
    """二次修复并验证清洗后的 JSON 文本"""
    try:
        json.loads(result)
        logger.debug("✅ 清洗后JSON验证成功")
        return result
    except json.JSONDecodeError as e:
        logger.error(f"❌ 清洗后JSON仍然无效: {e}")
        logger.debug(f"   结果预览: {result[:500]}")
        logger.debug(f"   结果结尾: ...{result[-200:]}")

        # 优先尝试修复字符串值中的未转义引号
        repaired = _repair_unescaped_quotes(result)
        if repaired:
            try:
                json.loads(repaired)
                logger.info("✅ 未转义引号修复成功")
                return repaired
            except json.JSONDecodeError as retry_error:
                logger.debug(f"   未转义引号修复后仍无效: {retry_error}")

        # 尝试截断修复（处理被截断的 JSON）
        repaired = _repair_truncated_json(result)
        if repaired:
            try:
                json.loads(repaired)
                logger.info("✅ 截断修复成功")
                return repaired
            except json.JSONDecodeError as retry_error:
                logger.debug(f"   截断修复后仍无效: {retry_error}")

        # 回退到简单的未闭合字符串修复
        repaired = _attempt_close_unterminated_string(result)
        if repaired:
            try:
                json.loads(repaired)
                logger.info("✅ 已修复未闭合字符串导致的JSON错误")
                return repaired
            except json.JSONDecodeError as retry_error:
                logger.error(f"❌ 修复后JSON仍然无效: {retry_error}")

        return result


def _repair_unescaped_quotes(text: str) -> Union[str, None]:
    """
    修复 JSON 字符串值中的未转义引号。

    当 AI 返回的 JSON 中字符串值包含未转义的引号（如 "卡塔"）时，
    标准 json.loads 会报错。此函数逐字符扫描，识别出字符串值中的
    非结构性引号并添加反斜杠转义。

    策略：逐字符遍历 JSON 文本，维护状态机：
    - 在字符串外部正常处理
    - 进入字符串后，遇到 " 时判断它是否是字符串结束：
      检查后续字符是否是 JSON 结构字符（, : ] } 或空白后跟这些）
      如果不是，则认为这是字符串内部的未转义引号，添加转义
    """
    if not text or len(text) < 2:
        return None

    # 先尝试直接解析，如果成功就不需要修复
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    result = []
    i = 0
    in_string = False
    fixed_count = 0

    while i < len(text):
        c = text[i]

        if not in_string:
            result.append(c)
            if c == '"':
                in_string = True
            i += 1
            continue

        # 在字符串内部
        if c == '\\':
            # 转义序列，原样保留两个字符
            result.append(c)
            if i + 1 < len(text):
                i += 1
                result.append(text[i])
            i += 1
            continue

        if c == '"':
            # 判断这个引号是字符串结束还是字符串内部的未转义引号
            # 向后看跳过空白，检查下一个有效字符是否是 JSON 结构字符
            j = i + 1
            while j < len(text) and text[j] in ' \t\r\n':
                j += 1

            if j >= len(text):
                # 到末尾了，这是字符串结束
                result.append(c)
                in_string = False
                i += 1
                continue

            next_char = text[j]
            # JSON 字符串结束后合法的下一个字符: , : ] } 或另一个 key 的开始
            if next_char in (',', ':', ']', '}'):
                # 这是字符串结束
                result.append(c)
                in_string = False
                i += 1
                continue

            # 特殊情况：紧跟换行后的 "key": 模式（下一行是新的 key-value pair）
            # 如 ..."value"\n  "next_key": ...
            # 这种情况 next_char 是 " ，检查是否像 key-value 模式
            if next_char == '"':
                # 检查是否是 "...",\n  "key": 或 "..."\n} 的模式
                # 找到下一个引号后面是否跟 :
                k = j + 1
                while k < len(text) and text[k] != '"' and text[k] != '\n':
                    k += 1
                if k < len(text) and text[k] == '"':
                    # 找到了下一个引号，检查后面是否是 :
                    k2 = k + 1
                    while k2 < len(text) and text[k2] in ' \t':
                        k2 += 1
                    if k2 < len(text) and text[k2] == ':':
                        # 这是 "value"\n"key": 模式，当前引号是字符串结束
                        result.append(c)
                        in_string = False
                        i += 1
                        continue

                # 否则可能是字符串内部的引号（如 "他说"好的"然后走了"）
                # 转义它
                result.append('\\')
                result.append(c)
                fixed_count += 1
                i += 1
                continue

            # next_char 不是结构字符也不是引号，说明当前引号在字符串内部
            # 例如 "那种"卡塔"一声" -> 遇到"卡，不是结构字符
            result.append('\\')
            result.append(c)
            fixed_count += 1
            i += 1
            continue

        # 普通字符
        result.append(c)
        i += 1

    if fixed_count > 0:
        repaired = ''.join(result)
        logger.info(f"🔧 修复了 {fixed_count} 处未转义引号")
        return repaired

    return None


def _repair_truncated_json(text: str) -> Union[str, None]:
    """
    修复被截断的 JSON。

    策略：从末尾向前查找最后一个完整的对象/数组元素边界，
    截断不完整部分，然后补齐缺失的闭合括号。
    """
    if not text or len(text) < 2:
        return None

    # 逐步尝试：从末尾回溯找到可以截断的安全位置
    # 安全位置是：字符串外的 }, 或 ], 或 }, ] 或 ], } 等
    # 也就是最后一个完整元素结束的位置

    # 方法：从后向前扫描，找到最后一个不在字符串内的 } 或 ]
    # 然后尝试从那个位置截断并补齐括号
    candidates = []

    in_string = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            if not in_string:
                in_string = True
            else:
                num_bs = 0
                j = i - 1
                while j >= 0 and text[j] == '\\':
                    num_bs += 1
                    j -= 1
                if num_bs % 2 == 0:
                    in_string = False
        elif not in_string and c in ('}', ']'):
            candidates.append(i)
        i += 1

    # 从最近的完整闭合位置开始尝试
    for pos in reversed(candidates):
        truncated = text[:pos + 1]

        # 计算字符串外的未闭合括号
        stack = []
        in_str = False
        idx = 0
        while idx < len(truncated):
            ch = truncated[idx]
            if ch == '"':
                if not in_str:
                    in_str = True
                else:
                    nbs = 0
                    k = idx - 1
                    while k >= 0 and truncated[k] == '\\':
                        nbs += 1
                        k -= 1
                    if nbs % 2 == 0:
                        in_str = False
            elif not in_str:
                if ch in ('{', '['):
                    stack.append(ch)
                elif ch == '}' and stack and stack[-1] == '{':
                    stack.pop()
                elif ch == ']' and stack and stack[-1] == '[':
                    stack.pop()
            idx += 1

        # 补齐缺失的闭合括号
        closing = ""
        for bracket in reversed(stack):
            closing += '}' if bracket == '{' else ']'

        candidate = truncated + closing

        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            # 可能截断位置刚好在逗号后面（尾逗号），尝试移除
            stripped = truncated.rstrip()
            if stripped.endswith(','):
                candidate2 = stripped[:-1] + closing
                try:
                    json.loads(candidate2)
                    return candidate2
                except json.JSONDecodeError:
                    pass
            continue

    return None


def _attempt_close_unterminated_string(text: str) -> Union[str, None]:
    """尝试修复未闭合字符串，仅在末尾追加必要字符（简单回退方案）"""
    if not text:
        return None

    trimmed = text.rstrip()
    has_trailing_backslash = trimmed.endswith("\\")
    if has_trailing_backslash:
        trimmed = trimmed[:-1]

    # 尝试补一个引号与缺失的括号
    if '"' in trimmed:
        candidate = f"{trimmed}\""
    else:
        return None

    # 统计缺失的括号数量
    missing_braces = trimmed.count('{') - trimmed.count('}')
    missing_brackets = trimmed.count('[') - trimmed.count(']')

    if missing_braces > 0:
        candidate += "}" * missing_braces
    if missing_brackets > 0:
        candidate += "]" * missing_brackets

    if has_trailing_backslash:
        candidate += "\\"

    return candidate
