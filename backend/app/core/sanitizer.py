"""
敏感信息脱敏模块

对文本中的手机号、身份证号、邮箱、银行卡号、API密钥、内网IP等做正则脱敏。
用于：
1. 检索结果脱敏（search_knowledge 返回前调用，防止敏感信息进入 LLM context）
2. 答案脱敏（非流式 /chat 响应前调用，防止 LLM 泄漏敏感信息）

设计原则：
- 只做正则匹配，不调用外部服务，零延迟
- 保留部分明文以便用户识别（如 138****1234）
- 代码块脱敏策略由 SANITIZER_SANITIZE_CODE 控制（C2 修复）：
  - strict（默认/生产）：代码块内的高置信凭据（API key/AWS key/私钥）仍脱敏，
    普通文本模式（手机号/身份证/邮箱等）豁免（代码块中可能是测试数据）
  - loose（开发调试）：代码块完全豁免（保留代码示例原样，便于排查）

  背景：原实现对 ``` 代码块与行内代码原样保留，LLM 输出只要把敏感数据放进
  代码块即绕过脱敏。strict 模式堵住高置信凭据的旁路，同时不误伤代码示例中的
  测试手机号/内网 IP。
"""

import re
from typing import List, Tuple

from .config import settings

# ========== 脱敏规则 ==========

# 注意顺序：先匹配长模式（身份证、银行卡），再匹配短模式（手机号）
# 边界用 (?<!\d) / (?!\d) 替代 \b，避免中文紧邻数字时 \b 失效
# （Python 默认把 Unicode 字母当单词字符，"话1" 之间不算 \b 边界）

# 高置信凭据模式：任何位置（含代码块）都必须脱敏
# 这些模式不会作为合法测试数据出现在代码示例中，且泄漏代价极高
_HIGH_CONFIDENCE_SANITIZERS: List[Tuple[str, str]] = [
    # PEM 私钥块（含 RSA/EC/OpenSSH/PGP/加密私钥等）
    (r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----',
     lambda m: '-----REDACTED PRIVATE KEY-----'),

    # API Key 常见格式（sk-/ak-/token-/key- 开头，长度>=20）
    (r'(?<![a-zA-Z0-9])(?:sk|ak|token|key)-[a-zA-Z0-9]{20,}(?![a-zA-Z0-9])',
     lambda m: m.group()[:8] + '***' + m.group()[-4:]),

    # AWS Access Key（20位大写字母数字）
    (r'(?<![a-zA-Z0-9])AKIA[0-9A-Z]{16}(?![a-zA-Z0-9])',
     lambda m: m.group()[:8] + '****************'),
]

# 普通文本模式：仅在非代码文本中脱敏；代码块中可能是测试数据，按策略豁免
_TEXT_SANITIZERS: List[Tuple[str, str]] = [
    # 身份证号（18位，最后一位可能是X）
    (r'(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)',
     lambda m: m.group()[:6] + '********' + m.group()[-4:]),

    # 银行卡号（16-19位连续数字）
    (r'(?<!\d)6[0-9]{15,18}(?!\d)',
     lambda m: m.group()[:4] + ' **** **** ' + m.group()[-4:]),

    # 手机号（11位，1开头）
    (r'(?<!\d)1[3-9]\d{9}(?!\d)',
     lambda m: m.group()[:3] + '****' + m.group()[-4:]),

    # 邮箱
    (r'(?<![a-zA-Z0-9])([a-zA-Z0-9._%+-])([a-zA-Z0-9._%+-]*?)@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})(?![a-zA-Z0-9])',
     lambda m: m.group(1) + '***@' + m.group(3)),

    # 内网 IP（10.x / 172.16-31.x / 192.168.x）— 隐藏后两段
    (r'(?<!\d)(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?!\d)',
     lambda m: re.sub(r'^(\d+\.\d+)\.\d+\.\d+$', r'\1.***.***', m.group())),
]

# 兼容旧引用（合并列表，顺序保持高置信在前以优先匹配凭据）
_SANITIZERS: List[Tuple[str, str]] = _HIGH_CONFIDENCE_SANITIZERS + _TEXT_SANITIZERS


# 代码块正则（```...``` 之间的内容）
_CODE_BLOCK_PATTERN = re.compile(r'```[\s\S]*?```', re.MULTILINE)
_INLINE_CODE_PATTERN = re.compile(r'`[^`]+`')
# 统一匹配代码块 + 行内代码
_CODE_SPAN_PATTERN = re.compile(r'```[\s\S]*?```|`[^`]+`')


def _apply_rules(text: str, rules: List[Tuple[str, str]]) -> str:
    """对文本按给定规则列表做脱敏替换"""
    for pattern, replacement in rules:
        text = re.sub(pattern, replacement, text)
    return text


def _replace_sensitive(text: str) -> str:
    """对非代码部分做全量脱敏（高置信凭据 + 普通文本模式）"""
    return _apply_rules(text, _HIGH_CONFIDENCE_SANITIZERS + _TEXT_SANITIZERS)


def _replace_high_confidence(text: str) -> str:
    """仅对高置信凭据脱敏（用于代码块内，普通文本模式豁免）"""
    return _apply_rules(text, _HIGH_CONFIDENCE_SANITIZERS)


def _is_strict_code_mode() -> bool:
    """代码块脱敏模式：strict（默认）对代码块内高置信凭据仍脱敏；loose 完全豁免"""
    return (settings.SANITIZER_SANITIZE_CODE or "strict").strip().lower() != "loose"


def sanitize_text(text: str) -> str:
    """对文本做敏感信息脱敏

    代码块脱敏策略由 SANITIZER_SANITIZE_CODE 控制（C2 修复）：
    - strict（默认）：代码块内的高置信凭据（API key/AWS key/私钥）仍脱敏，
      普通文本模式（手机号/身份证等）豁免；非代码文本全量脱敏
    - loose：代码块和行内代码完全豁免（保留代码示例原样）

    Args:
        text: 原始文本

    Returns:
        脱敏后的文本
    """
    if not text:
        return text

    strict = _is_strict_code_mode()

    # 分离代码块和普通文本
    parts = []
    last_end = 0

    for match in _CODE_SPAN_PATTERN.finditer(text):
        # 代码块之前的普通文本：全量脱敏
        if match.start() > last_end:
            parts.append(_replace_sensitive(text[last_end:match.start()]))

        code_span = match.group()
        if strict:
            # strict：代码块内仅对高置信凭据脱敏，普通文本模式豁免
            parts.append(_replace_high_confidence(code_span))
        else:
            # loose：代码块原样保留
            parts.append(code_span)
        last_end = match.end()

    # 处理末尾剩余的普通文本
    if last_end < len(text):
        parts.append(_replace_sensitive(text[last_end:]))

    return ''.join(parts)


def has_sensitive_info(text: str) -> bool:
    """快速检测文本是否包含敏感信息（不做替换，仅判断）

    用于流式 done 事件后检测是否需要发送脱敏修正。
    判定口径与 sanitize_text 一致：strict 模式下代码块内高置信凭据也算敏感，
    loose 模式下代码块内任何内容都不算敏感。

    Args:
        text: 待检测文本

    Returns:
        True 表示包含敏感信息
    """
    if not text:
        return False

    strict = _is_strict_code_mode()
    # 高置信凭据检查范围：strict=全文（含代码块），loose=仅非代码
    high_conf_text = text if strict else _CODE_SPAN_PATTERN.sub('', text)
    for pattern, _ in _HIGH_CONFIDENCE_SANITIZERS:
        if re.search(pattern, high_conf_text):
            return True

    # 普通文本模式：仅非代码部分（两种模式一致，代码块中可能是测试数据）
    cleaned = _CODE_SPAN_PATTERN.sub('', text)
    for pattern, _ in _TEXT_SANITIZERS:
        if re.search(pattern, cleaned):
            return True
    return False
