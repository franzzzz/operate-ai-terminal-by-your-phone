#!/usr/bin/env node

import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const MCP_ERROR_FRAGMENT = 'MCP client';

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8').trim();
}

function requireString(value, name) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`${name} is required`);
  }
  return value;
}

async function pathExists(targetPath) {
  try {
    await fs.access(targetPath);
    return true;
  } catch {
    return false;
  }
}

async function resolveExecutable(name) {
  if (path.isAbsolute(name)) {
    return name;
  }

  const searchPath = process.env.PATH || '';
  for (const entry of searchPath.split(path.delimiter)) {
    if (!entry) {
      continue;
    }
    const candidate = path.join(entry, name);
    if (await pathExists(candidate)) {
      return candidate;
    }
  }
  return name;
}

async function prepareCodexEnvironment() {
  const realHome = os.homedir();
  const realCodexDir = path.join(realHome, '.codex');
  const isolatedCodexDir = path.join(os.tmpdir(), 'pocket-operator-codex-home');

  await fs.mkdir(isolatedCodexDir, { recursive: true });

  const authPath = path.join(realCodexDir, 'auth.json');
  if (await pathExists(authPath)) {
    await fs.copyFile(authPath, path.join(isolatedCodexDir, 'auth.json'));
  }

  const configPath = path.join(realCodexDir, 'config.toml');
  if (await pathExists(configPath)) {
    let configText = await fs.readFile(configPath, 'utf8');
    configText = configText.replaceAll(
      'model_reasoning_effort = "xhigh"',
      'model_reasoning_effort = "high"',
    );
    await fs.writeFile(path.join(isolatedCodexDir, 'config.toml'), configText);
  }

  process.env.CODEX_HOME = isolatedCodexDir;
}

async function prepareClaudeEnvironment() {
  const isolatedConfigDir = path.join(os.tmpdir(), 'pocket-operator-claude-config');
  const debugLogPath = path.join(isolatedConfigDir, 'debug', 'sdk.log');

  await fs.mkdir(path.dirname(debugLogPath), { recursive: true });
  process.env.CLAUDE_CODE_DEBUG_LOGS_DIR = debugLogPath;
}

async function runCodex(request) {
  await prepareCodexEnvironment();
  const { Codex } = await import('@openai/codex-sdk');
  const client = new Codex();

  let thread;
  if (request.resumeSessionId) {
    try {
      thread = client.resumeThread(request.resumeSessionId, {
        workingDirectory: request.cwd,
        skipGitRepoCheck: true,
        modelReasoningEffort: 'high',
      });
    } catch (_error) {
      thread = client.startThread({
        workingDirectory: request.cwd,
        skipGitRepoCheck: true,
        modelReasoningEffort: 'high',
      });
    }
  } else {
    thread = client.startThread({
      workingDirectory: request.cwd,
      skipGitRepoCheck: true,
      modelReasoningEffort: 'high',
    });
  }

  const result = await thread.runStreamed(request.prompt);
  for await (const event of result.events) {
    if (event.type === 'error') {
      if (!String(event.message || '').includes(MCP_ERROR_FRAGMENT)) {
        emit({ type: 'system', content: `⚠️ ${event.message}` });
      }
      continue;
    }

    if (event.type === 'turn.failed') {
      emit({
        type: 'system',
        content: `❌ Turn failed: ${event.error?.message || 'Unknown error'}`,
      });
      break;
    }

    if (event.type === 'item.completed') {
      const item = event.item;
      if (item.type === 'agent_message' && item.text) {
        emit({ type: 'assistant', content: item.text });
      } else if (item.type === 'command_execution' && item.command) {
        emit({ type: 'tool', toolName: item.command });
      } else if (item.type === 'reasoning' && item.text) {
        emit({ type: 'thinking', content: item.text });
      }
    }

    if (event.type === 'turn.completed') {
      emit({ type: 'result', sessionId: thread.id || null });
      break;
    }
  }
}

async function runClaude(request) {
  await prepareClaudeEnvironment();
  const { query } = await import('@anthropic-ai/claude-agent-sdk');
  const claudeExecutable = await resolveExecutable(process.env.CLAUDE_CODE_EXECUTABLE || 'claude');
  const options = {
    cwd: request.cwd,
    env: {
      PATH: process.env.PATH,
      ...process.env,
    },
    pathToClaudeCodeExecutable: claudeExecutable,
    permissionMode: 'bypassPermissions',
    stderr: (data) => {
      const output = String(data || '').trim();
      if (!output) {
        return;
      }
      const lowered = output.toLowerCase();
      if (
        lowered.includes('error') ||
        lowered.includes('fatal') ||
        lowered.includes('failed') ||
        lowered.includes('exception')
      ) {
        process.stderr.write(`${output}\n`);
      }
    },
  };

  if (request.resumeSessionId) {
    options.resume = request.resumeSessionId;
  }

  for await (const msg of query({ prompt: request.prompt, options })) {
    if (msg.type === 'assistant') {
      for (const block of msg.message.content) {
        if (block.type === 'text' && block.text) {
          emit({ type: 'assistant', content: block.text });
        } else if (block.type === 'tool_use') {
          emit({ type: 'tool', toolName: block.name });
        }
      }
    } else if (msg.type === 'result') {
      emit({ type: 'result', sessionId: msg.session_id || null });
    }
  }
}

async function main() {
  const raw = await readStdin();
  if (!raw) {
    throw new Error('No JSON request received on stdin');
  }

  const request = JSON.parse(raw);
  const assistant = requireString(request.assistant, 'assistant').toLowerCase();
  request.cwd = requireString(request.cwd, 'cwd');
  request.prompt = requireString(request.prompt, 'prompt');

  if (assistant === 'codex') {
    await runCodex(request);
    return;
  }
  if (assistant === 'claude') {
    await runClaude(request);
    return;
  }

  throw new Error(`Unsupported assistant: ${assistant}`);
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
});
