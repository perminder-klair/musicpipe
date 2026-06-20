#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
AUTO_MODE=false
QUIET_MODE=false
DRY_RUN=false

# Function to display help
show_help() {
    echo -e "${GREEN}Music Tagging Script - Docker Beets Integration${NC}"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -a, --auto      Auto-tag without prompts (uses strong match threshold)"
    echo "  -q, --quiet     Quiet mode - minimal output"
    echo "  -d, --dry-run   Preview what would be imported without making changes"
    echo "  -h, --help      Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0              # Interactive tagging (recommended for first use)"
    echo "  $0 --auto       # Automatic tagging of all files"
    echo "  $0 --dry-run    # Preview what would be tagged"
    echo ""
    echo "This script imports music from Music/Incoming/ to Music/Library/"
    echo "using MusicBrainz database for accurate tagging."
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -a|--auto)
            AUTO_MODE=true
            shift
            ;;
        -q|--quiet)
            QUIET_MODE=true
            shift
            ;;
        -d|--dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use -h or --help for usage information"
            exit 1
            ;;
    esac
done

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null; then
    echo -e "${RED}Error: docker-compose is not installed or not in PATH${NC}"
    exit 1
fi

# Check if docker-compose.yml exists
if [ ! -f "docker-compose.yml" ]; then
    echo -e "${RED}Error: docker-compose.yml not found in current directory${NC}"
    exit 1
fi

# Check if incoming directory has files
INCOMING_DIR="${INCOMING_DIR:-./Music/Incoming}"
if [ ! -d "$INCOMING_DIR" ]; then
    echo -e "${RED}Error: Incoming directory $INCOMING_DIR does not exist${NC}"
    exit 1
fi

# Check if there are music files in incoming directory
MUSIC_FILES=$(find -L "$INCOMING_DIR" -type f \( -name "*.mp3" -o -name "*.flac" -o -name "*.m4a" -o -name "*.ogg" -o -name "*.wav" \) 2>/dev/null | wc -l)
if [ "$MUSIC_FILES" -eq 0 ]; then
    echo -e "${YELLOW}No music files found in $INCOMING_DIR${NC}"
    echo -e "${BLUE}Supported formats: MP3, FLAC, M4A, OGG, WAV${NC}"
    exit 0
fi

echo -e "${GREEN}Found $MUSIC_FILES music files in $INCOMING_DIR${NC}"

# Ensure beets container is running
if ! docker-compose ps beets --status running 2>/dev/null | grep -q beets; then
    echo -e "${YELLOW}Starting beets container...${NC}"
    docker-compose up -d beets
    sleep 3
fi

# Build command based on options
BEET_CMD="beet import"

if [ "$AUTO_MODE" = true ]; then
    BEET_CMD="$BEET_CMD -A"  # Auto-tag
    echo -e "${BLUE}Running in automatic mode - strong matches will be imported automatically${NC}"
fi

if [ "$QUIET_MODE" = true ]; then
    BEET_CMD="$BEET_CMD -q"  # Quiet
fi

if [ "$DRY_RUN" = true ]; then
    BEET_CMD="$BEET_CMD -p"  # Pretend (dry-run)
    echo -e "${BLUE}Dry run mode - no files will be moved or modified${NC}"
fi

BEET_CMD="$BEET_CMD /music/incoming"

echo -e "${GREEN}Running: docker-compose exec beets $BEET_CMD${NC}"
echo ""

if [ "$AUTO_MODE" = false ] && [ "$DRY_RUN" = false ]; then
    echo -e "${YELLOW}Interactive Mode Tips:${NC}"
    echo "  - Press 'y' to accept a match"
    echo "  - Press 'n' to skip/reject"  
    echo "  - Press 's' to skip this album entirely"
    echo "  - Press 'u' to import as-is without MusicBrainz match"
    echo "  - Press 'q' to quit"
    echo ""
fi

# Execute the beets import command
docker-compose exec beets $BEET_CMD

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo ""
    echo -e "${GREEN}✓ Import completed successfully!${NC}"
    if [ "$DRY_RUN" = false ]; then
        echo -e "${BLUE}Tagged music has been moved to Music/Library/${NC}"
        echo -e "${BLUE}Navidrome will automatically scan for new files within an hour${NC}"
    fi
else
    echo ""
    echo -e "${RED}✗ Import failed with exit code $EXIT_CODE${NC}"
    echo -e "${YELLOW}Check the output above for error details${NC}"
fi

# Show some post-import statistics if not in quiet mode
if [ "$QUIET_MODE" = false ] && [ "$DRY_RUN" = false ]; then
    echo ""
    echo -e "${GREEN}Library Statistics:${NC}"
    docker-compose exec beets beet stats
fi

echo ""
echo -e "${GREEN}Useful commands:${NC}"
echo "  docker-compose exec beets beet ls          # List imported music"
echo "  docker-compose exec beets beet stats       # Show library statistics"
echo "  docker-compose exec beets beet update      # Update tags from MusicBrainz"
echo "  docker-compose logs beets                  # View beets container logs"

exit $EXIT_CODE