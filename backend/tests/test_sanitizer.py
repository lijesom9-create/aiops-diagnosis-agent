"""
敏感信息脱敏模块单元测试

测试覆盖：
- 手机号脱敏
- 身份证号脱敏
- 邮箱脱敏
- 银行卡号脱敏
- API Key 脱敏
- 内网 IP 脱敏
- 代码块保护（不脱敏）
- 混合文本
- has_sensitive_info 检测函数
- 边界情况（空字符串、无敏感信息）
"""

from app.core.config import settings
from app.core.sanitizer import has_sensitive_info, sanitize_text


class TestPhoneSanitization:
    """手机号脱敏"""

    def test_standard_phone(self):
        assert sanitize_text("电话: 13812345678") == "电话: 138****5678"

    def test_multiple_phones(self):
        result = sanitize_text("13812345678 和 15987654321")
        assert "138****5678" in result
        assert "159****4321" in result

    def test_phone_in_sentence(self):
        text = "请联系13812345678获取帮助"
        assert "138****5678" in sanitize_text(text)

    def test_non_phone_number_not_affected(self):
        """11位但非手机号的数字不应被脱敏"""
        assert sanitize_text("订单号: 12345678901") == "订单号: 12345678901"


class TestIDCardSanitization:
    """身份证号脱敏"""

    def test_standard_id_card(self):
        text = "身份证: 110101199001011234"
        result = sanitize_text(text)
        assert "110101" in result  # 前6位保留
        assert "1234" in result     # 后4位保留
        assert "19900101" not in result  # 中间部分脱敏

    def test_id_card_with_x(self):
        text = "身份证: 11010119900101123X"
        result = sanitize_text(text)
        assert "110101" in result
        assert "123X" in result


class TestEmailSanitization:
    """邮箱脱敏"""

    def test_standard_email(self):
        result = sanitize_text("邮箱: test@example.com")
        assert "t***@example.com" == result.replace("邮箱: ", "")

    def test_long_email(self):
        result = sanitize_text("联系: john.doe@company.org")
        assert "***@" in result
        assert "company.org" in result


class TestAPIKeySanitization:
    """API Key 脱敏"""

    def test_sk_prefix_key(self):
        text = "API Key: sk-1234567890abcdefghijklmnopqrst"
        result = sanitize_text(text)
        assert "sk-12345" in result  # 前8位保留
        assert "qrst" in result      # 后4位保留
        assert "1234567890abcdefghij" not in result  # 中间脱敏


class TestIPSanitization:
    """内网 IP 脱敏"""

    def test_192_168(self):
        result = sanitize_text("服务器: 192.168.1.100")
        assert "192.168" in result     # 前两段保留
        assert "1.100" not in result   # 后两段脱敏
        assert "***" in result

    def test_10_x(self):
        result = sanitize_text("内网: 10.0.0.5")
        assert "10.0" in result
        assert "***" in result

    def test_172_16(self):
        result = sanitize_text("网关: 172.16.3.20")
        assert "172.16" in result
        assert "***" in result

    def test_public_ip_not_affected(self):
        """公网 IP 不应被脱敏"""
        assert sanitize_text("公网: 8.8.8.8") == "公网: 8.8.8.8"


class TestCodeBlockProtection:
    """代码块保护"""

    def test_inline_code_protected(self):
        """行内代码中的 IP 不脱敏"""
        text = "配置 `192.168.1.1` 作为网关"
        result = sanitize_text(text)
        assert "192.168.1.1" in result  # 代码块内不脱敏

    def test_code_block_protected(self):
        """代码块中的 IP 不脱敏"""
        text = "配置如下:\n```\nserver = 192.168.1.100\nport = 8080\n```"
        result = sanitize_text(text)
        assert "192.168.1.100" in result  # 代码块内不脱敏

    def test_mixed_code_and_text(self):
        """混合代码和普通文本"""
        text = "服务器IP `10.0.0.1`，另一台 10.0.0.2"
        result = sanitize_text(text)
        assert "10.0.0.1" in result   # 代码块内保留
        assert "10.0.0.2" not in result  # 普通文本脱敏


class TestHasSensitiveInfo:
    """敏感信息检测函数"""

    def test_detect_phone(self):
        assert has_sensitive_info("电话: 13812345678") is True

    def test_detect_ip(self):
        assert has_sensitive_info("服务器: 192.168.1.1") is True

    def test_no_sensitive_info(self):
        assert has_sensitive_info("这是一段普通文本") is False

    def test_empty_string(self):
        assert has_sensitive_info("") is False

    def test_sensitive_in_code_not_detected(self):
        """代码块内的敏感信息不应被检测到"""
        text = "配置 `192.168.1.1`"
        assert has_sensitive_info(text) is False


class TestEdgeCases:
    """边界情况"""

    def test_empty_string(self):
        assert sanitize_text("") == ""

    def test_no_sensitive_info(self):
        text = "这是一段普通文本，没有敏感信息"
        assert sanitize_text(text) == text

    def test_mixed_all_types(self):
        """混合多种敏感信息"""
        text = "电话13812345678，邮箱test@ex.com，IP 192.168.1.1"
        result = sanitize_text(text)
        assert "138****5678" in result
        assert "***@" in result
        assert "192.168" in result
        assert "1.1" not in result.split("IP ")[1] if "IP " in result else True


class TestCodeBlockBypassFix:
    """C2: 代码块脱敏旁路修复

    背景：原实现无条件豁免代码块，LLM 把敏感数据放进 ``` 代码块即可绕过脱敏。
    strict 模式（默认/生产）：代码块内高置信凭据（API key/AWS key/私钥）仍脱敏，
    普通文本模式（手机号/身份证/内网IP）豁免（代码块中可能是测试数据）。
    loose 模式：代码块完全豁免（向后兼容，开发调试用）。
    """

    def setup_method(self):
        """每个测试前恢复默认 strict 模式，避免互相污染"""
        self._prev = settings.SANITIZER_SANITIZE_CODE
        settings.SANITIZER_SANITIZE_CODE = "strict"

    def teardown_method(self):
        settings.SANITIZER_SANITIZE_CODE = self._prev

    def test_api_key_in_code_block_sanitized_strict(self):
        """strict：代码块内 API key 仍被脱敏（堵住旁路）"""
        text = "配置如下:\n```\nAPI_KEY=sk-1234567890abcdefghijklmnopqrst\n```"
        result = sanitize_text(text)
        assert "sk-12345" in result   # 前8位保留
        assert "qrst" in result       # 后4位保留
        assert "1234567890abcdefghij" not in result  # 中间脱敏

    def test_aws_key_in_code_block_sanitized_strict(self):
        """strict：代码块内 AWS Access Key 仍被脱敏"""
        text = "```\naws_access_key_id = AKIAIOSFODNN7EXAMPLE\n```"
        result = sanitize_text(text)
        assert "AKIAIOSF" in result   # 前8位保留
        assert "EXAMPLE" not in result

    def test_private_key_in_code_block_sanitized_strict(self):
        """strict：代码块内 PEM 私钥块仍被脱敏"""
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        text = f"```\n{pem}\n```"
        result = sanitize_text(text)
        assert "REDACTED PRIVATE KEY" in result
        assert "MIIEpAIBAAKCAQEA" not in result

    def test_inline_code_api_key_sanitized_strict(self):
        """strict：行内代码中的 API key 也被脱敏（高置信凭据无例外）"""
        text = "密钥 `sk-1234567890abcdefghijklmnopqrst` 已泄露"
        result = sanitize_text(text)
        assert "sk-12345" in result
        assert "1234567890abcdefghij" not in result

    def test_text_pattern_in_code_block_exempt_strict(self):
        """strict：代码块内普通文本模式（手机号/内网IP）豁免（可能是测试数据）"""
        text = "```\nphone = 13812345678\nhost = 192.168.1.100\n```"
        result = sanitize_text(text)
        assert "13812345678" in result   # 手机号保留
        assert "192.168.1.100" in result  # 内网IP保留

    def test_api_key_in_code_block_preserved_loose(self):
        """loose：代码块完全豁免（向后兼容，开发调试用）"""
        settings.SANITIZER_SANITIZE_CODE = "loose"
        text = "```\nAPI_KEY=sk-1234567890abcdefghijklmnopqrst\n```"
        result = sanitize_text(text)
        assert "sk-1234567890abcdefghijklmnopqrst" in result  # 原样保留

    def test_text_pattern_in_code_block_preserved_loose(self):
        """loose：代码块内 IP 也完全保留"""
        settings.SANITIZER_SANITIZE_CODE = "loose"
        text = "```\nserver = 192.168.1.100\n```"
        result = sanitize_text(text)
        assert "192.168.1.100" in result

    def test_has_sensitive_info_detects_api_key_in_code_block_strict(self):
        """strict：has_sensitive_info 检测到代码块内 API key"""
        text = "```\nkey = sk-1234567890abcdefghijklmnopqrst\n```"
        assert has_sensitive_info(text) is True

    def test_has_sensitive_info_skips_text_pattern_in_code_block_strict(self):
        """strict：代码块内的手机号不算敏感（与 sanitize 口径一致）"""
        text = "```\nphone = 13812345678\n```"
        assert has_sensitive_info(text) is False

    def test_has_sensitive_info_skips_code_block_loose(self):
        """loose：代码块内任何内容都不算敏感"""
        settings.SANITIZER_SANITIZE_CODE = "loose"
        text = "```\nkey = sk-1234567890abcdefghijklmnopqrst\n```"
        assert has_sensitive_info(text) is False

    def test_non_code_api_key_still_sanitized_both_modes(self):
        """两种模式下，非代码文本中的 API key 都脱敏"""
        text = "密钥 sk-1234567890abcdefghijklmnopqrst 已泄露"
        for mode in ("strict", "loose"):
            settings.SANITIZER_SANITIZE_CODE = mode
            result = sanitize_text(text)
            assert "sk-12345" in result, f"{mode} 模式下非代码 API key 应脱敏"
            assert "1234567890abcdefghij" not in result
