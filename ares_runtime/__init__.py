"""Runtime image and materialization contracts for Ares releases.

``ares_runtime.local_runtime`` owns installed-runtime selection and lifecycle;
``hermes_cli.ares_candidate_store`` owns candidate custody and authorization.

Public exports load on demand so SessionDB and tool dispatch do not import
platform-specific installation/key machinery merely by importing this package.
"""

from importlib import import_module

_EXPORT_MODULES = {
    'ACTIVATION_GRANT_SCHEMA': '.contracts',
    'ActivationGrant': '.contracts',
    'AresRuntimeError': '.errors',
    'AresRuntimeLayout': '.layout',
    'MaterializedRelease': '.materializer',
    'materialize_candidate_release': '.materializer',
    'RELEASE_MANIFEST_SCHEMA': '.image',
    'RuntimeImage': '.image',
    'stage_runtime_image': '.image',
    'write_release_manifest': '.image',
    'BaselineResultV1': '.collaboration',
    'BlindWitness': '.collaboration',
    'ClosureProjector': '.collaboration',
    'ContextCompiler': '.collaboration',
    'ContextMaterializer': '.collaboration',
    'ContextPacketV1': '.collaboration',
    'ResolvedPolicyBasisV1': '.collaboration',
    'ResolvedPolicyBasisV2': '.collaboration',
    'SealedInvocationV1': '.collaboration',
    'ContractBindings': '.collaboration',
    'ContractError': '.collaboration',
    'DesktopProductionApprovalController': '.collaboration',
    'DesktopProductionApprovalEnvelope': '.collaboration',
    'DesktopProductionApprovalWitnessProvider': '.collaboration',
    'FrozenReplayCorpusV1': '.collaboration',
    'closure_ui_projection': '.collaboration',
    'DaemonPermitReceiptAdapter': '.collaboration',
    'PermitBridgeOutcome': '.collaboration',
    'PermitBridgeState': '.collaboration',
    'EvidenceItemV1': '.collaboration',
    'FindingV1': '.collaboration',
    'HandoffPacketV1': '.collaboration',
    'MissionContractV1': '.collaboration',
    'RoleContractV1': '.collaboration',
    'SpecialistDescriptorV1': '.collaboration',
    'TestRequestV1': '.collaboration',
    'dispatcher_boundary': '.collaboration',
    'evaluation_ui_projection': '.collaboration',
    'freeze_replay_corpus': '.collaboration',
    'make_artifact': '.collaboration',
    'replay_mutations': '.collaboration',
    'specialist_descriptor_ref': '.collaboration',
    'validate_specialist_descriptor_set': '.collaboration',
    'decide_specialist_nonactivation': '.specialist_routing',
    'MANAGED_MODEL_CALL_KINDS': '.governed_context',
    'GovernedContextMaterializer': '.governed_context',
    'ManagedCallKind': '.governed_context',
    'ManagedMaterialization': '.governed_context',
    'MaterializationReceiptStore': '.governed_context',
    'MaterializedContextV1': '.governed_context',
    'MemoryRequirement': '.governed_context',
    'MemoryResolutionState': '.governed_context',
    'PersistedManagedMaterialization': '.governed_context',
    'SemanticMemoryObservationV1': '.governed_context',
    'SemanticMemoryWitnessedPort': '.governed_context',
    'plan_legacy_memory_import': '.governed_context',
}

__all__ = list(_EXPORT_MODULES)


def __getattr__(name):
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
