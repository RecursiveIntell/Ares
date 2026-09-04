export type HostRecord = {
  connection_id: string;
  host_id: string;
  label: string;
  endpoint: string;
  tls_fingerprint: string;
};

export class HostTrustError extends Error {
  readonly code = 'host_identity_changed' as const;
}

/**
 * Rebuildable host configuration projection. It deliberately has no token or
 * session-history fields; credential storage belongs to the native Keystore
 * bridge and server session truth belongs to the gateway.
 */
export class HostRegistry {
  private readonly records = new Map<string, HostRecord>();

  list(): HostRecord[] {
    return [...this.records.values()];
  }

  add(record: HostRecord): void {
    if (this.records.has(record.connection_id)) {
      throw new HostTrustError('connection is already registered; use replaceEndpoint after explicit trust');
    }
    this.records.set(record.connection_id, {...record});
  }

  replaceEndpoint(connectionId: string, record: HostRecord): void {
    const current = this.records.get(connectionId);
    if (!current) {
      throw new HostTrustError('connection is not enrolled');
    }
    if (current.host_id !== record.host_id || current.tls_fingerprint !== record.tls_fingerprint) {
      throw new HostTrustError('host identity or TLS fingerprint changed; re-enrollment required');
    }
    this.records.set(connectionId, {...record, connection_id: connectionId});
  }

  remove(connectionId: string): boolean {
    return this.records.delete(connectionId);
  }
}
