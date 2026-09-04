import {
  GatewayReadyV2,
  MOBILE_PROTOCOL_REVISION,
  MOBILE_PROTOCOL_SCHEMA_DIGEST,
  MobileSessionListItemV1,
  SessionOwnerScope,
  SessionSnapshotV1,
} from '@hermes/shared/protocol';

export type MobileRpc = (method: string, params: Record<string, unknown>) => Promise<unknown>;

export type MobileGatewayErrorCode =
  | 'protocol_mismatch'
  | 'capability_disabled'
  | 'route_mismatch'
  | 'snapshot_required'
  | 'rpc_error';

export class MobileGatewayError extends Error {
  readonly code: MobileGatewayErrorCode;
  readonly data: Record<string, unknown> | undefined;

  constructor(code: MobileGatewayErrorCode, message: string, data?: Record<string, unknown>) {
    super(message);
    this.name = 'MobileGatewayError';
    this.code = code;
    this.data = data;
  }
}

export type MobileGatewayScope = SessionOwnerScope;

type RpcEnvelope = {
  error?: {code?: number; message?: string; data?: Record<string, unknown>};
  result?: Record<string, unknown>;
};

function asEnvelope(value: unknown): RpcEnvelope {
  if (!value || typeof value !== 'object') {
    throw new MobileGatewayError('rpc_error', 'gateway returned a malformed response');
  }
  return value as RpcEnvelope;
}

function requireScope(actual: unknown, expected: MobileGatewayScope): void {
  if (!actual || typeof actual !== 'object') {
    throw new MobileGatewayError('route_mismatch', 'gateway response omitted its owner scope');
  }
  const received = actual as Record<string, unknown>;
  for (const key of ['host_id', 'connection_id', 'profile', 'session_id', 'lineage_root_id', 'runtime_id']) {
    if (received[key] !== expected[key as keyof MobileGatewayScope]) {
      throw new MobileGatewayError('route_mismatch', `gateway response changed ${key}`);
    }
  }
}

export class MobileGatewayClient {
  private readonly capabilities: GatewayReadyV2['capabilities'];

  constructor(
    private readonly rpc: MobileRpc,
    private readonly ready: GatewayReadyV2,
  ) {
    if (ready.protocol_revision !== MOBILE_PROTOCOL_REVISION) {
      throw new MobileGatewayError('protocol_mismatch', 'gateway protocol revision is not supported');
    }
    if (ready.schema_digest !== MOBILE_PROTOCOL_SCHEMA_DIGEST) {
      throw new MobileGatewayError('protocol_mismatch', 'gateway schema digest is not supported');
    }
    this.capabilities = ready.capabilities;
  }

  private requireCapability(name: keyof GatewayReadyV2['capabilities']): void {
    if (this.capabilities[name] !== true) {
      throw new MobileGatewayError('capability_disabled', `gateway capability is disabled: ${name}`, {
        capability: name,
      });
    }
  }

  private async call(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const envelope = asEnvelope(await this.rpc(method, params));
    if (envelope.error) {
      throw new MobileGatewayError(
        'rpc_error',
        envelope.error.message || `gateway rejected ${method}`,
        envelope.error.data,
      );
    }
    if (!envelope.result || typeof envelope.result !== 'object') {
      throw new MobileGatewayError('rpc_error', `gateway response for ${method} omitted result`);
    }
    return envelope.result;
  }

  async listSessions(profile: string, hostId: string, limit = 200): Promise<MobileSessionListItemV1[]> {
    this.requireCapability('mobile_surface_v1');
    const result = await this.call('session.mobile.list', {
      host_id: hostId,
      profile,
      limit,
    });
    if (result.runtime_id !== this.ready.runtime_id) {
      throw new MobileGatewayError('route_mismatch', 'session list changed runtime identity');
    }
    const sessions = result.sessions;
    if (!Array.isArray(sessions)) {
      throw new MobileGatewayError('rpc_error', 'session list response omitted sessions');
    }
    for (const item of sessions) {
      if (!item || typeof item !== 'object') {
        throw new MobileGatewayError('rpc_error', 'session list contained a malformed item');
      }
      const scope = (item as MobileSessionListItemV1).scope;
      if (
        scope.host_id !== hostId ||
        scope.profile !== profile ||
        scope.runtime_id !== this.ready.runtime_id
      ) {
        throw new MobileGatewayError('route_mismatch', 'session list contained a foreign route');
      }
    }
    return sessions as MobileSessionListItemV1[];
  }

  async snapshot(scope: MobileGatewayScope): Promise<SessionSnapshotV1> {
    this.requireCapability('session_snapshot_v1');
    const result = await this.call('session.snapshot', {...scope});
    const snapshot = result.snapshot as SessionSnapshotV1 | undefined;
    if (!snapshot) {
      throw new MobileGatewayError('rpc_error', 'snapshot response omitted snapshot');
    }
    requireScope(snapshot.scope, scope);
    return snapshot;
  }

  async observe(scope: MobileGatewayScope, lastSeenRevision: number): Promise<Record<string, unknown>> {
    this.requireCapability('session_observe_v1');
    const result = await this.call('session.observe', {
      ...scope,
      last_seen_revision: lastSeenRevision,
    });
    if (result.snapshot_required === true) {
      throw new MobileGatewayError('snapshot_required', 'gateway requires a fresh snapshot', {
        runtime_id: result.runtime_id,
        current_revision: result.current_revision,
      });
    }
    if (result.runtime_id !== scope.runtime_id) {
      throw new MobileGatewayError('route_mismatch', 'observe response changed runtime identity');
    }
    return result;
  }
}
