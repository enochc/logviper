#!/usr/bin/env bash
set -e
GLOBAL=false
[[ "$1" == "--global" ]] && GLOBAL=true

echo "🐍 Checking Python..."
python3 --version || { echo "Python 3.8+ required"; exit 1; }

echo "📦 Installing dependencies..."
pip3 install textual watchdog --break-system-packages 2>/dev/null || pip3 install textual watchdog

if $GLOBAL; then
    echo "🔗 Installing globally..."
    sudo install -m 755 logviper.py /usr/local/bin/logviper
    echo "✅ Run: logviper [file1] [file2]"
else
    chmod +x logviper.py
    echo "✅ Run: python3 logviper.py [file1] [file2]"
fi
