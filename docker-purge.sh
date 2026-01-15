#!/bin/bash
# Purge all related containers, images, and volumes

echo "🗑️  Purging seats-app (containers, images, volumes)..."

# Stop and remove containers
docker-compose down -v --remove-orphans

# Remove the image
docker rmi seats-seats 2>/dev/null || docker rmi seats_seats 2>/dev/null || true

# Remove dangling images
docker image prune -f

echo "✅ Purge complete"
