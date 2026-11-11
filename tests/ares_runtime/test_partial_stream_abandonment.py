"""A consumer that stops reading a live stream abandons it; the provider did not fail.

Closing a generator (explicitly, or by dropping the last reference) raises
GeneratorExit inside every body wrapper suspended in that chain. That is the
consumer's decision, not a provider error. Each case reads at least one chunk so
the physical send is genuinely in flight when the consumer walks away.
"""
import asyncio
import gc
from types import SimpleNamespace

import httpx
import openai
import pytest

from ares_runtime.physical_dispatch import bind_http_client, bind_inference_client
from hermes_cli import middleware
from hermes_cli.middleware import GovernedStreamProxy
from tests.ares_runtime.test_transport_deadline import lease
from tests.ares_runtime.test_transport_stream_ownership import stream_endpoint  # noqa: F401
from tests.ares_runtime.test_bedrock_lease_stream import aws_stream, sdk as bedrock_sdk  # noqa: F401


@pytest.fixture(params=["unleased", "leased"])
def outcomes(request, monkeypatch):
    settled = []

    def governor(**_):
        ticket = SimpleNamespace(settle=lambda **kw: settled.append(kw))
        if request.param == "leased":
            ticket.transport_lease = lease()
        return ticket

    monkeypatch.setattr(middleware, "_get_middleware_callbacks", lambda _: [governor])
    return settled


def assert_abandoned(settled, count=1):
    assert len(settled) == count
    for settlement in settled:
        assert settlement["outcome"] == "abandoned"
        assert settlement["error"] is None


def test_raw_sync_stream_stopped_mid_body_is_abandoned(stream_endpoint, outcomes):
    url, bodies = stream_endpoint
    with bind_http_client(httpx.Client(trust_env=False)) as client:
        with client.stream("POST", url + "/chat/completions", json={"max_tokens": 7}) as response:
            for _ in response.iter_bytes():
                break
    assert_abandoned(outcomes)
    assert len(bodies) == 1


def test_sdk_sync_stream_dropped_after_first_chunk_is_abandoned(stream_endpoint, outcomes):
    url, bodies = stream_endpoint
    with bind_inference_client(openai.OpenAI(api_key="local", base_url=url, max_retries=0,
                                             http_client=httpx.Client(trust_env=False))) as sdk:
        stream = sdk.chat.completions.create(model="x", messages=[], max_tokens=7, stream=True)
        assert next(stream).choices[0].delta.content == "ok"
        del stream
        gc.collect()
        assert_abandoned(outcomes)
    assert len(bodies) == 1


def test_sdk_sync_stream_closed_after_first_chunk_is_abandoned(stream_endpoint, outcomes):
    url, bodies = stream_endpoint
    with bind_inference_client(openai.OpenAI(api_key="local", base_url=url, max_retries=0,
                                             http_client=httpx.Client(trust_env=False))) as sdk:
        stream = sdk.chat.completions.create(model="x", messages=[], max_tokens=7, stream=True)
        assert next(stream).choices[0].delta.content == "ok"
        stream.close()
        assert_abandoned(outcomes)
    assert len(bodies) == 1


@pytest.mark.asyncio
async def test_raw_async_stream_stopped_mid_body_is_abandoned(stream_endpoint, outcomes):
    url, bodies = stream_endpoint
    async with bind_http_client(httpx.AsyncClient(trust_env=False)) as client:
        async with client.stream("POST", url + "/chat/completions", json={"max_tokens": 7}) as response:
            chunks = response.aiter_bytes()
            await chunks.__anext__()
            await chunks.aclose()
    assert_abandoned(outcomes)
    assert len(bodies) == 1


@pytest.mark.asyncio
async def test_sdk_async_stream_closed_after_first_chunk_is_abandoned(stream_endpoint, outcomes):
    url, bodies = stream_endpoint
    async with bind_inference_client(openai.AsyncOpenAI(api_key="local", base_url=url, max_retries=0,
                                                        http_client=httpx.AsyncClient(trust_env=False))) as sdk:
        stream = await sdk.chat.completions.create(model="x", messages=[], max_tokens=7, stream=True)
        assert (await stream.__anext__()).choices[0].delta.content == "ok"
        await stream.close()
        await asyncio.sleep(0)
        assert_abandoned(outcomes)
    assert len(bodies) == 1


def test_bedrock_stream_dropped_after_first_event_is_abandoned(aws_stream, outcomes):
    url, bodies, _, _ = aws_stream
    client = bedrock_sdk(url)
    try:
        result = client.converse_stream(modelId="local", messages=[], inferenceConfig={"maxTokens": 7})
        events = iter(result["stream"])
        assert next(events) == {"messageStart": {"role": "assistant"}}
        del events, result
        gc.collect()
        assert_abandoned(outcomes)
    finally:
        client.close()
    assert len(bodies) == 1


def test_invocation_proxy_closed_by_relaying_generator_is_abandoned():
    """A UI relay generator that stops early exits the proxy's context with GeneratorExit."""
    settled = []
    ticket = SimpleNamespace(settle=lambda **kw: settled.append(kw))

    class Manager:
        def __enter__(self):
            return iter(["one", "two"])

        def __exit__(self, *args):
            return False

    def relay(proxy):
        # A for-loop (unlike yield-from) does not forward close() to the proxy,
        # so the proxy only learns of the stop through __exit__(GeneratorExit).
        with proxy as stream:
            for chunk in stream:
                yield chunk

    chunks = relay(GovernedStreamProxy(Manager(), [ticket]))
    assert next(chunks) == "one"
    chunks.close()
    assert_abandoned(settled)
