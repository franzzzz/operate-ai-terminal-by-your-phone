#!/usr/bin/env bash
set -euo pipefail

echo "Pocket Operator demo session started."
sleep 1

echo "Scanning local project state..."
sleep 1

echo "Building a short execution plan..."
sleep 1

echo "Waiting for operator instruction from Telegram..."
echo
echo "Send a reply from Telegram or press the Continue button."

read -r operator_input

echo
echo "Operator instruction received:"
printf '%s\n' "$operator_input"
sleep 1

echo
echo "Continuing work in the same terminal session..."
sleep 1
echo "Step 1/3 complete."
sleep 1
echo "Step 2/3 complete."
sleep 1
echo "Step 3/3 complete."
sleep 1

echo
echo "Demo complete."
