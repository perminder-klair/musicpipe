#!/bin/bash

# Exit on error
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo -e "${GREEN}Setting up Navidrome, Soulseek, and Beets with Docker Compose...${NC}"
echo -e "${GREEN}This setup includes headless music tagging perfect for SSH access.${NC}"
echo -e "${GREEN}Please use 'docker-compose up -d' to start services. See README.md for details.${NC}"

# Check if Docker Compose files exist
if [ ! -f "docker-compose.yml" ] || [ ! -f ".env" ]; then
  echo -e "${RED}Docker Compose files not found. Make sure docker-compose.yml and .env are in the current directory.${NC}"
  exit 1
fi

# Note: Beets runs in Docker - no local installation needed
echo -e "${GREEN}Beets (headless music tagger) will run in Docker container...${NC}"

# Create music folder structure and beets config
echo -e "${GREEN}Setting up music folders and configuration...${NC}"
mkdir -p Music/{Incoming,Library,NavidromeData,SlskdData} beets
chmod -R 755 Music/

# Copy slskd configuration if it exists
if [ -f "slskd/slskd.yml" ]; then
  echo -e "${GREEN}Copying slskd configuration...${NC}"
  cp slskd/slskd.yml Music/SlskdData/slskd.yml
else
  echo -e "${YELLOW}Warning: slskd/slskd.yml not found, using default configuration${NC}"
fi

echo -e "${GREEN}Starting services with Docker Compose...${NC}"
docker-compose up -d

# Wait for services to start
sleep 10

# Check if services are running
if docker-compose ps | grep -q "Up"; then
  echo -e "${GREEN}Services are running!${NC}"
  echo -e "${GREEN}Navidrome: http://localhost:$(grep NAVIDROME_PORT .env | cut -d'=' -f2)${NC}"
  echo -e "${GREEN}slskd: http://localhost:$(grep SLSKD_PORT .env | cut -d'=' -f2)${NC}"
else
  echo -e "${RED}Services failed to start. Check logs with: docker-compose logs${NC}"
  exit 1
fi

echo -e "${GREEN}Setup complete! See README.md and BEETS_GUIDE.md for usage instructions.${NC}"
echo -e "${GREEN}Use ./tag-music.sh to tag downloaded music (headless, SSH-friendly)${NC}"
echo -e "${RED}Legal Note: Only download/share files you have rights to. Use a VPN for privacy.${NC}"
