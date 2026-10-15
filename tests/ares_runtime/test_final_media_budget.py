"""C03: the final-wire counter must not admit provider-shaped media as text.

The byte counter is a qualified upper bound only for text on the supported
routes. Media is detected structurally across the provider shapes this repo
actually emits or forwards, not by one ``type`` discriminator, so a media block
in any known wire shape is refused with the accurate typed code before effect.

Scope is the *refusal boundary*. No media counter is introduced; supported
media accounting needs an owner-reviewed per-route bound (plan 5.2) and is not
claimed here. Neutral synthetic payloads only; no provider call.
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from ares_runtime.continuity.budget import BudgetError, CountMethod, final_request_upper_bound
from ares_runtime.continuity.runtime import (
    ContextDispatchError, admit_final_context_dispatch, context_dispatch_route_identity,
)
from hermes_state import SessionDB

OPAQUE = 'FINAL_PAYLOAD_OPAQUE_ACCOUNTING_UNQUALIFIED'
B64 = 'QUJD' * 1000  # neutral base64 text, 4000 chars


def _count(payload):
    return final_request_upper_bound(route_ref='r', payload=payload)


def _converse(block):
    return {'modelId': 'test-model', 'inferenceConfig': {'maxTokens': 1000},
            'messages': [{'role': 'user', 'content': [block]}]}


def _gemini(part):
    return {'model': 'test-model', 'contents': [{'role': 'user', 'parts': [part]}]}


def _chat(block):
    return {'model': 'test-model', 'max_tokens': 1000,
            'messages': [{'role': 'user', 'content': [block]}]}


MEDIA_SHAPES = {
    # Bedrock Converse: media is a keyed block with a ``source``, no ``type`` key.
    'converse_image_b64_text': _converse({'image': {'format': 'png', 'source': {'bytes': B64}}}),
    'converse_image_raw_bytes': _converse({'image': {'format': 'png', 'source': {'bytes': b'\x89PNG\r\n'}}}),
    'converse_document': _converse({'document': {'format': 'pdf', 'name': 'x', 'source': {'bytes': B64}}}),
    'converse_video': _converse({'video': {'format': 'mp4', 'source': {'bytes': B64}}}),
    # Gemini native parts.
    'gemini_inlineData': _gemini({'inlineData': {'mimeType': 'image/png', 'data': B64}}),
    'gemini_inline_data': _gemini({'inline_data': {'mime_type': 'image/png', 'data': B64}}),
    'gemini_fileData': _gemini({'fileData': {'mimeType': 'image/png', 'fileUri': 'gs://b/o'}}),
    'gemini_file_data': _gemini({'file_data': {'mime_type': 'image/png', 'file_uri': 'gs://b/o'}}),
    # Type-discriminated blocks that were missing from the closed list.
    'anthropic_document': _chat({'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': B64}}),
    'anthropic_video': _chat({'type': 'video', 'source': {'type': 'base64', 'media_type': 'video/mp4', 'data': B64}}),
    'input_video': _chat({'type': 'input_video', 'video_url': 'x'}),
    'video_url': _chat({'type': 'video_url', 'video_url': {'url': 'x'}}),
    'audio_url': _chat({'type': 'audio_url', 'audio_url': {'url': 'x'}}),
}


@pytest.mark.parametrize('name', sorted(MEDIA_SHAPES))
def test_every_known_media_wire_shape_is_refused_with_the_accurate_code(name):
    with pytest.raises(BudgetError, match=OPAQUE):
        _count(MEDIA_SHAPES[name])


@pytest.mark.parametrize('kind', ['image', 'image_url', 'input_image', 'input_file', 'file', 'audio',
                                  'input_audio', 'compaction', 'computer_screenshot'])
def test_previously_qualified_type_discriminated_media_still_refuses(kind):
    with pytest.raises(BudgetError, match=OPAQUE):
        _count(_chat({'type': kind}))


def test_media_nested_deep_inside_a_content_list_is_still_found():
    body = {'model': 'm', 'max_tokens': 1000, 'messages': [
        {'role': 'user', 'content': 'text'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'a'},
                                     {'inlineData': {'mimeType': 'image/png', 'data': B64}}]}]}
    with pytest.raises(BudgetError, match=OPAQUE):
        _count(body)


# --- negative controls: text and schema data must keep their prior counting ---

TEXT_ROUTES = {
    'chat_completions': {'model': 'm', 'max_tokens': 1000, 'messages': [{'role': 'user', 'content': 'hello'}]},
    'codex_responses': {'model': 'm', 'max_output_tokens': 1000, 'input': [{'role': 'user', 'content': 'hello'}]},
    'anthropic_messages': {'model': 'm', 'max_tokens': 1000,
                           'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}]},
    'bedrock_converse': _converse({'text': 'hello'}),
}


@pytest.mark.parametrize('route', sorted(TEXT_ROUTES))
def test_plain_text_still_qualifies_on_every_supported_route(route):
    result = _count(TEXT_ROUTES[route])
    assert result.method is CountMethod.QUALIFIED_UPPER_BOUND
    assert result.tokens > 8192


@pytest.mark.parametrize('name', ['image', 'document', 'video', 'inlineData', 'inline_data', 'fileData', 'file_data'])
def test_tool_schema_properties_with_media_looking_names_are_not_media(name):
    schema = {'type': 'object', 'properties': {name: {'type': 'object', 'properties': {'source': {'type': 'string'}}}},
              'required': [name]}
    body = {'model': 'm', 'max_tokens': 1000, 'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [{'type': 'function', 'function': {'name': 'lookup', 'description': 'd', 'parameters': schema}}]}
    assert _count(body).method is CountMethod.QUALIFIED_UPPER_BOUND


def test_converse_tool_result_json_with_media_looking_keys_is_data_not_media():
    block = {'toolResult': {'toolUseId': 't1', 'content': [
        {'json': {'image': {'source': 'a path string'}, 'inlineData': 'a note', 'document': {'source': 1}}}]}}
    assert _count(_converse(block)).method is CountMethod.QUALIFIED_UPPER_BOUND


def test_converse_tool_result_media_beside_json_data_is_still_media():
    """The ``json`` exemption is positional: a sibling image block is not data."""
    block = {'toolResult': {'toolUseId': 't1', 'content': [
        {'json': {'note': 'data'}},
        {'image': {'format': 'png', 'source': {'bytes': B64}}}]}}
    with pytest.raises(BudgetError, match=OPAQUE):
        _count(_converse(block))


def test_converse_tool_use_input_is_data_but_a_sibling_media_block_is_not():
    data_only = {'toolUse': {'toolUseId': 't', 'name': 'n', 'input': {'image': {'source': 'p'}}}}
    assert _count(_converse(data_only)).method is CountMethod.QUALIFIED_UPPER_BOUND
    body = _converse(data_only)
    body['messages'][0]['content'].append({'document': {'format': 'pdf', 'name': 'x', 'source': {'bytes': B64}}})
    with pytest.raises(BudgetError, match=OPAQUE):
        _count(body)


def test_a_data_uri_inside_plain_text_is_counted_as_text_not_exempted():
    text = 'data:image/png;base64,' + B64
    body = {'model': 'm', 'max_tokens': 1000, 'messages': [{'role': 'user', 'content': text}]}
    plain = {'model': 'm', 'max_tokens': 1000, 'messages': [{'role': 'user', 'content': 'x'}]}
    assert _count(body).tokens >= _count(plain).tokens + len(text) - 1


@pytest.mark.parametrize('root_key', ['input', 'json', 'response', 'args', 'arguments'])
def test_root_level_container_keys_cannot_launder_media_as_tool_data(root_key):
    """``input`` is the Responses item list at the root; it is never a data container there."""
    body = {'model': 'm', 'max_output_tokens': 1000,
            root_key: {'inlineData': {'mimeType': 'image/png', 'data': B64}}}
    with pytest.raises(BudgetError, match=OPAQUE):
        _count(body)


def test_tool_use_input_dict_is_data_and_still_counts_as_text():
    body = {'model': 'm', 'max_tokens': 1000, 'messages': [{'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 't', 'name': 'lookup',
         'input': {'image': {'source': 'a path string'}, 'document': {'source': 1}}}]}]}
    assert _count(body).method is CountMethod.QUALIFIED_UPPER_BOUND


def test_refused_media_yields_no_digest_to_seal():
    """A refusal returns no InputCount, so no seal can exist for media."""
    for body in MEDIA_SHAPES.values():
        with pytest.raises(BudgetError):
            _count(body)


def test_text_digest_is_stable_and_changes_with_content():
    a = _count(TEXT_ROUTES['chat_completions'])
    b = _count(json.loads(json.dumps(TEXT_ROUTES['chat_completions'])))
    c_body = json.loads(json.dumps(TEXT_ROUTES['chat_completions']))
    c_body['messages'][0]['content'] = 'hello!'
    assert a.payload_digest == b.payload_digest != _count(c_body).payload_digest
    assert a.payload_digest.startswith('sha256:') and len(a.payload_digest) == 71
    assert hashlib.sha256(b'').hexdigest() != a.payload_digest[7:]


# --- through the real admission boundary ---

@pytest.fixture
def bound_agent(tmp_path):
    db = SessionDB(db_path=tmp_path / 'context.db')
    db.create_session('s', source='cli')
    db.append_message('s', 'user', 'hi')
    assert db.try_acquire_session_turn_lease('s', 'holder', ttl_seconds=300)
    agent = SimpleNamespace(provider='test', model='test-model', api_mode='chat_completions',
        base_url='', client=object(), max_tokens=1000,
        context_compressor=SimpleNamespace(context_length=100_000),
        _session_db=db, session_id='s', _active_session_turn_lease_holder='holder')
    yield agent
    db.close()


@pytest.mark.parametrize('mode,name', [
    ('bedrock_converse', 'converse_image_b64_text'),
    ('bedrock_converse', 'converse_document'),
    ('chat_completions', 'anthropic_document'),
    ('chat_completions', 'video_url'),
])
def test_admission_refuses_media_before_any_dispatch_record(bound_agent, mode, name):
    bound_agent.api_mode = mode
    body = dict(MEDIA_SHAPES[name])
    if mode == 'bedrock_converse':
        body['modelId'] = 'test-model'
    else:
        body['model'] = 'test-model'
    snapshot = bound_agent._session_db.read_context_rebase_snapshot('s')
    before = bound_agent._session_db.read_context_rebase_snapshot('s')
    with pytest.raises(ContextDispatchError, match=OPAQUE):
        admit_final_context_dispatch(bound_agent, snapshot, body, attempt_id='media-refused',
            route_identity=context_dispatch_route_identity(bound_agent))
    assert bound_agent._session_db.read_context_rebase_snapshot('s') == before
