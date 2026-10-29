import logging
import os

import discord

from audiobot.client import AudioBot
from audiobot.config import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"ตั้งค่าไม่ถูกต้อง: {exc}")
        raise SystemExit(1) from exc

    if not settings.token:
        print("ยังไม่ได้ตั้งค่า DISCORD_TOKEN")
        print("คัดลอก .env.example เป็น .env แล้วใส่ token ของบอท")
        raise SystemExit(1)

    try:
        AudioBot(settings).run(settings.token)
    except discord.errors.PrivilegedIntentsRequired:
        print(
            "บอทเชื่อมต่อไม่ได้ เพราะยังไม่ได้เปิด Message Content Intent\n"
            "ไปที่ https://discord.com/developers/applications\n"
            "เลือกแอป > Bot > Privileged Gateway Intents\n"
            "เปิด Message Content Intent แล้วกด Save Changes\n"
            "จากนั้นรัน start.bat ใหม่",
            flush=True,
        )
        os._exit(1)
    except Exception:
        logging.getLogger(__name__).exception("บอทหยุดทำงาน")
        os._exit(1)

    print("ปิดบอทแล้ว", flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
