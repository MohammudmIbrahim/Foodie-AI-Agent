"""HTTP layer: health, BlueBubbles webhook auth/normalization/dedupe/loops, dev simulator, sender."""

import json
import time

import httpx
import respx
from conftest import ALEX, CHAT, Harness, make_settings
from fastapi.testclient import TestClient

from app.main import build, create_app
from app.messaging.bluebubbles import BlueBubblesProvider

BB = "http://bb.test:1234"


def bb_payload(guid="m1", text="Foodie AI setup", sender="+15550000001", chat=CHAT, from_me=False, type_="new-message"):
    return {"type": type_, "data": {
        "guid": guid, "text": text, "isFromMe": from_me, "handle": {"address": sender},
        "chats": [{"guid": chat}], "dateCreated": 1793000000000,
    }}


def bb_app(tmp_path, **over):
    settings = make_settings(tmp_path, message_provider="bluebubbles", bb_password="pw", bb_base_url=BB,
                             bb_webhook_secret="s3cret", **over)
    container = build(settings)
    return container, TestClient(create_app(settings, container), client=("127.0.0.1", 50000))


def test_webhook_rejected_when_no_secret_configured(tmp_path):
    with TestClient(create_app(make_settings(tmp_path)), client=("127.0.0.1", 1)) as client:
        assert client.post("/webhooks/bluebubbles", json=bb_payload()).status_code == 403


def test_health_is_minimal(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        r = client.get("/health")
    assert r.json() == {"status": "ok"}


@respx.mock
def test_webhook_requires_token_and_loopback(tmp_path):
    respx.post(f"{BB}/api/v1/message/text").mock(return_value=httpx.Response(200, json={"status": 200, "data": {"guid": "out-1"}}))
    container, client = bb_app(tmp_path)
    with client:
        assert client.post("/webhooks/bluebubbles", json=bb_payload()).status_code == 403
        assert client.post("/webhooks/bluebubbles?token=wrong", json=bb_payload()).status_code == 403
        assert client.post("/webhooks/bluebubbles?token=s3cret", json=bb_payload()).json() == {"status": "accepted"}
    lan = TestClient(create_app(container.settings, build(container.settings)), client=("192.168.1.20", 50000))
    with lan:
        assert lan.post("/webhooks/bluebubbles?token=s3cret", json=bb_payload(guid="m9")).status_code == 403


@respx.mock
def test_webhook_end_to_end_send_and_dedupe_and_no_loop(tmp_path):
    sends = respx.post(f"{BB}/api/v1/message/text").mock(
        return_value=httpx.Response(200, json={"status": 200, "data": {"guid": "out-1"}})
    )
    container, client = bb_app(tmp_path)
    with client:
        client.post("/webhooks/bluebubbles?token=s3cret", json=bb_payload(guid="dup"))
        client.post("/webhooks/bluebubbles?token=s3cret", json=bb_payload(guid="dup"))
        # BlueBubbles echoes our own outgoing message back as isFromMe=true.
        client.post("/webhooks/bluebubbles?token=s3cret", json=bb_payload(guid="out-1", text="Hi, I'm Foodie AI", from_me=True))
        client.post("/webhooks/bluebubbles?token=s3cret", json=bb_payload(guid="x", type_="typing-indicator"))
        for _ in range(100):  # webhook returns before processing; wait for the background task
            if not container.tasks and sends.call_count:
                break
            time.sleep(0.02)
    assert sends.call_count == 1
    req = sends.calls[0].request
    body = json.loads(req.content)
    assert body["chatGuid"] == CHAT and body["method"] == "apple-script" and body["tempGuid"]
    assert body["message"].startswith("Hi, I'm Foodie AI")
    assert req.url.params["password"] == "pw"


def test_bluebubbles_normalize():
    p = BlueBubblesProvider(httpx.AsyncClient(), BB, "pw")
    m = p.normalize(bb_payload())
    assert (m.event_id, m.chat_guid, m.sender, m.is_from_me) == ("bb:m1", CHAT, "+15550000001", False)
    assert p.normalize({"type": "new-message", "data": {"guid": "g"}}) is None
    assert p.normalize({"type": "updated-message", "data": {}}) is None
    assert p.normalize("junk") is None


@respx.mock
async def test_bluebubbles_send_outcomes():
    route = respx.post(f"{BB}/api/v1/message/text")
    async with httpx.AsyncClient() as c:
        p = BlueBubblesProvider(c, BB, "pw")
        route.mock(return_value=httpx.Response(200, json={"data": {"guid": "g1"}}))
        assert (await p.send(CHAT, "hi", "k1")).provider_message_id == "g1"
        route.mock(side_effect=httpx.ConnectError("down"))
        assert (await p.send(CHAT, "hi", "k2")).retryable
        route.mock(side_effect=httpx.ReadTimeout("lost"))
        r = await p.send(CHAT, "hi", "k3")
        assert r.uncertain and not r.retryable
        route.mock(return_value=httpx.Response(500))
        assert (await p.send(CHAT, "hi", "k4")).uncertain


def test_dev_simulate_only_in_development_on_localhost(tmp_path):
    dev = make_settings(tmp_path, app_env="development")
    with TestClient(create_app(dev), client=("127.0.0.1", 1)) as client:
        r = client.post("/dev/simulate", json={"chat_guid": CHAT, "sender": ALEX, "text": "Foodie AI setup"})
        assert r.status_code == 200 and r.json()["outcome"] == "intro"
    with TestClient(create_app(dev), client=("10.0.0.5", 1)) as client:
        assert client.post("/dev/simulate", json={"chat_guid": CHAT, "sender": ALEX, "text": "x"}).status_code == 404
    with TestClient(create_app(make_settings(tmp_path / "t")), client=("127.0.0.1", 1)) as client:
        assert client.post("/dev/simulate", json={"chat_guid": CHAT, "sender": ALEX, "text": "x"}).status_code == 404


async def test_self_sent_messages_never_processed(tmp_path):
    h = Harness(tmp_path)
    assert await h.send(ALEX, "Foodie AI setup", is_from_me=True) == "self"
    assert h.texts() == []


def test_production_requires_secrets(tmp_path):
    import pytest

    from app.config import ConfigError
    with pytest.raises((ConfigError, ValueError)):
        make_settings(tmp_path, app_env="production", id_hash_pepper="")
    with pytest.raises((ConfigError, ValueError)):
        make_settings(tmp_path, app_env="production", message_provider="bluebubbles", bb_password="x", bb_webhook_secret="")
