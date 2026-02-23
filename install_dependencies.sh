#!/bin/bash
# install_dependencies.sh - Install system dependencies

echo "Installing system dependencies for Maintenance Tracker..."
echo ""

# Update package list
sudo apt update

# Install Python and required packages
echo "Installing Python and system packages..."
sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    python3-tk \
    python3-pil \
    python3-pil.imagetk

echo ""
echo "System dependencies installed successfully!"
echo ""
echo "Now run: ./start.sh"