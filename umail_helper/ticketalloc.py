# import asyncio
# from glide import GlideClusterClient, GlideClusterClientConfiguration

# TTL_90_DAYS = 90 * 24 * 60 * 60  # 90 days in seconds


# class TicketAllocator:
#     """
#     Async-safe, multi-process ticket allocator.
#     - Seeds from UmailLanceClient (DB) once.
#     - Uses Redis INCR for cross-process uniqueness.
#     """

#     def __init__(self, redis_client, client_ticket, user_id: str):
#         self.redis = redis_client
#         self.client_ticket = client_ticket
#         self.user_id = user_id
#         self._lock = asyncio.Lock()
#         self._redis_key = f"ticket:{user_id}"

#     @classmethod
#     async def create(cls, addresses, client_ticket, user_id: str):
#         """
#         Create an allocator, seeding Redis with DB's ticket number if missing.
#         """
#         config = GlideClusterClientConfiguration(addresses=addresses, use_tls=True)
#         redis_client = await GlideClusterClient.create(config)

#         # 1️⃣ get the base ticket number from DB
#         latest = client_ticket.call_ticket_number(user_id) or 0
#         print("latest", latest)

#         # 2️⃣ seed Redis if key does not yet exist
#         key = f"ticket:{user_id}"
#         exists = await redis_client.get(key)
#         if exists is None:
#             # Set to 'latest' (DB’s number). The first INCR will give latest+1.
#             await redis_client.set(f"ticket:{user_id}", str(latest), TTL_90_DAYS)

#         return cls(redis_client, client_ticket, user_id)

#     async def next_ticket(self) -> int:
#         """
#         Atomically get the next ticket number across all processes.
#         """
#         async with self._lock:
#             ticket_num = await self.redis.incr(self._redis_key)  # INCR is atomic
#             print("val", ticket_num)
#             await self.redis.expire(self._redis_key, TTL_90_DAYS)
#             return ticket_num

#     async def finalize(self):
#         """
#         Persist the last ticket number back to DB.
#         """
#         async with self._lock:
#             current = await self.redis.get(self._redis_key)
#             if current is not None:
#                 current = int(current)
#                 await asyncio.to_thread(
#                     self.client_ticket.update_ticket_number,
#                     self.user_id,
#                     int(current),
#                 )


import asyncio

TTL_90_DAYS = 90 * 24 * 60 * 60  # 90 days in seconds


class TicketAllocator:
    """
    Async-safe ticket allocator without Redis.
    Uses a local in-memory counter, seeded from the DB once.
    """

    def __init__(self, client_ticket, user_id: str, latest_ticket: int = 0):
        self.client_ticket = client_ticket
        self.user_id = user_id
        self._lock = asyncio.Lock()
        self._ticket_counter = latest_ticket  # in-memory counter

    @classmethod
    async def create(cls, client_ticket, user_id: str):
        """
        Initialize the allocator, seed counter from DB.
        """
        # Get the base ticket number from DB (synchronous call)
        latest = await asyncio.to_thread(client_ticket.call_ticket_number, user_id) or 0
        print(f"Latest ticket from DB: {latest}")

        return cls(client_ticket, user_id, latest_ticket=latest)

    async def next_ticket(self) -> int:
        """
        Atomically get the next ticket number.
        """
        async with self._lock:
            self._ticket_counter += 1
            ticket_num = self._ticket_counter
            return ticket_num

    async def finalize(self):
        """
        Persist the last ticket number back to DB.
        """
        async with self._lock:
            await asyncio.to_thread(
                self.client_ticket.update_ticket_number,
                self.user_id,
                self._ticket_counter,
            )
            print(f"Final ticket number saved to DB: {self._ticket_counter}")
