#!/usr/bin/env python3
import os
import sys
import subprocess

def run_command(cmd):
    """Run a shell command and return output"""
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
    return result.returncode, result.stdout, result.stderr

def main():
    print("="*50)
    print("Maintenance Tracker Launcher")
    print("="*50)
    
    # Check Python
    print("\n1. Checking Python...")
    ret, out, err = run_command("python3 --version")
    if ret != 0:
        print("Python3 not found. Trying to install...")
        run_command("sudo apt update && sudo apt install -y python3 python3-venv")
    
    # Setup venv
    print("\n2. Setting up virtual environment...")
    if not os.path.exists("venv"):
        run_command("python3 -m venv venv")
    else:
        print("Virtual environment already exists")
    
    # Activate and install
    print("\n3. Installing dependencies...")
    
    # Determine pip path
    if sys.platform == "win32":
        pip = "venv\\Scripts\\pip"
        python = "venv\\Scripts\\python"
    else:
        pip = "venv/bin/pip"
        python = "venv/bin/python"
    
    run_command(f"{pip} install --upgrade pip")
    run_command(f"{pip} install pandas openpyxl imapclient schedule python-dateutil pytz")
    
    # Launch
    print("\n4. Launching application...")
    print("="*50)
    os.system(f"{python} main.py")

if __name__ == "__main__":
    main()