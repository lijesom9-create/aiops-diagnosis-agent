"""
飞书通知客户端

用飞书自建应用给指定用户发送告警卡片消息。
- tenant_access_token 缓存 + 自动刷新（2 小时过期，提前 5 分钟续期）
- 卡片消息格式：firing 红色 / resolved 绿色，含告警名、摘要、详情、时间

API 文档：
- 获取 token: https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
- 发消息:    https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id
"""
import time
import threading
from typing import Optional, Dict, Any, List

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
        is_resolved = overall_status == "resolved" and not has_firing

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
