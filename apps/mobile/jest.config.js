// Hermes/Ares launches commands with NODE_ENV=production. Jest normally only
// supplies `test` when NODE_ENV is unset; force the test build so React's test
// renderer exposes `act` consistently across shells and CI.
process.env.NODE_ENV = 'test';

module.exports = {
  preset: '@react-native/jest-preset',
  // Shared TypeScript source is intentionally consumed from a sibling package;
  // direct Jest resolution from that path cannot ascend into this package's
  // dependency tree, so bind the transpiler helper to this local runtime.
  moduleNameMapper: {
    '^@babel/runtime/(.*)$': '<rootDir>/node_modules/@babel/runtime/$1',
  },
};
