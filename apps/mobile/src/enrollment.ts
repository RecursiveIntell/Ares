/* eslint-disable no-bitwise -- byte-level UTF-8/base64 encoding requires masks and shifts. */
import {MOBILE_ENROLLMENT_DOMAIN} from '@hermes/shared/protocol';

export type EnrollmentChallenge = {
  challenge_id: string;
  challenge: string;
  host_id: string;
  expires_at: number;
  requested_scopes: string[];
};

export type EnrollmentProof = {
  challenge_id: string;
  challenge: string;
  app_instance_id: string;
  public_key_der_b64: string;
  signature_b64: string;
};

function normalizeScopes(scopes: string[]): string[] {
  return [...new Set(scopes.map((scope) => scope.trim()).filter(Boolean))].sort();
}

export function canonicalEnrollmentMessage(challenge: EnrollmentChallenge, appInstanceId: string): string {
  const fields = {
    app_instance_id: appInstanceId,
    challenge: challenge.challenge,
    challenge_id: challenge.challenge_id,
    expires_at: challenge.expires_at.toFixed(6),
    host_id: challenge.host_id,
    requested_scopes: normalizeScopes(challenge.requested_scopes),
  };
  return `${MOBILE_ENROLLMENT_DOMAIN}\0${JSON.stringify(fields)}`;
}

function utf8Bytes(value: string): number[] {
  const bytes: number[] = [];
  for (let index = 0; index < value.length; ) {
    const codePoint = value.codePointAt(index) ?? 0;
    index += codePoint > 0xffff ? 2 : 1;
    if (codePoint <= 0x7f) {
      bytes.push(codePoint);
    } else if (codePoint <= 0x7ff) {
      bytes.push(0xc0 | (codePoint >> 6), 0x80 | (codePoint & 0x3f));
    } else if (codePoint <= 0xffff) {
      bytes.push(
        0xe0 | (codePoint >> 12),
        0x80 | ((codePoint >> 6) & 0x3f),
        0x80 | (codePoint & 0x3f),
      );
    } else {
      bytes.push(
        0xf0 | (codePoint >> 18),
        0x80 | ((codePoint >> 12) & 0x3f),
        0x80 | ((codePoint >> 6) & 0x3f),
        0x80 | (codePoint & 0x3f),
      );
    }
  }
  return bytes;
}

function base64FromUtf8(value: string): string {
  const bytes = utf8Bytes(value);
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  let output = '';
  for (let index = 0; index < bytes.length; index += 3) {
    const first = bytes[index];
    const second = bytes[index + 1];
    const third = bytes[index + 2];
    const triplet = (first << 16) | ((second ?? 0) << 8) | (third ?? 0);
    output += alphabet[(triplet >> 18) & 63];
    output += alphabet[(triplet >> 12) & 63];
    output += second === undefined ? '=' : alphabet[(triplet >> 6) & 63];
    output += third === undefined ? '=' : alphabet[triplet & 63];
  }
  return output;
}

export function canonicalEnrollmentMessageBase64(
  challenge: EnrollmentChallenge,
  appInstanceId: string,
): string {
  return base64FromUtf8(canonicalEnrollmentMessage(challenge, appInstanceId));
}

export type EnrollmentKeyStoreBridge = {
  publicKeyDer(): Promise<string>;
  publicKeyFingerprint(): Promise<string>;
  signEnrollmentMessage(messageBase64: string): Promise<string>;
};

export async function buildEnrollmentProof(
  challenge: EnrollmentChallenge,
  appInstanceId: string,
  keyStore: EnrollmentKeyStoreBridge,
): Promise<EnrollmentProof> {
  const messageBase64 = canonicalEnrollmentMessageBase64(challenge, appInstanceId);
  return {
    challenge_id: challenge.challenge_id,
    challenge: challenge.challenge,
    app_instance_id: appInstanceId,
    public_key_der_b64: await keyStore.publicKeyDer(),
    signature_b64: await keyStore.signEnrollmentMessage(messageBase64),
  };
}
