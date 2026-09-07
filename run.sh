#!/bin/bash
cd "$(dirname "$0")"
set -a
source config.env
set +a
exec .venv/bin/python bot.py
