"""
敏感信息脱敏模块

对文本中的手机号、身份证号、邮箱、银行卡号、API密钥、内网IP等做正则脱敏。
用于：
1. 检索结果脱敏（search_knowledge 返回前调用，防止敏感信息进入 LLM context）
2. 答案脱敏（非流式 /chat 响应前调用，防止 LLM 泄漏敏感信息）

设计原则：
- 只做正则匹配，不调用外部服务，零延迟
- 保留部分明文以便用户识别（如 138****1234）
- 对代码块内的内容不脱敏（避免破坏代码示例中的 IP/端口）
"""

import re
from typing import List, Tuple
from loguru import logger


# ========== 脱敏规则 ==========

# 注意顺序：先匹配长模式（身份证、银行卡），再匹配短模式（手机号）
# 边界用 (?<!\d) / (?!\d) 替代 \b，避免中文紧邻数字时 \b 失效
# （Python 默认把 Unicode 字母当单词字符，"话1" 之间不算 \b 边界）
_SANITIZERS: List[Tuple[str, str]] = [
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

    # API Key 常见格式（sk-/ak-/token- 开头，长度>=20）
    (r'(?<![a-zA-Z0-9])(?:sk|ak|token|key)-[a-zA-Z0-9]{20,}(?![a-zA-Z0-9])',
     lambda m: m.group()[:8] + '***' + m.group()[-4:]),

    # AWS Access Key（20位大写字母数字）
    (r'(?<![a-zA-Z0-9])AKIA[0-9A-Z]{16}(?![a-zA-Z0-9])',
     lambda m: m.group()[:8] + '****************'),

    # 内网 IP（10.x / 172.16-31.x / 192.168.x）— 隐藏后两段
    (r'(?<!\d)(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?!\d)',
     lambda m: re.sub(r'^(\d+\.\d+)\.\d+\.\d+$', r'\1.***.***', m.group())),
]


# 代码块正则（```...``` 之间的内容不脱敏）
_CODE_BLOCK_PATTERN = re.compile(r'```[\s\S]*?```', re.MULTILINE)
_INLINE_CODE_PATTERN = re.compile(r'`[^`]+`')


def _replace_sensitive(text: str) -> str:
    """对非代码部分做脱敏替换"""
    for pattern, replacement in _SANITIZERS:
        text = re.sub(pattern, replacement, text)
    return text


def sanitize_text(text: str) -> str:
    """对文本做敏感信息脱敏

    保留代码块和行内代码中的内容不变（避免破坏代码示例）。
    对其余文本按规则脱敏：手机号、身份证、邮箱、银行卡、API Key、内网IP。

    Args:
        text: 原始文本

    Returns:
        脱敏后的文本
    """
    if not text:
        return text

    # 分离代码块和普通文本，只对普通文本脱敏
    parts = []
    last_end = 0

    # 匹配代码块和行内代码
    for match in re.finditer(r'```[\s\S]*?```|`[^`]+`', text):
        # 脱敏代码块之前的普通文本
        if match.start() > last_end:
            parts.append(_replace_sensitive(text[last_end:match.start()]))
        # 代码块原样保留
        parts.append(match.group())
        last_end = match.end()

    # 处理末尾剩余的普通文本
    if last_end < len(text):
        parts.append(_replace_sensitive(text[last_end:]))

    return ''.join(parts)


def has_sensitive_info(text: str) -> bool:
    """快速检测文本是否包含敏感信息（不做替换，仅判断）

    用于流式 done 事件后检测是否需要发送脱敏修正。

    Args:
        text: 待检测文本

    Returns:
        True 表示包含敏感信息
    """
    if not text:
        return False
    # 只检查非代码部分
    cleaned = re.sub(r'```[\s\S]*?```|`[^`]+`', '', text)
    for pattern, _ in _SANITIZERS:
        if re.search(pattern, cleaned):
            return True
    return False
