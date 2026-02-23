#!/usr/bin/env python3
# main.py
import sys
import os

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config_manager import ConfigManager

def main():
    """Main entry point"""
    print("Ubuntu Maintenance Tracker")
    print("=" * 30)
    
    # Load or create configuration
    config_manager = ConfigManager()
    
    # Show configuration GUI
    config_manager.show_config_gui()

if __name__ == "__main__":
    main()