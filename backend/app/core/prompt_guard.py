"""
Prompt Injection 检测模块

对用户输入做轻量级规则检测，识别常见的 prompt injection 攻击模式，
阻止恶意指令劫持 Agent 行为（如"忽略之前指令""你现在是 DAN"等）。

设计原则：
- 零延迟：纯正则 + 关键词匹配，不调用 LLM（避免额外成本和延迟）
- 低误报：只拦截高风险模式，正常运维提问不受影响
- 教育性拦截：被拦截时返回明确的拒绝原因，而非静默失败
- 可扩展：规则集中管理，便于后续增加新模式

为什么不用 LLM 做检测：
- 每次请求额外调用 LLM 检测成本高、延迟大
- 规则匹配对已知攻击模式准确率足够（覆盖 OWASP LLM Top 10 常见注入）
- 复杂语义攻击需结合上下文，应由 Agent 系统提示 + 工具沙箱兜底

检测层级：
1. 高风险指令劫持（必须拦截）：忽略指令/角色切换/越狱模板
2. 中风险系统提示窃取（告警+放行）：尝试读取 system prompt
3. 低风险可疑模式（仅记录）：编码绕过/分隔符注入
"""

import re
from typing import List, Tuple

from loguru import logger

# ========== 检测规则 ==========
#
# 规则按风险等级分组，每条规则：(pattern, description, risk_level)
# risk_level: "high"（拦截）/ "medium"（告警放行）/ "low"（记录）
#
# 为什么用 (?i) 内联标志：re.IGNORECASE 全局标志会同时影响中文匹配，
# 内联标志只影响当前 pattern，更精确。

# 高风险：指令劫持模式（必须拦截）
_HIGH_RISK_PATTERNS: List[Tuple[str, str]] = [
    # 直接指令覆盖：忽略/无视/跳过 [之前的/上面/前面/所有/任何] 的 指令/规则/系统提示
    # "的?" 容忍"之前的指令"和"之前指令"两种表述；"所有|任何"覆盖"跳过所有规则"
    (r'(?i)(忽略|无视|跳过|不要遵守|disregard|ignore)\s*(之前的?|上面的?|前面的?|所有|任何|prior|previous|above)?\s*(的)?\s*(指令|规则|提示|系统提示|约束|instruction|rule|prompt)',
     "尝试覆盖系统指令"),
    # 角色切换/越狱：你现在是/扮演/进入 [一个] XX 模式（DAN/developer/无限制/evil/hacker 等）
    # 中间允许少量修饰词（一个/a/an），覆盖"扮演一个 evil hacker"
    (r'(?i)(你现在是|扮演|进入|act as|you are now|pretend to be)\s*(\S{0,4}\s)?(DAN|developer|无限制|越狱|jailbreak|无约束|不受限|evil|hacker)',
     "尝试切换 Agent 角色（越狱）"),
    # 开发者模式伪造：假装有更高权限
    (r'(?i)(开发者模式|管理员模式|root\s*模式|debug\s*模式|developer\s*mode|admin\s*mode)\s*(已启用|开启|激活|enabled|activated)',
     "伪造开发者模式获取特权"),
    # 直接要求输出系统提示
    # .{0,15}? 允许中间少量修饰词（你的/me your/等），非贪婪避免跨句误报
    (r'(?i)(输出|显示|告诉我|打印|reveal|show|print).{0,15}?(系统提示|指令|规则|prompt|instruction|system\s*message|system\s*prompt|system\s*instruction)',
     "尝试窃取系统提示"),
    # 分隔符注入：试图用 ---/### 等分隔符伪造系统消息
    (r'(?i)(^|\n)\s*(---|###)\s*(系统|SYSTEM|助手|ASSISTANT|指令|INSTRUCTION)\s*[:：]',
     "分隔符注入伪造系统消息"),
]

# 中风险：可疑但可能是正常提问（告警 + 放行）
_MEDIUM_RISK_PATTERNS: List[Tuple[str, str]] = [
    # 要求"不要拒绝"：常见越狱前缀
    (r'(?i)(不要拒绝|不要说不|不要拒绝我| do not refuse | never refuse )',
     "请求不拒绝（越狱前缀）"),
    # 要求"直接执行"代码/命令（可能是正常运维，也可能是注入）
    (r'(?i)(直接执行|立即执行|不要解释|直接运行| execute directly | run without )',
     "要求直接执行（可能是正常运维）"),
]

# 低风险：编码/混淆绕过尝试（仅记录，便于审计）
_LOW_RISK_PATTERNS: List[Tuple[str, str]] = [
    # Base64 编码的长字符串（可能是 payload 伪装）
    (r'[A-Za-z0-9+/]{60,}={0,2}', "可疑 Base64 长串"),
    # 尝试用 Unicode 转义绕过
    (r'\\u[0-9a-fA-F]{4}', "Unicode 转义序列"),
]


def detect_prompt_injection(text: str) -> Tuple[bool, str, str]:
    """检测用户输入是否包含 prompt injection 攻击

    分层检测：高风险拦截、中风险告警、低风险记录。
    只有高风险模式触发时才返回 should_block=True。

    Args:
        text: 用户输入文本

    Returns:
        (should_block, reason, risk_level)
        - should_block: True 表示应拦截请求
        - reason: 拦截/告警原因（用于日志和响应）
        - risk_level: "high" / "medium" / "low" / "none"
    """
    if not text:
        return False, "", "none"

    # 1. 高风险检测：命中任一即拦截
    for pattern, desc in _HIGH_RISK_PATTERNS:
        if re.search(pattern, text):
            logger.warning(f"Prompt injection 拦截（高风险）: {desc} | 输入前80字: {text[:80]}")
            return True, f"检测到高风险 prompt injection：{desc}", "high"

    # 2. 中风险检测：告警但不拦截（正常运维可能触发）
    for pattern, desc in _MEDIUM_RISK_PATTERNS:
        if re.search(pattern, text):
            logger.info(f"Prompt injection 告警（中风险，已放行）: {desc} | 输入前80字: {text[:80]}")
            return False, f"中风险模式：{desc}", "medium"

    # 3. 低风险检测：仅记录审计
    for pattern, desc in _LOW_RISK_PATTERNS:
        if re.search(pattern, text):
            logger.debug(f"Prompt injection 记录（低风险）: {desc}")
            return False, f"低风险模式：{desc}", "low"

    return False, "", "none"


def is_safe_input(text: str) -> bool:
    """快速判断输入是否安全（不拦截）

    便捷接口，只返回布尔值，不提供原因。
    用于不需要详细原因的简单场景。

    Args:
        text: 用户输入文本

    Returns:
        True 表示输入安全（无高风险注入），False 表示应拦截
    """
    should_block, _, _ = detect_prompt_injection(text)
    return not should_block


def sanitize_injection(text: str) -> str:
    """对输入做轻量级清洗（不拦截，仅移除明显注入标记）

    用于中低风险场景：放行请求但移除可疑分隔符，降低注入成功率。
    高风险场景应直接拦截，不应调用此函数。

    Args:
        text: 原始输入

    Returns:
        清洗后的输入（移除了伪造的系统消息分隔符）
    """
    if not text:
        return text
    # 移除伪造的系统消息分隔符（---SYSTEM: / ### ASSISTANT: 等）
    cleaned = re.sub(
        r'(?i)(^|\n)\s*(---|###)\s*(系统|SYSTEM|助手|ASSISTANT|指令|INSTRUCTION)\s*[:：][^\n]*',
        '',
        text
    )
    return cleaned
