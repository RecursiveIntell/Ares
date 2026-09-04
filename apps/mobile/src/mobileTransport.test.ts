import {JsonRpcGatewayClient} from '@hermes/shared/json-rpc-gateway';
import {createMobileGatewayTransport, mobileSocketFactory} from './mobileTransport';

type Listener = (event: {data?: string}) => void;

class FakeSocket {
  static instances: FakeSocket[] = [];

  readyState = 0;
  sent: string[] = [];
  constructorArgs: unknown[];
  private readonly listeners = new Map<string, Set<Listener>>();

  constructor(...args: unknown[]) {
    this.constructorArgs = args;
    FakeSocket.instances.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    const current = this.listeners.get(type) ?? new Set<Listener>();
    current.add(listener);
    this.listeners.set(type, current);
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners.get(type)?.delete(listener);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = 3;
    this.emit('close', {});
  }

  open(): void {
    this.readyState = 1;
    this.emit('open', {});
  }

  serverFrame(frame: unknown): void {
    this.emit('message', {data: JSON.stringify(frame)});
  }

  private emit(type: string, event: {data?: string}): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

test('mobile socket factory supplies Authorization only to React Native socket construction', () => {
  FakeSocket.instances = [];
  const factory = mobileSocketFactory('device-access-token', FakeSocket as unknown as never);
  factory('wss://host.example/api/ws');

  expect(FakeSocket.instances[0].constructorArgs).toEqual([
    'wss://host.example/api/ws',
    ['hermes-gateway-v1'],
    {headers: {Authorization: 'Bearer device-access-token'}},
  ]);
});

test('mobile transport reuses the shared JSON-RPC gateway owner', async () => {
  FakeSocket.instances = [];
  const transport = createMobileGatewayTransport('device-access-token', {
    webSocketConstructor: FakeSocket as unknown as never,
    heartbeatIntervalMs: 0,
    heartbeatDeadlineMs: 0,
    connectTimeoutMs: 1000,
  });
  expect(transport.gateway).toBeInstanceOf(JsonRpcGatewayClient);

  const connecting = transport.connect('ws://127.0.0.1:9124/api/ws');
  FakeSocket.instances[0].open();
  await connecting;

  const request = transport.rpc('session.snapshot', {session_id: 'session'});
  const frame = JSON.parse(FakeSocket.instances[0].sent[0]);
  FakeSocket.instances[0].serverFrame({
    jsonrpc: '2.0',
    id: frame.id,
    result: {snapshot: {}},
  });

  await expect(request).resolves.toEqual({result: {snapshot: {}}});
  transport.close();
});

test('mobile transport refuses empty access tokens before opening a socket', () => {
  expect(() => mobileSocketFactory('   ')).toThrow('requires an access token');
});
