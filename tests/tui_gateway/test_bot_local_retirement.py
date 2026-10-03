"""Instance disposal through real capability refresh and memory manager.

No provider, terminal, browser, or child process is started. SQLite ownership
and the refresh path are real; counted resources implement the local lifecycle.
"""
from types import SimpleNamespace

import pytest

from agent.context_engine import ContextEngine
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from run_agent import AIAgent
from tests.tui_gateway.test_bot_capability_refresh import FakeAgent, KEY, SID, env  # noqa: F401
from tests.tui_gateway.test_bot_native_retirement import native_session
from tests.tui_gateway.test_prompt_recovery_contract import turn_env  # noqa: F401
from tui_gateway import server


class CountedProvider(MemoryProvider):
    name = "counted-local"

    def __init__(self):
        self.shutdowns = 0
        self.ends = []
        self.lifecycle = []
        self.live = False

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.session_id = session_id
        self.live = True

    def get_tool_schemas(self):
        return []

    def on_session_end(self, messages):
        self.lifecycle.append("end")
        self.ends.append(messages)

    def shutdown(self):
        self.lifecycle.append("shutdown")
        self.shutdowns += 1
        self.live = False

    def prefetch(self, query, *, session_id=""):
        assert self.live
        return "survivor memory"


class CountedEngine(ContextEngine):
    name = "counted-local"

    def __init__(self):
        self.shutdowns = 0
        self.ends = []
        self.lifecycle = []
        self.live = True

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        assert self.live
        return False

    def compress(self, messages, **kwargs):
        pytest.fail("retirement attempted compression")

    def on_session_end(self, session_id, messages):
        # A logical end could mutate canonical state for this same session ID.
        self.lifecycle.append("end")
        self.ends.append((session_id, messages))

    def shutdown(self):
        self.lifecycle.append("shutdown")
        self.shutdowns += 1
        self.live = False


class ResourceAgent(FakeAgent):
    shutdown_memory_provider = AIAgent.shutdown_memory_provider

    def retire_local_resources(self, *, preserve_agent=None):
        return AIAgent.retire_local_resources(self, preserve_agent=preserve_agent)

    def attach_resources(self):
        self.provider_resource = CountedProvider()
        self._memory_manager = MemoryManager()
        self._memory_manager.add_provider(self.provider_resource)
        self._memory_manager.initialize_all(self.session_id)
        self.context_compressor = CountedEngine()


@pytest.fixture
def resources(env, monkeypatch):
    env.old.__class__ = ResourceAgent
    env.old.attach_resources()
    task_state = SimpleNamespace(terminal=object(), browser=object(), processes=object())
    env.old._session_messages = [{"role": "assistant", "content": "prior turn"}]
    env.old.task_state = task_state

    def construct(**kwargs):
        new = ResourceAgent(**kwargs)
        new.attach_resources()
        new.task_state = task_state
        env.built.append(new)
        return new

    monkeypatch.setattr("run_agent.AIAgent", construct)
    env.resource_construct = construct

    def forbidden(*args, **kwargs):
        pytest.fail("local retirement destroyed logical session/task resources")

    with monkeypatch.context() as guard:
        guard.setattr(AIAgent, "close", forbidden)
        guard.setattr(env.target, "end_session", forbidden)
        guard.setattr(env.target, "close", forbidden)
        guard.setattr(env.launch, "close", forbidden)
        guard.setattr("run_agent.cleanup_vm", forbidden)
        guard.setattr("run_agent.cleanup_browser", forbidden)
        guard.setattr("tools.process_registry.process_registry.kill_all", forbidden)
        yield env


def assert_local_state(agent, shutdowns):
    assert agent.provider_resource.shutdowns == shutdowns
    assert agent.context_compressor.shutdowns == shutdowns
    assert agent.provider_resource.live is (shutdowns == 0)
    assert agent.context_compressor.live is (shutdowns == 0)
    assert agent.provider_resource.ends == []
    assert agent.context_compressor.ends == []


def assert_canonical_store(owner):
    current = owner.session["agent"]
    assert current._session_db is owner.target
    assert current.session_id == KEY
    assert current._owns_session_db
    assert owner.target.get_session(KEY)["ended_at"] is None
    assert owner.launch.get_session(KEY) is None
    assert owner.launch.get_messages("launch-sentinel")[0]["content"] == "untouched"
    assert current.task_state is owner.old.task_state


def test_success_and_repeated_refresh_dispose_only_superseded_instances(resources):
    owner = resources
    server._sync_bot_capabilities(SID, owner.session)
    first = owner.session["agent"]
    assert first is not owner.old
    assert_local_state(owner.old, 1)
    assert_local_state(first, 0)
    assert len(owner.old.client_closes) == 1
    owner.old.retire_local_resources()
    owner.old.shutdown_memory_provider()
    server._sync_bot_capabilities(SID, owner.session)
    assert owner.session["agent"] is first
    assert_local_state(owner.old, 1)
    assert len(owner.built) == 1

    owner.monkeypatch.setattr("tools.bot_mode_probe.capability_fingerprint", lambda home: "later-caps")
    server._sync_bot_capabilities(SID, owner.session)
    assert_local_state(first, 1)
    assert_local_state(owner.session["agent"], 0)
    assert len(first.client_closes) == 1
    assert_canonical_store(owner)


@pytest.mark.parametrize("rejection", ["runtime", "transfer", "cancel"])
def test_rejected_candidate_is_disposed_once_without_ending_predecessor(resources, rejection):
    owner = resources

    def rejected(**kwargs):
        new = owner.resource_construct(**kwargs)
        if rejection == "runtime":
            new.model = "wrong-model"
        elif rejection == "cancel":
            owner.session["_turn_cancel_requested"] = True
        return new

    owner.monkeypatch.setattr("run_agent.AIAgent", rejected)
    if rejection == "transfer":
        owner.monkeypatch.setattr(server, "_transfer_db_to_agent", lambda *args: False)
    if rejection == "cancel":
        with pytest.raises(RuntimeError, match="BOT_CAPABILITY_OWNER_CHANGED"):
            server._sync_bot_capabilities(SID, owner.session)
    else:
        server._sync_bot_capabilities(SID, owner.session)
    candidate = owner.built[0]
    assert owner.session["agent"] is owner.old
    assert_local_state(owner.old, 0)
    assert_local_state(candidate, 1)
    candidate.retire_local_resources()
    assert_local_state(candidate, 1)
    assert len(candidate.client_closes) == 1
    assert not candidate._owns_session_db
    assert owner.session["bot_caps_seen"] == "old-caps"
    assert_canonical_store(owner)


def test_native_retirement_failure_still_disposes_other_instance_resources(resources):
    owner = resources
    native, client = native_session()
    owner.old._codex_session = native

    def fail():
        raise RuntimeError("inert native retirement failure")

    owner.monkeypatch.setattr(native, "close", fail)
    server._sync_bot_capabilities(SID, owner.session)
    assert owner.session["agent"] is not owner.old
    assert owner.session["_bot_native_retirement"]["status"] == "failed"
    assert client.closes == 0
    assert_local_state(owner.old, 1)
    assert_local_state(owner.session["agent"], 0)
    assert_canonical_store(owner)


def test_constructor_failure_preserves_predecessor_and_allows_retry(resources):
    owner = resources

    def fail(**kwargs):
        raise RuntimeError("inert constructor failure before allocation")

    owner.monkeypatch.setattr("run_agent.AIAgent", fail)
    server._sync_bot_capabilities(SID, owner.session)
    assert owner.built == []
    assert owner.session["agent"] is owner.old
    assert_local_state(owner.old, 0)
    assert_canonical_store(owner)
    owner.monkeypatch.setattr("run_agent.AIAgent", owner.resource_construct)
    server._sync_bot_capabilities(SID, owner.session)
    assert_local_state(owner.old, 1)
    assert_local_state(owner.session["agent"], 0)


def test_real_session_boundary_delivers_end_before_local_shutdown(resources):
    current = resources.old
    messages = current._session_messages
    current.shutdown_memory_provider(messages)
    assert current.provider_resource.ends == [messages]
    assert current.context_compressor.ends == [(KEY, messages)]
    assert current.provider_resource.shutdowns == current.context_compressor.shutdowns == 1
    assert current.provider_resource.lifecycle == ["end", "shutdown"]
    assert current.context_compressor.lifecycle == ["end", "shutdown"]
    current.shutdown_memory_provider(messages)
    current.retire_local_resources()
    assert current.provider_resource.ends == [messages]
    assert current.context_compressor.ends == [(KEY, messages)]
    assert current.provider_resource.shutdowns == current.context_compressor.shutdowns == 1


def test_client_release_failure_does_not_skip_local_provider_or_engine_shutdown(resources):
    current = resources.old

    def fail():
        raise RuntimeError("inert client release failure")

    current.release_clients = fail
    with pytest.raises(RuntimeError, match="client release failure"):
        current.retire_local_resources()
    assert_local_state(current, 1)
    current.retire_local_resources()
    assert_local_state(current, 1)


@pytest.mark.parametrize("allocation", ["none", "clients", "providers"])
def test_actual_constructor_failure_reclaims_partial_local_resources(resources, allocation):
    owner = resources
    allocated = []
    client_retirements = []
    failure = RuntimeError("inert initializer failure")

    def initialize(agent, **kwargs):
        allocated.append(agent)
        agent.session_id = KEY
        agent._session_db = owner.target
        agent._owns_session_db = False
        if allocation in {"clients", "providers"}:
            agent.client = object()
        if allocation == "providers":
            ResourceAgent.attach_resources(agent)
        raise failure

    def retire_client(agent, client, *, reason):
        client_retirements.append(client)

    owner.monkeypatch.setattr("agent.agent_init.init_agent", initialize)
    owner.monkeypatch.setattr(AIAgent, "_retire_shared_openai_client", retire_client)
    # The actual wrapper and local disposal execute; provider initialization
    # alone is inert, with no generic provider resolution or transport spawn.
    with pytest.raises(RuntimeError) as raised:
        AIAgent(session_db=owner.target, session_id=KEY)
    assert raised.value is failure
    failed = allocated[0]
    assert failed._local_resources_retired
    assert failed._memory_provider_shutdown
    assert len(client_retirements) == (0 if allocation == "none" else 1)
    if allocation == "providers":
        assert_local_state(failed, 1)
    failed.retire_local_resources()
    assert len(client_retirements) == (0 if allocation == "none" else 1)
    assert_canonical_store(owner)


def test_constructor_cleanup_failure_preserves_original_exception(resources):
    owner = resources
    failure = ValueError("original initializer failure")

    def initialize(agent, **kwargs):
        raise failure

    def cleanup_fail(agent, *, preserve_agent=None):
        raise RuntimeError("cleanup failure")

    owner.monkeypatch.setattr("agent.agent_init.init_agent", initialize)
    owner.monkeypatch.setattr(AIAgent, "retire_local_resources", cleanup_fail)
    with pytest.raises(ValueError) as raised:
        AIAgent(session_db=owner.target, session_id=KEY)
    assert raised.value is failure
    assert_local_state(owner.old, 0)
    assert_canonical_store(owner)


@pytest.mark.parametrize("sharing", ["provider", "engine", "manager"])
@pytest.mark.parametrize("rejected", [False, True])
def test_shared_instances_remain_usable_for_the_live_survivor(resources, sharing, rejected):
    owner = resources
    old = owner.old

    def construct(**kwargs):
        new = ResourceAgent(**kwargs)
        if sharing == "manager":
            new._memory_manager = old._memory_manager
            new.provider_resource = old.provider_resource
        else:
            new.provider_resource = old.provider_resource if sharing == "provider" else CountedProvider()
            new._memory_manager = MemoryManager()
            new._memory_manager.add_provider(new.provider_resource)
            if sharing != "provider":
                new._memory_manager.initialize_all(new.session_id)
        new.context_compressor = old.context_compressor if sharing == "engine" else CountedEngine()
        new.task_state = old.task_state
        if rejected:
            new.model = "wrong-model"
        owner.built.append(new)
        return new

    owner.monkeypatch.setattr("run_agent.AIAgent", construct)
    server._sync_bot_capabilities(SID, owner.session)
    new = owner.built[0]
    live, retired = (old, new) if rejected else (new, old)
    assert owner.session["agent"] is live
    assert live.provider_resource.live
    assert live.context_compressor.live
    assert live.provider_resource.shutdowns == live.context_compressor.shutdowns == 0
    assert live.provider_resource.prefetch("next turn", session_id=KEY) == "survivor memory"
    assert live.context_compressor.should_compress() is False
    assert not live._memory_manager._shutting_down
    if sharing == "engine":
        assert retired.context_compressor is live.context_compressor
        assert retired.provider_resource.shutdowns == 1
    else:
        assert retired.provider_resource is live.provider_resource
        assert retired.context_compressor.shutdowns == 1
    if sharing == "manager":
        assert retired._memory_manager is live._memory_manager
    else:
        # A separate retired manager drains even when one provider is borrowed.
        assert retired._memory_manager._shutting_down
        assert retired._memory_manager.shutdown_drain_state["status"] == "drained"
    assert live.provider_resource.ends == []
    assert live.context_compressor.ends == []
    retired.retire_local_resources(preserve_agent=live)
    assert live.provider_resource.shutdowns == live.context_compressor.shutdowns == 0
    assert_canonical_store(owner)


@pytest.mark.parametrize("sharing", ["provider", "engine", "manager"])
@pytest.mark.parametrize("path", ["sync", "wrapper"])
def test_failed_constructor_preserves_predecessor_borrowed_instances(resources, sharing, path):
    owner = resources
    old = owner.old
    allocated, client_retirements = [], []
    failure = RuntimeError("inert borrowed-resource initializer failure")

    def initialize(agent, **kwargs):
        # The private cleanup context belongs to the wrapper, not init_agent.
        assert "_resource_preserve_agent" not in kwargs
        allocated.append(agent)
        agent.session_id = kwargs["session_id"]
        agent._session_db = kwargs["session_db"]
        agent._owns_session_db = False
        agent.client = object()
        if sharing == "manager":
            agent._memory_manager = old._memory_manager
            agent.provider_resource = old.provider_resource
        else:
            agent.provider_resource = old.provider_resource if sharing == "provider" else CountedProvider()
            agent._memory_manager = MemoryManager()
            agent._memory_manager.add_provider(agent.provider_resource)
            if sharing != "provider":
                agent._memory_manager.initialize_all(agent.session_id)
        agent.context_compressor = old.context_compressor if sharing == "engine" else CountedEngine()
        raise failure

    def retire_client(agent, client, *, reason):
        client_retirements.append(client)

    owner.monkeypatch.setattr("run_agent.AIAgent", AIAgent)
    owner.monkeypatch.setattr("agent.agent_init.init_agent", initialize)
    owner.monkeypatch.setattr(AIAgent, "_retire_shared_openai_client", retire_client)
    if path == "sync":
        # Real refresh -> real factory -> actual constructor wrapper.
        server._sync_bot_capabilities(SID, owner.session)
    else:
        with pytest.raises(RuntimeError) as raised:
            AIAgent(session_db=owner.target, session_id=KEY,
                    _resource_preserve_agent=old)
        assert raised.value is failure
    assert len(allocated) == 1
    failed = allocated[0]
    assert failed._local_resources_retired and failed._memory_provider_shutdown
    assert client_retirements and len(client_retirements) == 1
    assert owner.session["agent"] is old
    assert owner.session["bot_caps_seen"] == "old-caps"
    assert_local_state(old, 0)
    assert old.provider_resource.prefetch("next turn", session_id=KEY) == "survivor memory"
    assert old.context_compressor.should_compress() is False
    assert not old._memory_manager._shutting_down
    if sharing == "engine":
        assert failed.context_compressor is old.context_compressor
        assert failed.provider_resource.shutdowns == 1
    else:
        assert failed.provider_resource is old.provider_resource
        assert failed.context_compressor.shutdowns == 1
    if sharing == "manager":
        assert failed._memory_manager is old._memory_manager
    else:
        assert failed._memory_manager.shutdown_drain_state["status"] == "drained"
    failed.retire_local_resources(preserve_agent=old)
    assert len(client_retirements) == 1
    assert_local_state(old, 0)
    assert_canonical_store(owner)
