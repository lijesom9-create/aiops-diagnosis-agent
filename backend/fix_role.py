import asyncio

from app.core.database import db


async def main():
    await db.connect()
    mongo = db._mongo
    user = await mongo.users.find_one({"username": "xzf"})
    if user:
        print(f"username: {user['username']}")
        print(f"role: {user.get('role', 'NOT SET')}")
        await mongo.users.update_one({"username": "xzf"}, {"$set": {"role": "admin"}})
        print("Role updated to admin")
    else:
        print("User not found")

asyncio.run(main())
