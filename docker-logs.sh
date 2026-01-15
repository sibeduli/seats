#!/bin/bash
# View container logs

echo "📋 Viewing seats-app logs..."
docker-compose logs -f --tail=100
