#!/bin/bash

# Script to create symlinks from local Music directories to SSD storage
# This allows Docker to use local paths while storing data on external drive

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration - modify these paths as needed
SSD_BASE="/mnt/SharedDataSsd/navidrome"
LOCAL_BASE="./Music"

echo -e "${GREEN}Setting up symlinks for music directories...${NC}"

# Function to create symlink safely
create_symlink() {
    local target="$1"
    local link="$2"
    
    # Remove existing directory or broken symlink
    if [ -L "$link" ]; then
        echo -e "${YELLOW}Removing existing symlink: $link${NC}"
        rm "$link"
    elif [ -d "$link" ]; then
        if [ -z "$(ls -A $link)" ]; then
            echo -e "${YELLOW}Removing empty directory: $link${NC}"
            rmdir "$link"
        else
            echo -e "${RED}Directory $link is not empty! Please backup/remove manually.${NC}"
            return 1
        fi
    fi
    
    # Create target directory if it doesn't exist
    if [ ! -d "$target" ]; then
        echo -e "${GREEN}Creating target directory: $target${NC}"
        mkdir -p "$target"
    fi
    
    # Create symlink
    echo -e "${GREEN}Creating symlink: $link -> $target${NC}"
    ln -s "$target" "$link"
}

# Create Music directory if it doesn't exist
mkdir -p "$LOCAL_BASE"

# Create symlinks for each directory
echo -e "${GREEN}Creating symlinks for music directories...${NC}"

create_symlink "$SSD_BASE/Incoming" "$LOCAL_BASE/Incoming"
create_symlink "$SSD_BASE/Library" "$LOCAL_BASE/Library"
create_symlink "$SSD_BASE/NavidromeData" "$LOCAL_BASE/NavidromeData"
create_symlink "$SSD_BASE/SlskdData" "$LOCAL_BASE/SlskdData"

# Verify symlinks
echo -e "\n${GREEN}Verifying symlinks:${NC}"
ls -la "$LOCAL_BASE/"

# Update .env file to use local paths
if [ -f ".env" ]; then
    echo -e "\n${GREEN}Updating .env file to use local paths...${NC}"
    
    # Backup existing .env
    cp .env .env.backup
    
    # Update paths in .env
    cat > .env << 'EOF'
# Music Directory Configuration
MUSIC_ROOT=./Music
INCOMING_DIR=./Music/Incoming
LIBRARY_DIR=./Music/Library
NAVIDROME_DATA=./Music/NavidromeData
SLSKD_DATA=./Music/SlskdData

# Beets Configuration
BEETS_CONFIG=./beets
PUID=1000
PGID=1001
TZ=America/New_York

# Port Configuration
NAVIDROME_PORT=4533
SLSKD_PORT=5030
EOF
    
    echo -e "${GREEN}.env file updated (backup saved as .env.backup)${NC}"
else
    echo -e "${YELLOW}No .env file found. Please create one based on .env.example${NC}"
fi

echo -e "\n${GREEN}Symlink setup complete!${NC}"
echo -e "${GREEN}Your music data will be stored on: $SSD_BASE${NC}"
echo -e "${GREEN}Docker will access it through: $LOCAL_BASE${NC}"
echo -e "\n${YELLOW}Note: You may need to restart Docker containers:${NC}"
echo -e "  docker-compose down"
echo -e "  docker-compose up -d"