import {
  JsonRpcGatewayClient,
  type GatewayClientOptions,
  type WebSocketLike,
} from '@hermes/shared/json-rpc-gateway';
import type {MobileRpc} from './mobileGateway';

export type ReactNativeWebSocketConstructor = new (
  url: string,
  protocols?: string | string[],
  options?: {headers?: Record<string, string>},
) => WebSocketLike;

/**
 * Build the only mobile socket factory. The shared JsonRpcGatewayClient remains
 * the owner of connection state, heartbeats, replay, request IDs, and pending
 * RPC lifecycle; this adapter only supplies React Native's header-capable
 * WebSocket constructor.
 */
export function mobileSocketFactory(
  accessToken: string,
  constructor: ReactNativeWebSocketConstructor = WebSocket as unknown as ReactNativeWebSocketConstructor,
): (url: string) => WebSocketLike {
  if (!accessToken.trim()) {
    throw new Error('mobile gateway connection requires an access token');
  }
  return (url: string) =>
    new constructor(url, ['hermes-gateway-v1'], {
      headers: {Authorization: `Bearer ${accessToken}`},
    });
}

export type MobileGatewayTransport = {
  gateway: JsonRpcGatewayClient;
  rpc: MobileRpc;
  connect: (url: string) => Promise<void>;
  close: () => void;
};

export function createMobileGatewayTransport(
  accessToken: string,
  options: Omit<GatewayClientOptions, 'socketFactory' | 'requestIdPrefix'> & {
    webSocketConstructor?: ReactNativeWebSocketConstructor;
  } = {},
): MobileGatewayTransport {
  const {webSocketConstructor, ...sharedOptions} = options;
  const gateway = new JsonRpcGatewayClient({
    ...sharedOptions,
    requestIdPrefix: 'mobile-',
    socketFactory: mobileSocketFactory(accessToken, webSocketConstructor),
  });
  const rpc: MobileRpc = async (method, params) => ({
    result: await gateway.request(method, params),
  });
  return {
    gateway,
    rpc,
    connect: (url: string) => gateway.connect(url),
    close: () => gateway.close(),
  };
}
