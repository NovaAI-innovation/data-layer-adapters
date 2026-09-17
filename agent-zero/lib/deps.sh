#!/usr/bin/env bash
# agent-zero/lib/deps.sh — VESTIGIAL.
#
# In the A0-first compose flow (see docker-compose.yml a0-init), the shared
# /opt/venv-a0 is populated by the A0 image's own pip, which is libc-compatible
# with the A0 runtime that reuses the volume. No other image should run this
# step. The adapters image already ships psycopg[binary] (requirements.txt).
#
# Kept for documentation/back-compat only. Returns 0 unconditionally.
exit 0
