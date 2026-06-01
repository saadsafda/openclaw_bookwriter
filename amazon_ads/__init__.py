"""Amazon Ads API integration for OpenClaw Book Writer.

Modules:
    config       - region/endpoint constants and env loading
    auth         - LWA access-token minting + caching
    client       - thin HTTP wrapper that injects region host + headers
    profiles     - list/cache profileIds per marketplace
    campaigns    - create auto / keyword / category / ASIN Sponsored Products campaigns
    get_refresh_token - one-time interactive OAuth helper
"""
