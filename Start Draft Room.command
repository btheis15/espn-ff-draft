#!/bin/bash
cd "$(dirname "$0")"
echo ""
echo "  Starting Draft Room… your browser will open in a second."
echo "  Leave this window open. Press Ctrl+C here to stop."
echo ""
python3 server.py
echo ""
echo "  Draft Room stopped. You can close this window."
read -n 1 -s -r -p "  Press any key to close."
