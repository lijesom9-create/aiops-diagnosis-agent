"""
飞书通知客户端

用飞书自建应用给指定用户发送告警卡片消息。
- tenant_access_token 缓存 + 自动刷新（2 小时过期，提前 5 分钟续期）
- 卡片消息格式：firing 红色 / resolved 绿色，含告警名、摘要、详情、时间

API 文档：
- 获取 token: https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
- 发消息:    https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id
"""
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from loguru import logger


class FeishuClient:
    """飞书自建应用通知客户端（线程安全，token 自动刷新）"""

    def __init__(self, app_id: str, app_secret: str):
        if not app_id or not app_secret:
            raise ValueError("FeishuClient 需要 app_id 和 app_secret")
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: Optional[str] = None
        self._token_expire_at: float = 0.0
        self._lock = threading.Lock()
        # 飞书 API 基地址
        self._base = "https://open.feishu.cn/open-apis"

    def _get_token(self) -> str:
        """获取 tenant_access_token，过期则自动刷新

        token 有效期 2 小时，提前 300s 续期避免临界过期。
        线程安全：多线程并发请求时只有一个刷新请求。
        """
        with self._lock:
            now = time.time()
            # 提前 5 分钟判定过期
            if self._token and now < self._token_expire_at - 300:
                return self._token
            try:
                resp = requests.post(
                    f"{self._base}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"飞书 token 获取失败: {data.get('msg')}")
                self._token = data["tenant_access_token"]
                self._token_expire_at = now + data.get("expire", 7200)
                logger.info("飞书 tenant_access_token 刷新成功")
                return self._token
            except Exception as e:
                logger.error(f"获取飞书 token 失败: {e}")
                raise

    def send_card(self, open_id: str, card: Dict[str, Any]) -> bool:
        """给指定用户发送卡片消息

        Args:
            open_id: 接收人 open_id（ou_ 开头）
            card: 飞书卡片结构（msg_type="interactive" 的 content）

        Returns:
            True 成功 / False 失败
        """
        try:
            token = self._get_token()
            import json
            resp = requests.post(
                f"{self._base}/im/v1/messages?receive_id_type=open_id",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=utf-8"},
                json={
                    "receive_id": open_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                logger.error(f"飞书发消息失败: code={data.get('code')} msg={data.get('msg')}")
                return False
            return True
        except Exception as e:
            logger.error(f"飞书发消息异常: {e}")
            return False

    @staticmethod
    def build_alert_card(alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """构建飞书告警卡片消息

        alertmanager webhook 推送格式（关键字段）:
        {
          "status": "firing",           # firing / resolved
          "alerts": [
            {
              "status": "firing",
              "labels": {"alertname": "HighCpuUsage", "severity": "warning", ...},
              "annotations": {"summary": "...", "description": "..."},
              "startsAt": "2026-08-07T03:10:00Z",
              "endsAt": "2026-08-07T03:20:00Z",
              "fingerprint": "abc123"
            }, ...
          ]
        }

        卡片设计：
        - 标题颜色按整体 status 区分（firing 红 / resolved 绿）
        - 每条告警用一个模块展示：alertname + severity + summary
        - 底部展示告警数量和时间
        """
        overall_status = alerts[0].get("overall_status", "firing") if alerts else "firing"
        # 任一 firing 则整体标红
        has_firing = any(a.get("status") == "firing" for a in alerts)
        _ = overall_status  # 预留：resolved 独立场景的颜色策略

        template = "red" if has_firing else "green"
        title_emoji = "🔥" if has_firing else "✅"
        title_text = f"{title_emoji} 告警通知" if has_firing else f"{title_emoji} 告警恢复"

        # 构建告警明细元素
        elements: List[Dict[str, Any]] = []
        for a in alerts:
            labels = a.get("labels", {})
            annotations = a.get("annotations", {})
            alertname = labels.get("alertname", "Unknown")
            severity = labels.get("severity", "-")
            instance = labels.get("instance", "-")
            summary = annotations.get("summary", "")
            description = annotations.get("description", "")
            status = a.get("status", "-")

            # 告警状态标记
            status_mark = "🔴 firing" if status == "firing" else "🟢 resolved"

            # 单条告警用 column_set 展示关键字段
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{alertname}**  |  {status_mark}\n"
                        f"severity: `{severity}`  instance: `{instance}`\n"
                        f"summary: {summary}\n"
                        f"description: {description}"
                    ),
                },
            })
            elements.append({"tag": "hr"})

        # 去掉最后一个 hr
        if elements and elements[-1].get("tag") == "hr":
            elements.pop()

        # 底部备注
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text",
                 "content": f"共 {len(alerts)} 条告警  |  推送时间: {now_str}"},
            ],
        })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title_text},
                "template": template,
            },
            "elements": elements,
        }
        return card

    @staticmethod
    def build_diagnosis_card(alert: Dict[str, Any], result: Dict[str, Any],
                             trigger: str = "initial",
                             incident_id: str = "") -> Dict[str, Any]:
        """构建自动诊断报告卡片（告警 → Agent 诊断 → 飞书推送）

        Args:
            alert: 单条告警 dict（alerts webhook 的 alerts[i]）
            result: agent.run() 返回 dict（content / diagnosis_report / tools_used 等）
            trigger: 诊断触发类型 initial/escalation/repeat（重诊卡片显示更新标记）
            incident_id: 事故编号（用于卡片标题追踪）

        卡片设计：
        - header 橙色（诊断结论是建议而非告警本身）
        - 顶部告警摘要，正文为诊断报告的结构化字段（根因/置信度/处置方案），
          优先取 diagnosis_report 结构化字段，缺失时降级为报告全文截断
        - 底部注明 AI 生成，供参考
        """
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        alertname = labels.get("alertname", "Unknown")
        severity = labels.get("severity", "-")
        instance = labels.get("instance", "-")
        summary = annotations.get("summary", "")

        trigger_mark = {
            "initial": "🩺 自动诊断报告",
            "escalation": "🔁 诊断更新（事故升级）",
            "repeat": "🔁 诊断更新（持续跟踪）",
        }.get(trigger, "🩺 自动诊断报告")
        if incident_id:
            trigger_mark = f"{trigger_mark} · {incident_id}"

        report = result.get("diagnosis_report") or {}
        content = result.get("content", "")

        # lark_md 只支持 markdown 子集（不支持 ### 标题和表格），做轻量转换
        def _to_lark_md(text: str, max_len: int = 800) -> str:
            if not text:
                return ""
            import re
            text = re.sub(r"^#{1,6}\s*", "**", text, flags=re.MULTILINE)
            # 给 **标题行 补右闭合（转换后形如 **根因分析\n）
            lines = []
            for line in text.split("\n"):
                if line.startswith("**") and not line.rstrip().endswith("**"):
                    line = line.rstrip() + "**"
                lines.append(line)
            out = "\n".join(lines).strip()
            if len(out) > max_len:
                out = out[:max_len] + "\n...(截断)"
            return out

        confidence_mark = {
            "high": "🟢 高", "medium": "🟡 中", "low": "🔴 低",
        }.get(report.get("confidence_level", "unknown"), "⚪ 未知")

        # 证据充分度（规则计算）：与 LLM 自报置信度并列展示，
        # 建议采信级别取两者中较低者（校准模型过度自信）
        suff = result.get("evidence_sufficiency") or {}
        suff_line = ""
        if suff.get("score") is not None:
            _rank = {"high": 2, "medium": 1, "low": 0}
            _mark = {"high": "🟢 高", "medium": "🟡 中", "low": "🔴 低"}
            conf_level = report.get("confidence_level", "unknown")
            suff_level = suff.get("level", "unknown")
            both = [lv for lv in (conf_level, suff_level) if lv in _rank]
            suggested = min(both, key=lambda lv: _rank[lv]) if both else "unknown"
            suff_mark = _mark.get(suff_level, "⚪ 未知")
            suggested_mark = _mark.get(suggested, "⚪ 未知")
            suff_line = (
                f"**证据充分度**: {suff.get('score', 0)}/100（{suff_mark}）\n"
                f"**建议采信级别**: {suggested_mark}（取 LLM 置信度与证据充分度中较低者）"
            )

        elements: List[Dict[str, Any]] = []

        # 告警摘要
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**触发告警**: {alertname}  |  severity: `{severity}`  instance: `{instance}`\n"
                    f"{summary}"
                ),
            },
        })
        elements.append({"tag": "hr"})

        # 结构化字段（diagnosis_report 存在时）
        if report:
            root_cause = _to_lark_md(report.get("root_cause", ""))
            solution = _to_lark_md(report.get("solution", ""))
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"**🎯 根因分析**\n{root_cause or '（未解析出根因，见全文）'}"},
            })
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"**🛠️ 处置方案**\n{solution or '（见全文）'}"},
            })
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"**置信度**: {confidence_mark}"
                                    + (f"\n{suff_line}" if suff_line else "")},
            })
        else:
            # 降级：报告全文截断
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": _to_lark_md(content, max_len=2000) or "（诊断无输出）"},
            })

        # 工具调用概览
        tools_used = result.get("tools_used") or []
        if tools_used:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"**取证工具**: {', '.join(tools_used)}"},
            })

        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text",
                 "content": f"🤖 AI 自动诊断，供参考，处置前请人工确认  |  {now_str}"},
            ],
        })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"{trigger_mark} - {alertname}"},
                "template": "orange",
            },
            "elements": elements,
        }

    @staticmethod
    def build_incident_summary_card(incident: Dict[str, Any],
                                    result: Dict[str, Any]) -> Dict[str, Any]:
        """构建事故恢复摘要卡片（resolved 闭案时生成）

        内容：事故时间线 + 根因结论 + 复盘要点；绿色 header（恢复）。
        """
        from datetime import datetime

        def _fmt(value) -> str:
            try:
                return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else str(value or "-")
            except Exception:
                return str(value or "-")

        alertnames = ", ".join(incident.get("alertnames") or ["Unknown"])
        elements: List[Dict[str, Any]] = []

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**事故**: {incident.get('incident_id', '-')}  |  "
                    f"告警: {alertnames}  |  最高级别: `{incident.get('max_severity', '-')}`\n"
                    f"开始: {_fmt(incident.get('first_seen_at'))}  →  "
                    f"恢复: {_fmt(incident.get('resolved_at'))}  |  "
                    f"诊断次数: {incident.get('diag_count', 0)}"
                ),
            },
        })
        elements.append({"tag": "hr"})

        content = result.get("content", "")
        if content:
            import re
            text = re.sub(r"^#{1,6}\s*", "**", content, flags=re.MULTILINE)
            lines = []
            for line in text.split("\n"):
                if line.startswith("**") and not line.rstrip().endswith("**"):
                    line = line.rstrip() + "**"
                lines.append(line)
            out = "\n".join(lines).strip()
            if len(out) > 2000:
                out = out[:2000] + "\n...(截断)"
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": out}})
        else:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "（摘要生成无输出）"},
            })

        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text",
                 "content": f"📝 复盘草稿已生成，请人工补充后归档知识库  |  "
                            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
            ],
        })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"✅ 事故恢复摘要 - {alertnames}"},
                "template": "green",
            },
            "elements": elements,
        }
