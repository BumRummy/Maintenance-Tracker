# config_manager.py
import json
import os
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

class ConfigManager:
    def __init__(self):
        self.config_file = "config.json"
        self.config = self.load_config()
    
    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    
                # Remove spreadsheet file_path from config - it's always in app directory
                if 'spreadsheet' in config:
                    # Keep only sheet_name
                    config['spreadsheet'] = {
                        'sheet_name': config['spreadsheet'].get('sheet_name', 'Jobs')
                    }
                
                # Ensure CC emails field exists
                if 'notification' in config:
                    if 'cc_emails' not in config['notification']:
                        config['notification']['cc_emails'] = ""
                
                return config
            except:
                return self.get_default_config()
        return self.get_default_config()
    
    def save_config(self, config):
        self.config = config
        with open(self.config_file, 'w') as f:
            json.dump(config, f, indent=4)
    
    def get_default_config(self):
        # Spreadsheet is always in app directory now
        return {
            "email": {
                "imap_server": "imap.gmail.com",
                "imap_port": 993,
                "smtp_server": "smtp.gmail.com",
                "smtp_port": 587,
                "use_tls": True,
                "email_address": "",
                "password": "",
                "check_interval": 60
            },
            "spreadsheet": {
                "sheet_name": "Jobs"
                # file_path is removed - always in app directory
            },
            "notification": {
                "weekly_report_email": "ppayne@bmihospitality.com",
                "cc_emails": "",  # NEW: CC emails for completion notifications
                "send_day": "monday",
                "send_time": "09:00"
            },
            "kiosk": {
                "password": "admin123",
                "fullscreen": True
            }
        }
    
    def show_config_gui(self):
        """GUI for initial setup and configuration"""
        self.config_window = tk.Tk()
        self.config_window.title("Maintenance Tracker - Configuration")
        self.config_window.geometry("600x800")  # Slightly taller for CC field
        
        # Make window resizable
        self.config_window.resizable(True, True)
        
        # Style
        style = ttk.Style()
        style.configure('TLabel', font=('Arial', 10))
        style.configure('TEntry', font=('Arial', 10))
        
        # Create main frame with scrollbar
        main_frame = tk.Frame(self.config_window)
        main_frame.pack(fill='both', expand=True, padx=10, pady=10)
        
        # Create canvas and scrollbar
        canvas = tk.Canvas(main_frame)
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Title
        title_label = tk.Label(scrollable_frame, 
                              text="Maintenance Tracker Configuration",
                              font=('Arial', 16, 'bold'))
        title_label.pack(pady=20)
        
        # Email Configuration Frame
        email_frame = ttk.LabelFrame(scrollable_frame, text="Email Configuration (IMAP - Receiving)")
        email_frame.pack(padx=20, pady=10, fill='x')
        
        ttk.Label(email_frame, text="IMAP Server:").grid(row=0, column=0, padx=5, pady=5, sticky='w')
        self.imap_server_var = tk.StringVar(value=self.config['email']['imap_server'])
        ttk.Entry(email_frame, textvariable=self.imap_server_var, width=40).grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(email_frame, text="IMAP Port:").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.imap_port_var = tk.StringVar(value=str(self.config['email']['imap_port']))
        ttk.Entry(email_frame, textvariable=self.imap_port_var, width=40).grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(email_frame, text="Email Address:").grid(row=2, column=0, padx=5, pady=5, sticky='w')
        self.email_var = tk.StringVar(value=self.config['email']['email_address'])
        ttk.Entry(email_frame, textvariable=self.email_var, width=40).grid(row=2, column=1, padx=5, pady=5)
        
        ttk.Label(email_frame, text="Password/App Password:").grid(row=3, column=0, padx=5, pady=5, sticky='w')
        self.password_var = tk.StringVar(value=self.config['email']['password'])
        ttk.Entry(email_frame, textvariable=self.password_var, show="*", width=40).grid(row=3, column=1, padx=5, pady=5)
        
        ttk.Label(email_frame, text="Check Interval (seconds):").grid(row=4, column=0, padx=5, pady=5, sticky='w')
        self.interval_var = tk.StringVar(value=str(self.config['email']['check_interval']))
        ttk.Entry(email_frame, textvariable=self.interval_var, width=40).grid(row=4, column=1, padx=5, pady=5)
        
        # SMTP Configuration Frame
        smtp_frame = ttk.LabelFrame(scrollable_frame, text="SMTP Configuration (For Sending Emails)")
        smtp_frame.pack(padx=20, pady=10, fill='x')
        
        ttk.Label(smtp_frame, text="SMTP Server:").grid(row=0, column=0, padx=5, pady=5, sticky='w')
        self.smtp_server_var = tk.StringVar(value=self.config['email'].get('smtp_server', 'smtp.gmail.com'))
        ttk.Entry(smtp_frame, textvariable=self.smtp_server_var, width=40).grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(smtp_frame, text="SMTP Port:").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.smtp_port_var = tk.StringVar(value=str(self.config['email'].get('smtp_port', 587)))
        ttk.Entry(smtp_frame, textvariable=self.smtp_port_var, width=40).grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(smtp_frame, text="Use TLS (port 587) / SSL (port 465):").grid(row=2, column=0, padx=5, pady=5, sticky='w')
        self.use_tls_var = tk.BooleanVar(value=self.config['email'].get('use_tls', True))
        ttk.Checkbutton(smtp_frame, variable=self.use_tls_var, text="Use TLS (port 587)").grid(row=2, column=1, padx=5, pady=5, sticky='w')
        
        # Spreadsheet Configuration Frame - SIMPLIFIED
        spreadsheet_frame = ttk.LabelFrame(scrollable_frame, text="Spreadsheet Configuration")
        spreadsheet_frame.pack(padx=20, pady=10, fill='x')
        
        info_label = tk.Label(spreadsheet_frame,
                             text="Spreadsheet is automatically saved in the same directory\nas the application (maintenance_jobs.xlsx)",
                             font=('Arial', 9, 'italic'),
                             fg='#666666')
        info_label.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky='w')
        
        ttk.Label(spreadsheet_frame, text="Sheet Name:").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.sheet_name_var = tk.StringVar(value=self.config['spreadsheet'].get('sheet_name', 'Jobs'))
        ttk.Entry(spreadsheet_frame, textvariable=self.sheet_name_var, width=40).grid(row=1, column=1, padx=5, pady=5)
        
        # Notification Configuration Frame - UPDATED WITH CC
        notify_frame = ttk.LabelFrame(scrollable_frame, text="Email Notification Configuration")
        notify_frame.pack(padx=20, pady=10, fill='x')
        
        ttk.Label(notify_frame, text="Weekly Report Email:").grid(row=0, column=0, padx=5, pady=5, sticky='w')
        self.report_email_var = tk.StringVar(value=self.config['notification']['weekly_report_email'])
        ttk.Entry(notify_frame, textvariable=self.report_email_var, width=40).grid(row=0, column=1, padx=5, pady=5)
        
        # NEW: CC Emails for completion notifications
        ttk.Label(notify_frame, text="CC Emails for Completion:").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.cc_emails_var = tk.StringVar(value=self.config['notification'].get('cc_emails', ''))
        cc_entry = ttk.Entry(notify_frame, textvariable=self.cc_emails_var, width=40)
        cc_entry.grid(row=1, column=1, padx=5, pady=5)
        
        cc_help = tk.Label(notify_frame,
                          text="Separate multiple emails with commas",
                          font=('Arial', 8, 'italic'),
                          fg='#666666')
        cc_help.grid(row=2, column=0, columnspan=2, padx=5, pady=(0, 5), sticky='w')
        
        # Kiosk Configuration Frame
        kiosk_frame = ttk.LabelFrame(scrollable_frame, text="Kiosk Mode Configuration")
        kiosk_frame.pack(padx=20, pady=10, fill='x')
        
        password_info_label = tk.Label(kiosk_frame, 
                                      text="This password is used for both minimizing and exiting the kiosk application.",
                                      font=('Arial', 9, 'italic'),
                                      fg='#666666',
                                      wraplength=500)
        password_info_label.grid(row=0, column=0, columnspan=2, padx=5, pady=(5, 0), sticky='w')
        
        ttk.Label(kiosk_frame, text="Kiosk Password:").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.kiosk_password_var = tk.StringVar(value=self.config.get('kiosk', {}).get('password', 'admin123'))
        ttk.Entry(kiosk_frame, textvariable=self.kiosk_password_var, show="*", width=40).grid(row=1, column=1, padx=5, pady=5)
        
        # Buttons
        button_frame = tk.Frame(scrollable_frame)
        button_frame.pack(pady=20)
        
        ttk.Button(button_frame, text="Save Configuration", 
                  command=self.save_configuration).pack(side=tk.LEFT, padx=10)
        ttk.Button(button_frame, text="Test IMAP Connection", 
                  command=self.test_imap_connection).pack(side=tk.LEFT, padx=10)
        ttk.Button(button_frame, text="Test SMTP Connection", 
                  command=self.test_smtp_connection).pack(side=tk.LEFT, padx=10)
        ttk.Button(button_frame, text="Launch Application", 
                  command=self.launch_main_app).pack(side=tk.LEFT, padx=10)
        
        # Configure grid column weights
        for frame in [email_frame, smtp_frame, spreadsheet_frame, notify_frame, kiosk_frame]:
            frame.grid_columnconfigure(1, weight=1)
        
        self.config_window.mainloop()
    
    def save_configuration(self):
        """Save the configuration"""
        try:
            new_config = {
                "email": {
                    "imap_server": self.imap_server_var.get().strip(),
                    "imap_port": int(self.imap_port_var.get()),
                    "smtp_server": self.smtp_server_var.get().strip(),
                    "smtp_port": int(self.smtp_port_var.get()),
                    "use_tls": self.use_tls_var.get(),
                    "email_address": self.email_var.get().strip(),
                    "password": self.password_var.get(),
                    "check_interval": int(self.interval_var.get())
                },
                "spreadsheet": {
                    "sheet_name": self.sheet_name_var.get().strip() or "Jobs"
                    # No file_path - always in app directory
                },
                "notification": {
                    "weekly_report_email": self.report_email_var.get().strip(),
                    "cc_emails": self.cc_emails_var.get().strip(),  # NEW: CC emails
                    "send_day": "monday",
                    "send_time": "09:00"
                },
                "kiosk": {
                    "password": self.kiosk_password_var.get(),
                    "fullscreen": True
                }
            }
            
            # Validate required fields
            if not new_config["email"]["email_address"]:
                messagebox.showerror("Error", "Email address is required!")
                return
            
            if not new_config["email"]["password"]:
                messagebox.showerror("Error", "Password is required!")
                return
            
            if not new_config["kiosk"]["password"]:
                messagebox.showerror("Error", "Kiosk password is required for security!")
                return
            
            self.save_config(new_config)
            messagebox.showinfo("Success", "Configuration saved successfully!")
            
        except ValueError as e:
            messagebox.showerror("Error", f"Invalid number: {str(e)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save configuration: {str(e)}")
    
    def test_imap_connection(self):
        """Test IMAP (receiving) connection"""
        try:
            # Save current configuration first
            self.save_configuration()
            
            # Import here to avoid circular imports
            import sys
            import os
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))
            
            from email_monitor import EmailMonitor
            
            # Load saved config
            saved_config = self.load_config()
            monitor = EmailMonitor(saved_config)
            
            if monitor.connect_imap():
                monitor.disconnect()
                messagebox.showinfo("Success", "IMAP connection successful!")
            else:
                messagebox.showerror("Error", "IMAP connection failed!")
        except Exception as e:
            messagebox.showerror("Error", f"IMAP connection test failed: {str(e)}")
    
    def test_smtp_connection(self):
        """Test SMTP (sending) connection"""
        try:
            # Save current configuration first
            self.save_configuration()
            
            # Import here to avoid circular imports
            import sys
            import os
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))
            
            from email_monitor import EmailMonitor
            
            # Load saved config
            saved_config = self.load_config()
            monitor = EmailMonitor(saved_config)
            
            if monitor.connect_smtp():
                monitor.disconnect()
                messagebox.showinfo("Success", "SMTP connection successful!")
            else:
                messagebox.showerror("Error", "SMTP connection failed!")
        except Exception as e:
            messagebox.showerror("Error", f"SMTP connection test failed: {str(e)}")
    
    def launch_main_app(self):
        """Launch the main application"""
        try:
            # Save configuration first
            self.save_configuration()
            
            # Close configuration window
            self.config_window.destroy()
            
            # Import here to avoid circular imports
            import sys
            import os
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))
            
            from gui_app import MaintenanceTrackerApp
            
            # Load the saved configuration
            config = self.load_config()
            
            # Launch main app
            app = MaintenanceTrackerApp(config)
            app.run()
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch application: {str(e)}")
