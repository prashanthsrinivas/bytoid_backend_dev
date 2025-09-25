# from glide import (
#     GlideClusterClient,
# )
# import asyncio
# import json

# # TTL_90_DAYS = 90 * 24 * 60 * 60

# # addresses = [
# #     NodeAddress("bytoidcache-w2ofwh.serverless.cac1.cache.amazonaws.com", 6379)
# # ]
# from utils.redis_config import redis_config_glide


# async def main():
#     # ✅ Await client creation
#     # config = GlideClusterClientConfiguration(addresses=addresses, use_tls=True)
#     client = await GlideClusterClient.create(redis_config_glide)

#     # ✅ Load JSON
#     # with open("cust_helpers/messages/100805564263044911738/2025-09-04.json") as f:
#     #     all_results = json.load(f)

#     # # ✅ Set value with expiry
#     # await client.set(
#     #     "100805564263044911738",
#     #     json.dumps(all_results, default=str),
#     #     TTL_90_DAYS,
#     # )

#     # ✅ Get value
#     value = await client.get("100805564263044911738")
#     if value:
#         data = json.loads(value)
#         # print("✅ Retrieved:", type(data), len(data))
#         print(data)
#         # await client.set("100805564263044911738", "")
#         print("OK")
#     else:
#         print("⚠️ No value found")


# if __name__ == "__main__":
#     asyncio.run(main())

# In trigger.py
from utils.celery_base import addbase

if __name__ == "__main__":
    print("BROKER in producer:", addbase.app.conf.broker_url)
    print("BACKEND in producer:", addbase.app.conf.result_backend)
    res = addbase.delay(2, 3)
    new = res.get()
    print("BBB", new)
    print("Task ID:", res.id)
