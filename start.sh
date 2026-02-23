#!/bin/bash
# start.sh - One-click startup script for Maintenance Tracker

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=========================================${NC}"
echo -e "${GREEN}   Maintenance Tracker - One Click Start${NC}"
echo -e "${BLUE}=========================================${NC}"
echo ""

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

# Check Python installation
echo -e "${YELLOW}[1/6] Checking Python installation...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Python3 not found! Installing...${NC}"
    sudo apt update
    sudo apt install python3 python3-venv python3-pip python3-tk -y
fi

PYTHON_VERSION=$(python3 --version)
echo -e "${GREEN}✓ Python version: $PYTHON_VERSION${NC}"

# Check virtual environment
echo -e "${YELLOW}[2/6] Setting up virtual environment...${NC}"
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating new virtual environment...${NC}"
    python3 -m venv venv
    echo -e "${GREEN}✓ Virtual environment created${NC}"
else
    echo -e "${GREEN}✓ Virtual environment already exists${NC}"
fi

# Activate virtual environment
echo -e "${YELLOW}[3/6] Activating virtual environment...${NC}"
source venv/bin/activate
echo -e "${GREEN}✓ Virtual environment activated${NC}"

# Upgrade pip
echo -e "${YELLOW}[4/6] Updating pip...${NC}"
pip install --upgrade pip
echo -e "${GREEN}✓ pip upgraded${NC}"

# Install/update requirements
echo -e "${YELLOW}[5/6] Installing dependencies...${NC}"
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
    echo -e "${GREEN}✓ Dependencies installed${NC}"
else
    echo -e "${YELLOW}No requirements.txt found, installing default packages...${NC}"
    pip install pandas openpyxl imapclient schedule python-dateutil pytz
    echo -e "${GREEN}✓ Default packages installed${NC}"
fi

# Fix any config issues
echo -e "${YELLOW}[6/6] Preparing application...${NC}"
if [ -f "cleanup.py" ]; then
    python cleanup.py
    echo -e "${GREEN}✓ Configuration cleaned up${NC}"
fi

echo ""
echo -e "${BLUE}=========================================${NC}"
echo -e "${GREEN}          Starting Application${NC}"
echo -e "${BLUE}=========================================${NC}"
echo ""

# Launch the application
python main.py

# Deactivate virtual environment when done
deactivate