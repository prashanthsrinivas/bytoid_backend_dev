from glide import (
    GlideClusterClient,
    GlideClusterClientConfiguration,
    NodeAddress,
)

TTL_90_DAYS = 90 * 24 * 60 * 60

addresses = [
    NodeAddress("bytoidcache-w2ofwh.serverless.cac1.cache.amazonaws.com", 6379)
]

redis_config_glide = GlideClusterClientConfiguration(addresses=addresses, use_tls=True)
