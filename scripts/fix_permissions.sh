#!/bin/bash

# Fix ownership of music files to current user
echo "Fixing ownership of music files..."
sudo chown -R $USER:$USER Music/

# Fix permissions 
chmod -R 755 Music/

echo "Permissions fixed! Now restart Docker containers:"
echo "docker-compose down && docker-compose up -d"