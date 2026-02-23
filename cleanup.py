#!/usr/bin/env python3
# cleanup.py
import json
import os

def fix_config():
    config_file = "config.json"
    
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
        except:
            config = {}
    else:
        config = {}
    
    # Ensure all required sections exist
    if 'email' not in config:
        config['email'] = {}
    
    if 'spreadsheet' not in config:
        config['spreadsheet'] = {}
    
    if 'notification' not in config:
        config['notification'] = {}
    
    # Set default values for missing fields
    config['email'].setdefault('imap_server', 'imap.gmail.com')
    config['email'].setdefault('imap_port', 993)
    config['email'].setdefault('email_address', '')
    config['email'].setdefault('password', '')
    config['email'].setdefault('check_interval', 60)
    
    # Fix spreadsheet path
    if 'file_path' not in config['spreadsheet'] or not config['spreadsheet']['file_path']:
        downloads_path = os.path.join(os.path.expanduser('~'), 'Downloads')
        config['spreadsheet']['file_path'] = os.path.join(downloads_path, 'maintenance_jobs.xlsx')
    
    config['spreadsheet'].setdefault('sheet_name', 'Jobs')
    
    config['notification'].setdefault('weekly_report_email', 'ppayne@bmihospitality.com')
    config['notification'].setdefault('send_day', 'monday')
    config['notification'].setdefault('send_time', '09:00')
    
    # Save fixed config
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=4)
    
    print(f"Configuration fixed and saved to {config_file}")
    print(f"Spreadsheet path: {config['spreadsheet']['file_path']}")

if __name__ == "__main__":
    fix_config()