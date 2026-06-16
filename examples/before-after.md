# Before / After

## Example 1: Session start

**Without Noshy:**

```
User: "What was that proxy fix from last week?"
Agent: "I don't have access to previous sessions. Can you describe the issue?"
User: "The Tailscale thing. Proxy kept disconnecting."
Agent: "I'll need more details. What was the error message?"
User: "Ugh. It was binding to localhost, you said to use 0.0.0.0 instead."
Agent: "Got it. Let me check the proxy config..."
```

**With Noshy:**

```
User: "What was that proxy fix from last week?"
Agent: "[Reading memory...]
From session 3 (June 15): Changed proxy to bind 0.0.0.0 instead of 127.0.0.1.
This fixed the Tailscale disconnection issue. The proxy service was
restarted and confirmed working on all interfaces."
```

## Example 2: Decision tracking

**Without Noshy:**

```
User: "Why did we pick Python over Rust for this project?"
Agent: "I don't have context on that decision. Would you like me to research
the trade-offs between Python and Rust?"
```

**With Noshy:**

```
Agent: "[Decision timeline]
June 16 — Chose Python over Rust for Noshy (decision-framework):
Faster Hermes integration, zero-deps possible with stdlib,
quicker iteration for memory features. Rust was considered
for performance but the overhead wasn't justified for an
SQLite-backed memory layer."
```

## Example 3: Pattern detection

**Without Noshy:**

```
Agent: "Here's a basic auth middleware implementation for your new service."
# Three weeks later...
Agent: "Here's a basic auth middleware for this other service."
# Two months later...
Agent: "Here's a basic auth middleware for—"
User: "I already have three of these."
```

**With Noshy:**

```
Agent: "[Pattern detected]
You've implemented auth middleware 3 times across projects
(hermes-proxy, paper-bot, noshy-server). Consider extracting
this into a reusable module.

Stored instances:
- hermes-proxy: Basic auth with session tokens
- paper-bot: API key auth with rate limiting
- noshy-server: None yet
→ Suggested action: Create a shared auth module"
```

## Example 4: Preference learning

**Without Noshy:**

```
Agent: "I'll write this in camelCase with a class-based architecture."
User: "No. kebab-case. Single-file modules. Always."
# Next session...
Agent: "Here's the new module in camelCase with—"
User: "I literally told you this last time."
```

**With Noshy:**

```
Agent: "[Loading preferences]
- kebab-case for all topic names
- Single-file Python modules preferred
- Hermes Agent + DeepSeek V4 Pro for coding tasks
- Bind services to 0.0.0.0, never localhost

Writing new module in kebab-case, single file. ✓"
```
