/**
 * @format
 */

import React from 'react';
import ReactTestRenderer from 'react-test-renderer';
import App, {refreshStatus} from '../App';

test('renders the observe-only boundary and refresh state', async () => {
  await ReactTestRenderer.act(() => {
    ReactTestRenderer.create(<App />);
  });
  expect(refreshStatus('idle')).toContain('No network request has been issued.');
  expect(refreshStatus('requested')).toContain('host enrollment is required before connection');
});
