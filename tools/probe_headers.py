"""Probe Gemini web page to extract model-to-header-token mappings."""
import asyncio
import re
import os
import json


async def main():
    from curl_cffi.requests import AsyncSession

    session = AsyncSession(impersonate="chrome")
    psid = os.environ.get("SECURE_1PSID", "")
    psidts = os.environ.get("SECURE_1PSIDTS", "")
    session.cookies.set("__Secure-1PSID", psid, domain=".google.com")
    session.cookies.set("__Secure-1PSIDTS", psidts, domain=".google.com")

    resp = await session.get("https://gemini.google.com/app")
    text = resp.text
    print(f"Page length={len(text)}")

    # === Step 1: Unescape the multi-level JSON escaping ===
    # The page has data like: \"[[\\"token1\\",\\"token2\\"]]\"
    t1 = text.replace('\\"', '"')  # one level
    t2 = t1.replace('\\\\"', '"')  # second level
    # Also handle unicode escapes
    t3 = t2.replace('\\\\u003d', '=').replace('\\u003d', '=')

    # === Step 2: Find family=token mappings ===
    # Pattern: thinking=hex,hex,hex
    for t in [text, t1, t2, t3]:
        mappings = re.findall(r'(\w+)(?:\\\\u003d|=)([0-9a-f]{16}(?:,[0-9a-f]{16})*)', t)
        if mappings:
            print("\nFamily=token mappings:")
            for label, tokens in mappings:
                print(f"  {label} = {tokens.split(',')}")
            break

    # === Step 3: Find all token group arrays ===
    # After full unescaping, look for ["hex1","hex2",...]
    for t in [t3, t2, t1]:
        hex_groups = re.findall(r'\["([0-9a-f]{16}(?:","[0-9a-f]{16})*)"', t)
        if hex_groups:
            print(f"\nToken groups ({len(hex_groups)}):")
            for g in hex_groups:
                print(f"  {g.split('","')}")
            break

    # === Step 4: Find model name arrays ===
    for t in [t3, t2, t1]:
        model_groups = re.findall(r'\["(gemini-[a-z0-9.-]+(?:","gemini-[a-z0-9.-]+)*)"', t)
        if model_groups:
            print(f"\nModel name groups ({len(model_groups)}):")
            for g in model_groups:
                print(f"  {g.split('","')}")
            break

    # === Step 5: Find flag->data entries with tokens ===
    for t in [t3, t2, t1]:
        # Pattern: "flagname",[data]  or  "flagname",["data"]
        flag_data = re.findall(r'"(\w+)",\["(\[\[.+?\]\])"\]', t)
        if flag_data:
            print(f"\nFlag->data with content ({len(flag_data)}):")
            for flag, data in flag_data:
                tokens = re.findall(r'[0-9a-f]{16}', data)
                models = re.findall(r'gemini-[a-z0-9.-]+', data)
                if tokens or models:
                    print(f"  {flag}:")
                    if tokens:
                        print(f"    tokens: {tokens}")
                    if models:
                        print(f"    models: {models}")
                    if '=' in data:
                        print(f"    raw: {data[:300]}")
            break

    # === Step 6: Find standalone token references with flag context ===
    # Some tokens appear as: [45xxxxxx,null,null,null,"token",null,"flagname"]
    for t in [t3, t2, t1]:
        standalone = re.findall(r'\[\d+,null,null,null,"([0-9a-f]{16})",null,"(\w+)"\]', t)
        if standalone:
            print(f"\nStandalone token->flag ({len(standalone)}):")
            for token, flag in standalone:
                print(f"  {token} -> {flag}")
            break


asyncio.run(main())
