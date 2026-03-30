"""Send test requests with different header tokens via the actual GeminiClient."""
import asyncio
import os
import sys

sys.path.insert(0, "/app")

from gemini_webapi import GeminiClient, set_log_level
from gemini_webapi.constants import Model, build_model_header

set_log_level("WARNING")

# Monkey-patch to prevent auto_refresh
async def _noop(*a, **kw):
    pass
GeminiClient.start_auto_refresh = _noop

TOKENS_TO_TEST = [
    ("fbb127bbb056c959", "flash-basic"),
    ("a74ec8485b3b5ce4", "flash-unknown-1"),
    ("1bc6b5d98741cd3d", "flash-unknown-2"),
    ("9d8ca3786ebdfbea", "pro-basic"),
    ("797f3d0293f288ad", "pro-unknown-1"),
    ("5bf011840784117a", "thinking-basic"),
    ("b11155c88d2cdac8", "thinking-unknown-1"),
    ("203e6bb81620bcfe", "ht-group-1"),
    ("2525e3954d185b3c", "ht-group-2"),
    ("61530e79959ab139", "ht-group-3"),
    ("4af6c7f5da75d65d", "ht-group-4"),
    ("1640bdc9f7ef4826", "sylssb-group"),
]

PROMPT = "Reply with ONLY your exact model name and version identifier. Nothing else."


async def main():
    psid = os.environ.get("SECURE_1PSID", "")
    psidts = os.environ.get("SECURE_1PSIDTS", "")

    client = GeminiClient(psid, psidts, proxy=None)
    await client.init(timeout=60, auto_refresh=False)
    print("Client initialized.\n")

    for token, label in TOKENS_TO_TEST:
        custom = {
            "model_name": f"probe-{label}",
            "model_header": build_model_header(token, 1),
        }
        try:
            resp = await client.generate_content(PROMPT, model=custom)
            text = (resp.text or "")[:200].replace("\n", " ")
            print(f"{label:25s} [{token}] => {text}")
        except Exception as e:
            err = str(e)[:150]
            print(f"{label:25s} [{token}] => ERROR: {err}")
        await asyncio.sleep(2)


asyncio.run(main())
