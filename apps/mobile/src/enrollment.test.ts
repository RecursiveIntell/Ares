import {
  canonicalEnrollmentMessage,
  canonicalEnrollmentMessageBase64,
} from './enrollment';

const challenge = {
  challenge_id: 'challenge-1',
  challenge: 'one-use-value',
  host_id: 'a'.repeat(64),
  expires_at: 1760000000.25,
  requested_scopes: ['session:control', 'session:read', 'session:read'],
};

test('canonical enrollment message is deterministic and sorted', () => {
  expect(canonicalEnrollmentMessage(challenge, 'app-1')).toBe(
    'ares-mobile-enrollment-v1\0{"app_instance_id":"app-1","challenge":"one-use-value","challenge_id":"challenge-1","expires_at":"1760000000.250000","host_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","requested_scopes":["session:control","session:read"]}',
  );
});

test('canonical enrollment message has a base64 transport form', () => {
  expect(canonicalEnrollmentMessageBase64(challenge, 'app-1')).toMatch(/^[A-Za-z0-9+/]+=*$/);
});
