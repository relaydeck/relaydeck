# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Reporting a vulnerability

If you believe you have found a security issue in relaydeck, please report it
privately rather than opening a public GitHub issue.

1. Open a [GitHub Security Advisory](https://github.com/relaydeck/relaydeck/security/advisories/new)
   (preferred), or email the maintainers through the contact address on the
   repository if advisory creation is unavailable.
2. Include a clear description, reproduction steps, and impact assessment.
3. Allow reasonable time for a fix before public disclosure.

We aim to acknowledge reports within a few business days.

## Scope notes

relaydeck is a **local-first control plane** that runs on the operator's
machine. The default daemon binds to loopback and uses a local auth token.
Treat `~/.relaydeck/vault.yaml`, `auth-token`, and agent YAML as sensitive
local data — they should never be committed to version control or shared
across trust boundaries.

Plugins and workspace automation (`script`, `code`, GitHub rules, loop actions)
run with the operator's privileges. Only install plugins and automation rules
you trust.
