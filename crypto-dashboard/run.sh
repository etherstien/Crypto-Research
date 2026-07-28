#!/usr/bin/env bash
set -e

if [ ! -f ".env" ]; then
  echo "No .env file found. Copying .env.example → .env"
  cp .env.example .env
  echo "Edit .env with your API keys, then re-run this script."
  exit 1
fi

if [ ! -d "venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

echo ""
echo "  Dashboard running at: http://localhost:5000"
echo ""
python app.py
