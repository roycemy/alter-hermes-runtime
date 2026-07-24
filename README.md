# Alter Hermes Runtime

Deployment configuration for running the Alter Hermes agent on Akash.

## Purpose

This repository contains the infrastructure and deployment configuration for Hermes.

It does **not** contain the Alter application itself.

## Principles

- Never commit API keys or secrets.
- Never commit GitHub tokens.
- Never commit WhatsApp session files.
- Hermes runs in Akash.
- Alter is cloned separately into the runtime.
- Hermes starts in read-only mode until explicitly promoted.
