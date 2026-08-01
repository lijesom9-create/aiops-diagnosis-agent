"""
管理员账号提权脚本（专题1）

用途：将已存在的普通用户提升为 admin 角色，用于访问管理后台。
全新部署时也可配合 .env 的 SUPER_ADMIN_USERNAME 自动初始化首个 admin；
本脚本用于已存在用户的提权/降权。

用法（在 backend/ 目录下运行）：
    python promote_admin.py <username>              # 提升为 admin
    python promote_admin.py <username> --revoke     # 撤销 admin（降为 student）

示例：
    python promote_admin.py alice
    python promote_admin.py alice --revoke

注意：脚本直接操作数据库，请确保 MongoDB 已启动且 .env 配置正确。
"""

import asyncio
import sys
from pathlib import Path

# 确保 import app.* 可用（脚本从 backend/ 目录运行）
sys.path.insert(0, str(Path(__file__).parent))

from app.core.database import db


async def main():
    if len(sys.argv) < 2:
        print("用法: python promote_admin.py <username> [--revoke]")
        print("  默认：提升为 admin")
        print("  --revoke：撤销 admin（降为 student）")
        sys.exit(1)

    username = sys.argv[1]
    revoke = "--revoke" in sys.argv[2:]

    await db.connect()

    # 脚本必须连上真实 MongoDB，否则降级到内存存储会导致操作无效
    if not getattr(db, "_use_mongo", False):
        print("错误：未连接到 MongoDB（请确保 MongoDB 已启动且 .env 中 MONGODB_URL 配置正确）")
        sys.exit(1)

    user = await db.get_user_by_username(username)
    if not user:
        print(f"错误：用户 '{username}' 不存在")
        sys.exit(1)

    user_id = user["user_id"]
    current_role = user.get("role", "student")
    new_role = "student" if revoke else "admin"

    if current_role == new_role:
        print(f"用户 '{username}' 当前角色已是 {current_role}，无需变更")
        sys.exit(0)

    ok = await db.update_user(user_id, {"role": new_role})
    if ok:
        print(f"成功：用户 '{username}' 角色 {current_role} -> {new_role}")
    else:
        print(f"失败：更新用户 '{username}' 角色失败")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
