import {HostRegistry, HostTrustError} from './hostRegistry';

const host = {
  connection_id: 'connection-1',
  host_id: 'a'.repeat(64),
  label: 'Workstation',
  endpoint: 'wss://host.example/api/ws',
  tls_fingerprint: 'cert-a',
};

test('host registry rejects silent identity changes', () => {
  const registry = new HostRegistry();
  registry.add(host);
  expect(() => registry.add(host)).toThrow(HostTrustError);
  expect(() => registry.replaceEndpoint(host.connection_id, {...host, host_id: 'b'.repeat(64)})).toThrow(
    HostTrustError,
  );
  expect(() => registry.replaceEndpoint(host.connection_id, {...host, endpoint: 'wss://new.example/api/ws'})).not.toThrow();
});
