from glide import (
    GlideClusterClient,
    GlideClusterClientConfiguration,
    NodeAddress,
)
import asyncio
import json

TTL_90_DAYS = 90 * 24 * 60 * 60

addresses = [
    NodeAddress("bytoidcache-w2ofwh.serverless.cac1.cache.amazonaws.com", 6379)
]


async def main():
    # ✅ Await client creation
    config = GlideClusterClientConfiguration(addresses=addresses, use_tls=True)
    client = await GlideClusterClient.create(config)

    # ✅ Load JSON
    # with open("cust_helpers/messages/100805564263044911738/2025-09-04.json") as f:
    #     all_results = json.load(f)

    # # ✅ Set value with expiry
    # await client.set(
    #     "100805564263044911738",
    #     json.dumps(all_results, default=str),
    #     TTL_90_DAYS,
    # )

    # ✅ Get value
    value = await client.get("100805564263044911738")
    if value:
        data = json.loads(value)
        # print("✅ Retrieved:", type(data), len(data))
        print(data)
        # await client.set("100805564263044911738", "")
        print("OK")
    else:
        print("⚠️ No value found")


if __name__ == "__main__":
    asyncio.run(main())
