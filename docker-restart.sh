#!/bin/bash
# Restart the Docker container

echo "🔄 Restarting seats-app..."
docker-compose restart
echo "✅ Container restarted"
docker-compose logs -f --tail=50
