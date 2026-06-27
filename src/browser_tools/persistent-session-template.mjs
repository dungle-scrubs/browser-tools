#!/usr/bin/env node
/**
 * Chrome DevTools MCP - Persistent Session Template
 *
 * This template demonstrates the CORRECT way to use chrome-devtools-mcp:
 * maintaining a single persistent server session for multiple operations.
 *
 * Usage:
 *   node persistent-session-template.mjs <url>
 *
 * Example:
 *   node persistent-session-template.mjs https://ign.com
 */

import { spawn } from 'child_process';

// Parse command line arguments
const targetUrl = process.argv[2] || 'https://example.com';

// Start chrome-devtools-mcp server with persistent session
const server = spawn('npx', ['-y', 'chrome-devtools-mcp@latest', '--channel', 'canary', '--isolated'], {
  stdio: ['pipe', 'pipe', 'pipe']
});

let msgId = 1;
const pending = new Map();

// Handle server responses
server.stdout.on('data', (data) => {
  const lines = data.toString().split('\n').filter(l => l.trim());
  for (const line of lines) {
    try {
      const msg = JSON.parse(line);
      if (msg.id && pending.has(msg.id)) {
        pending.get(msg.id)(msg);
        pending.delete(msg.id);
      }
    } catch (e) {
      // Ignore non-JSON output
    }
  }
});

server.stderr.on('data', (data) => {
  console.error('[Server]:', data.toString());
});

// Send MCP request to server
function sendRequest(method, params = {}) {
  return new Promise((resolve) => {
    const id = msgId++;
    const request = { jsonrpc: '2.0', method, params, id };
    server.stdin.write(JSON.stringify(request) + '\n');
    pending.set(id, resolve);
  });
}

// Main automation workflow
async function main() {
  console.log('Initializing chrome-devtools-mcp server...');

  // Wait for server to start
  await new Promise(r => setTimeout(r, 2000));

  // Initialize MCP session
  await sendRequest('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'persistent-session', version: '1.0.0' }
  });

  console.log(`Navigating to ${targetUrl}...`);

  // Navigate to target URL
  const navResult = await sendRequest('tools/call', {
    name: 'navigate_page',
    arguments: {
      type: 'url',
      url: targetUrl,
      timeout: 30000
    }
  });

  const navText = navResult.result.content[0].text;
  console.log(navText.substring(0, 200));

  // Wait for page to fully load
  console.log('\nWaiting for page to fully load...');
  await new Promise(r => setTimeout(r, 5000));

  // Fetch console messages (SAME server session - messages preserved!)
  console.log('\nFetching console messages...');
  const consoleResult = await sendRequest('tools/call', {
    name: 'list_console_messages',
    arguments: { pageSize: 500 }
  });

  console.log('\n' + consoleResult.result.content[0].text);

  // Optional: Take screenshot
  console.log('\nTaking screenshot...');
  const screenshotResult = await sendRequest('tools/call', {
    name: 'take_screenshot',
    arguments: {
      filePath: '/tmp/chrome-screenshot.png',
      fullPage: false
    }
  });

  console.log(screenshotResult.result.content[0].text);

  // Optional: Get network requests
  console.log('\nFetching network requests...');
  const networkResult = await sendRequest('tools/call', {
    name: 'list_network_requests',
    arguments: { pageSize: 50 }
  });

  console.log(networkResult.result.content[0].text.substring(0, 500));

  // Cleanup
  console.log('\nClosing server...');
  server.kill();
  process.exit(0);
}

// Run main workflow
main().catch(error => {
  console.error('Error:', error);
  server.kill();
  process.exit(1);
});
