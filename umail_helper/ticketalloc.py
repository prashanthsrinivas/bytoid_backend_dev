import asyncio
import pymysql
from db.rds_db import connect_to_rds

TTL_90_DAYS = 90 * 24 * 60 * 60  # 90 days in seconds


# class TicketAllocator:
#     """
#     Async-safe ticket allocator without Redis.
#     Uses a local in-memory counter, seeded from the DB once.
#     """

#     def __init__(self, client_ticket, user_id: str, latest_ticket: int = 0):
#         self.client_ticket = client_ticket
#         self.user_id = user_id
#         self._lock = asyncio.Lock()
#         self._ticket_counter = latest_ticket  # in-memory counter

#     @classmethod
#     async def create(cls, client_ticket, user_id: str):
#         """
#         Initialize the allocator, seed counter from DB.
#         """
#         # Get the base ticket number from DB (synchronous call)
#         latest = await asyncio.to_thread(client_ticket.call_ticket_number, user_id) or 0
#         #print(f"Latest ticket from DB: {latest}")

#         return cls(client_ticket, user_id, latest_ticket=latest)

#     async def next_ticket(self) -> int:
#         """
#         Atomically get the next ticket number.
#         """
#         async with self._lock:
#             self._ticket_counter += 1
#             ticket_num = self._ticket_counter
#             return ticket_num

#     async def finalize(self):
#         """
#         Persist the last ticket number back to DB.
#         """
#         async with self._lock:
#             await asyncio.to_thread(
#                 self.client_ticket.update_ticket_number,
#                 self.user_id,
#                 self._ticket_counter,
#             )
#             #print(f"Final ticket number saved to DB: {self._ticket_counter}")


import asyncio
import json
import pymysql


class TicketAllocator:
    """
    Async-safe ticket allocator without Redis.
    Uses a local in-memory counter, seeded from the DB once.
    """

    def __init__(self, user_id: str, latest_ticket: int = 0):
        self.user_id = user_id
        self._lock = asyncio.Lock()
        self._ticket_counter = latest_ticket  # in-memory counter

    @classmethod
    async def create(cls, user_id: str):
        """
        Initialize the allocator, seed counter from DB.
        """
        latest = 0
        connection = connect_to_rds()
        try:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT umail_json FROM users WHERE user_id = %s", (user_id,)
                )
                row = cursor.fetchone()
                if row and row["umail_json"]:
                    # umail_json might be already JSON or string depending on DB
                    if isinstance(row["umail_json"], str):
                        umail_json = json.loads(row["umail_json"])
                    else:
                        umail_json = row["umail_json"]

                    latest = umail_json.get("base_ticket", 0)
                else:
                    umail_json = {"base_ticket": 0}

                # If no base_ticket yet, ensure it’s in DB
                if "base_ticket" not in umail_json:
                    umail_json["base_ticket"] = 0
                    cursor.execute(
                        "UPDATE users SET umail_json=%s WHERE user_id=%s",
                        (json.dumps(umail_json), user_id),
                    )
                    connection.commit()

        finally:
            connection.close()

        #print(f"Latest ticket from DB: {latest}")
        return cls(user_id, latest_ticket=latest)

    def update_ticket(self, value):
        connection = connect_to_rds()
        try:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT umail_json FROM users WHERE user_id=%s",
                    (self.user_id,),
                )
                row = cursor.fetchone()
                if row and row["umail_json"]:
                    umail_json = (
                        json.loads(row["umail_json"])
                        if isinstance(row["umail_json"], str)
                        else row["umail_json"]
                    )
                else:
                    umail_json = {}

                umail_json["base_ticket"] = value

                cursor.execute(
                    "UPDATE users SET umail_json=%s WHERE user_id=%s",
                    (json.dumps(umail_json), self.user_id),
                )
                connection.commit()
        finally:
            connection.close()

    async def next_ticket(self) -> int:
        """
        Atomically get the next ticket number.
        """
        async with self._lock:
            self._ticket_counter += 1
            return self._ticket_counter

    async def finalize(self):
        """
        Persist the last ticket number back to DB as base_ticket.
        """
        async with self._lock:
            # update umail_json.base_ticket to current counter
            def _update():
                connection = connect_to_rds()
                try:
                    with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                        cursor.execute(
                            "SELECT umail_json FROM users WHERE user_id=%s",
                            (self.user_id,),
                        )
                        row = cursor.fetchone()
                        if row and row["umail_json"]:
                            umail_json = (
                                json.loads(row["umail_json"])
                                if isinstance(row["umail_json"], str)
                                else row["umail_json"]
                            )
                        else:
                            umail_json = {}

                        umail_json["base_ticket"] = self._ticket_counter

                        cursor.execute(
                            "UPDATE users SET umail_json=%s WHERE user_id=%s",
                            (json.dumps(umail_json), self.user_id),
                        )
                        connection.commit()
                finally:
                    connection.close()

            await asyncio.to_thread(_update)
            #print(f"Final ticket number saved to DB: {self._ticket_counter}")
