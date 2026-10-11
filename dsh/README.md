# dsh-stack-guardian

A DeepSeek Harness plugin for [stack-guardian](https://github.com/LuminariSoftwares/stack-guardian). At the start of a
session it runs `stack-guardian doctor --json --quiet`. When the report is WARN or FAIL it adds one short line to the
conversation, for example that a node is unreachable or a model is not loaded. When everything is OK it adds nothing.

## Needs

- Node 20+
- the Python CLI on PATH: `pip install stack-guardian`
- `STACK_GUARDIAN_NODES` set to your nodes file in the harness environment (see
  [examples/nodes.json](https://github.com/LuminariSoftwares/stack-guardian/blob/main/examples/nodes.json))

## Install

```
npm install dsh-stack-guardian
```

Then add the plugin to your harness configuration. Defaults: 5 s budget, injects on WARN and FAIL, at most 400
characters. A timeout or an unreadable report adds nothing, so a broken doctor never blocks a session.

MIT licensed.
