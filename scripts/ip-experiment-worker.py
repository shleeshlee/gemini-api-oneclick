"""Test-container-only entrypoint: switch transport proxy without replacing the client."""
from fastapi import Depends, HTTPException
import worker

app = worker.app


def switch_transport(slot, index):
    if type(index) is not int or index not in (0, 1, 2):
        raise HTTPException(400, 'Select one of three local test routes')
    if slot.active_requests or slot.state.get('initializing') or slot.retiring:
        raise HTTPException(409, 'Test slot is busy')
    client = slot.client
    if client is None or not client._running or client.client is None:
        raise HTTPException(409, 'Test slot is not authenticated')
    transport = client.client
    if not isinstance(transport.proxies, dict):
        raise HTTPException(409, 'Unsupported transport proxy configuration')
    before = client.cookie_audit.snapshot(transport.cookies)
    url = f'http://127.0.0.1:{27890 + index}'
    transport.proxies = {'all': url}
    client.proxy = url
    after = client.cookie_audit.snapshot(transport.cookies)
    return {'index': index, 'client_id': client.cookie_audit.client_id,
            'same_client': slot.client is client and client.client is transport,
            'cookies_unchanged': before == after}


@app.post('/_experiment/egress', dependencies=[Depends(worker._verify_management_key)])
async def egress(body: dict):
    if set(worker.slots) != {20}:
        raise HTTPException(409, 'Requires an isolated account 20 worker')
    slot = worker.slots[20]
    async with slot.lock, slot.refresh_lock:
        return switch_transport(slot, body.get('index'))
