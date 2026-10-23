#!/usr/bin/env bash
# usage: tools/sweep.sh '<json dials>' [n]
cd "$(dirname "$0")/.." && TOUCHLINE_DIALS="$1" ../.venv/Scripts/python -m touchline_engine realism --n "${2:-8}" --workers 6 | grep -E "goals_per_match|shots_per_team|xg_per_shot|sot_per|corners|pass_completion|ball_in_play|metrics inside"
