#!/bin/bash
# Start the Docker container (build if needed)

echo "🚀 Starting seats-app..."
docker compose up -d --build
echo "✅ Container started at http://localhost:8080"
