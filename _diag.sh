#!/bin/sh
echo "=== /app ?? ==="
ls /app 2>/dev/null | head -20
echo "=== data ?? ==="
ls /app/data 2>/dev/null | head -20
echo "=== debug-logs ==="
ls -la /app/data/debug-logs 2>/dev/null | tail -5
echo "=== logs ?? ==="
ls -la /app/logs 2>/dev/null | tail -5
