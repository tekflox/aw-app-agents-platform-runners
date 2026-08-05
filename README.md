# Agents Platform Runners

Agents Platform Runners connects an AW Workspace to hosted agent sessions. It lets the workspace start, supervise, and reuse coding-agent runners from the same place where the work is happening.

## What It Does

- Registers this workspace as a runner target for Agents Platform.
- Starts agent sessions that can work inside the workspace.
- Streams run output back to the platform so progress is visible.
- Adds agent-focused tools and skills for Telegram replies, supervision, coding work, documentation work, QA, and coordinated multi-agent runs.

## Why Use It

Use this app when an AW Workspace should be able to receive agent jobs from Agents Platform and run them against the workspace environment. It is useful for delegated coding tasks, documentation work, QA passes, and long-running workflows that need a reusable runner.

## How To Use It

Install the app in the workspace, open its settings, and connect it to the Agents Platform instance that should dispatch work here. Once configured, the platform can launch runs through this workspace and agents can use the contributed tools from their normal sessions.

## What It Delivers

The app turns the workspace into an active execution target for agent work. Instead of treating the workspace as only a place to store code and data, it makes the workspace available as a controlled runner that can accept tasks, expose progress, and keep agent workflows close to the files and services they need.
