#!/bin/bash
# Open a shell inside the container

echo "🐚 Opening shell in seats-app..."
docker-compose exec seats /bin/bash
