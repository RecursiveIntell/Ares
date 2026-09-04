import {MobileGatewayClient} from './mobileGateway';
import {MOBILE_PROTOCOL_SCHEMA_DIGEST} from '@hermes/shared/protocol';

const scope = {
  host_id: 'a'.repeat(64),
  connection_id: 'connection-1',
  profile: 'default',
  session_id: 'session-1',
  lineage_root_id: 'lineage-1',
  runtime_id: 'runtime-1',
};

const ready = {
  protocol_revision: 2 as const,
  schema_digest: MOBILE_PROTOCOL_SCHEMA_DIGEST,
  host_identity_digest: scope.host_id,
  runtime_id: scope.runtime_id,
  capabilities: {
    session_observe_v1: true,
    session_snapshot_v1: true,
    event_cursor_v1: true,
    bounded_replay_v1: true,
    session_control_lease_v1: true,
    write_idempotency_v1: true,
    mobile_surface_v1: true,
  },
};

test('disabled gateway capabilities fail closed before RPC', async () => {
  const rpc = jest.fn();
  const client = new MobileGatewayClient(rpc, {
    ...ready,
    capabilities: {...ready.capabilities, session_snapshot_v1: false},
  });
  await expect(client.snapshot(scope)).rejects.toMatchObject({
    code: 'capability_disabled',
  });
  expect(rpc).not.toHaveBeenCalled();
});

test('snapshot rejects a response retargeted to another session', async () => {
  const rpc = jest.fn().mockResolvedValue({
    result: {
      snapshot: {
        scope: {...scope, session_id: 'other-session'},
        schema_version: 1,
        revision: 0,
        status: 'idle',
        transcript_tail: [],
        transcript_cursor: null,
        pending_requests: [],
        controller: {state: 'none'},
      },
    },
  });
  const client = new MobileGatewayClient(rpc, ready);
  await expect(client.snapshot(scope)).rejects.toMatchObject({
    code: 'route_mismatch',
  });
});

test('session list validates every returned route', async () => {
  const rpc = jest.fn().mockResolvedValue({
    result: {
      runtime_id: scope.runtime_id,
      sessions: [
        {
          scope,
          title: 'Fixture',
          preview: 'hello',
          message_count: 1,
          started_at: 1,
          status: 'idle',
        },
      ],
    },
  });
  const client = new MobileGatewayClient(rpc, ready);
  await expect(client.listSessions('default', scope.host_id)).resolves.toHaveLength(1);
});
