# gui_app.py
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import threading
import time
from datetime import datetime
import hashlib

class MaintenanceTrackerApp:
    def __init__(self, config):
        self.config = config
        # Get password from config
        self.password = self.config.get('kiosk', {}).get('password', 'admin123')
        
        # Import inside the method to avoid circular imports
        import sys
        import os
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        
        from email_monitor import EmailMonitor
        from job_manager import JobManager
        from spreadsheet_handler import SpreadsheetHandler
        from weekly_report import WeeklyReport
        
        self.email_monitor = EmailMonitor(config)
        self.job_manager = JobManager()
        self.spreadsheet_handler = SpreadsheetHandler(config)
        self.weekly_report = WeeklyReport(config, self.email_monitor, self.spreadsheet_handler)
        self.job_frames = {}  # Dictionary to store job frames
        self.resolution_widgets = {}  # Dictionary to store resolution text widgets
        
        # Monitoring thread
        self.monitor_thread = None
        self.monitoring_active = False
        self.new_jobs_queue = []  # Queue for new jobs to process
    
    def run(self):
        """Run the main application"""
        self.create_gui()
        self.start_email_monitoring()
        self.weekly_report.schedule_weekly_report()
        self.root.mainloop()
    
    def create_gui(self):
        """Create the kiosk-style GUI"""
        self.root = tk.Tk()
        self.root.title("Maintenance Job Tracker - Kiosk Mode")
        
        # Fullscreen mode
        self.root.attributes('-fullscreen', True)
        
        # Prevent minimizing (kiosk mode)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Bind escape keys
        self.root.bind("<Escape>", self.require_password_minimize)
        self.root.bind("<Control-m>", self.require_password_minimize)
        self.root.bind("<Alt-F4>", self.require_password_minimize)
        
        # Block Windows/Super key
        self.root.bind("<Super_L>", lambda e: "break")
        self.root.bind("<Super_R>", lambda e: "break")
        
        # Configure styles
        style = ttk.Style()
        style.theme_use('clam')
        
        # Configure colors
        self.root.configure(bg='#2c3e50')
        
        # Title bar
        title_frame = tk.Frame(self.root, bg='#34495e', height=80)
        title_frame.pack(fill='x')
        title_frame.pack_propagate(False)
        
        # Title
        title_label = tk.Label(title_frame, 
                              text="🚧 MAINTENANCE JOB TRACKER", 
                              font=('Arial', 24, 'bold'),
                              bg='#34495e',
                              fg='#ecf0f1')
        title_label.pack(side=tk.LEFT, padx=30, pady=20)
        
        # Status and control buttons
        control_frame = tk.Frame(title_frame, bg='#34495e')
        control_frame.pack(side=tk.RIGHT, padx=30, pady=20)
        
        self.status_label = tk.Label(control_frame, 
                                    text="🟢 Connected", 
                                    font=('Arial', 12),
                                    bg='#34495e',
                                    fg='#2ecc71')
        self.status_label.pack(side=tk.LEFT, padx=10)
        
        refresh_btn = tk.Button(control_frame, 
                               text="🔄 Refresh", 
                               command=self.refresh_jobs,
                               font=('Arial', 10, 'bold'),
                               bg='#3498db',
                               fg='white',
                               padx=15,
                               pady=5,
                               relief='raised',
                               borderwidth=2)
        refresh_btn.pack(side=tk.LEFT, padx=5)
        
        # NEW: Create Job button
        create_btn = tk.Button(control_frame, 
                              text="➕ Create Job", 
                              command=self.create_manual_job,
                              font=('Arial', 10, 'bold'),
                              bg='#f39c12',
                              fg='white',
                              padx=15,
                              pady=5,
                              relief='raised',
                              borderwidth=2)
        create_btn.pack(side=tk.LEFT, padx=5)
        
        email_btn = tk.Button(control_frame, 
                             text="📧 Check Email", 
                             command=self.manual_email_check,
                             font=('Arial', 10, 'bold'),
                             bg='#9b59b6',
                             fg='white',
                             padx=15,
                             pady=5,
                             relief='raised',
                             borderwidth=2)
        email_btn.pack(side=tk.LEFT, padx=5)
        
        report_btn = tk.Button(control_frame, 
                              text="📊 Send Report", 
                              command=self.manual_weekly_report,
                              font=('Arial', 10, 'bold'),
                              bg='#e74c3c',
                              fg='white',
                              padx=15,
                              pady=5,
                              relief='raised',
                              borderwidth=2)
        report_btn.pack(side=tk.LEFT, padx=5)
        
        exit_btn = tk.Button(control_frame, 
                            text="🔒 Exit", 
                            command=self.on_close,
                            font=('Arial', 10, 'bold'),
                            bg='#95a5a6',
                            fg='white',
                            padx=15,
                            pady=5,
                            relief='raised',
                            borderwidth=2)
        exit_btn.pack(side=tk.LEFT, padx=5)
        
        # Main jobs container
        main_container = tk.Frame(self.root, bg='#2c3e50')
        main_container.pack(fill='both', expand=True, padx=20, pady=20)
        
        # Jobs grid frame
        self.jobs_grid_frame = tk.Frame(main_container, bg='#2c3e50')
        self.jobs_grid_frame.pack(fill='both', expand=True)
        
        # Configure grid for jobs - 6 COLUMNS at 60% width
        for i in range(6):
            self.jobs_grid_frame.columnconfigure(i, weight=1, uniform='col')
        
        # Initial refresh
        self.refresh_jobs()
        
        # Start a timer to check for new jobs in the queue
        self.check_new_jobs_queue()
        
        # Add footer
        footer_frame = tk.Frame(self.root, bg='#34495e', height=40)
        footer_frame.pack(fill='x')
        footer_frame.pack_propagate(False)
        
        footer_label = tk.Label(footer_frame, 
                               text="© 2024 Maintenance Tracker - Kiosk Mode | Press ESC and enter password to minimize/exit",
                               font=('Arial', 9),
                               bg='#34495e',
                               fg='#bdc3c7')
        footer_label.pack(pady=10)
    
    def create_manual_job(self):
        """Open dialog to manually create a job"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Create New Job")
        dialog.geometry("500x600")
        dialog.configure(bg='#2c3e50')
        dialog.transient(self.root)
        dialog.grab_set()
        
        # Center the dialog
        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = (dialog.winfo_screenwidth() // 2) - (width // 2)
        y = (dialog.winfo_screenheight() // 2) - (height // 2)
        dialog.geometry(f'{width}x{height}+{x}+{y}')
        
        # Title
        title_label = tk.Label(dialog, 
                              text="Create New Maintenance Job",
                              font=('Arial', 16, 'bold'),
                              bg='#2c3e50',
                              fg='#ecf0f1')
        title_label.pack(pady=20)
        
        # Form frame
        form_frame = tk.Frame(dialog, bg='#34495e', padx=20, pady=20)
        form_frame.pack(fill='both', expand=True, padx=20, pady=10)
        
        # Room Number
        tk.Label(form_frame, 
                text="Room Number:",
                font=('Arial', 11, 'bold'),
                bg='#34495e',
                fg='#ecf0f1').grid(row=0, column=0, sticky='w', pady=10)
        
        room_var = tk.StringVar()
        room_entry = tk.Entry(form_frame, 
                             textvariable=room_var,
                             font=('Arial', 11),
                             width=30)
        room_entry.grid(row=0, column=1, pady=10, padx=(10, 0))
        
        # Issue
        tk.Label(form_frame, 
                text="Issue Description:",
                font=('Arial', 11, 'bold'),
                bg='#34495e',
                fg='#ecf0f1').grid(row=1, column=0, sticky='nw', pady=10)
        
        issue_text = tk.Text(form_frame, 
                           height=8,
                           width=30,
                           font=('Arial', 10),
                           wrap='word')
        issue_text.grid(row=1, column=1, pady=10, padx=(10, 0), sticky='nsew')
        
        # Add scrollbar for issue text
        issue_scroll = tk.Scrollbar(form_frame, command=issue_text.yview)
        issue_text.config(yscrollcommand=issue_scroll.set)
        issue_scroll.grid(row=1, column=2, sticky='ns', pady=10)
        
        # Email Information (for completion emails)
        tk.Label(form_frame, 
                text="Email Information:",
                font=('Arial', 11, 'bold'),
                bg='#34495e',
                fg='#ecf0f1').grid(row=2, column=0, sticky='w', pady=10)
        
        email_frame = tk.Frame(form_frame, bg='#34495e')
        email_frame.grid(row=2, column=1, columnspan=2, sticky='w', pady=10, padx=(10, 0))
        
        tk.Label(email_frame, 
                text="Sender Email:",
                font=('Arial', 10),
                bg='#34495e',
                fg='#bdc3c7').pack(anchor='w')
        
        sender_var = tk.StringVar(value="manual@maintenance.com")
        sender_entry = tk.Entry(email_frame, 
                               textvariable=sender_var,
                               font=('Arial', 10),
                               width=35)
        sender_entry.pack(fill='x', pady=(2, 10))
        
        tk.Label(email_frame, 
                text="Email Subject:",
                font=('Arial', 10),
                bg='#34495e',
                fg='#bdc3c7').pack(anchor='w')
        
        subject_var = tk.StringVar(value="Manual Maintenance Request")
        subject_entry = tk.Entry(email_frame, 
                                textvariable=subject_var,
                                font=('Arial', 10),
                                width=35)
        subject_entry.pack(fill='x', pady=(2, 0))
        
        # NEW: Send completion email checkbox
        send_completion_var = tk.BooleanVar(value=True)
        completion_check = tk.Checkbutton(form_frame,
                                         text="Send completion email when job is finished",
                                         variable=send_completion_var,
                                         font=('Arial', 10),
                                         bg='#34495e',
                                         fg='#ecf0f1',
                                         selectcolor='#2c3e50',
                                         activebackground='#34495e',
                                         activeforeground='#ecf0f1')
        completion_check.grid(row=3, column=0, columnspan=3, sticky='w', pady=20, padx=(0, 0))
        
        # Button frame
        button_frame = tk.Frame(dialog, bg='#2c3e50')
        button_frame.pack(pady=20)
        
        def create_job():
            """Create the job from form data"""
            room = room_var.get().strip()
            issue = issue_text.get("1.0", "end-1c").strip()
            sender = sender_var.get().strip()
            subject = subject_var.get().strip()
            send_completion = send_completion_var.get()
            
            if not room:
                messagebox.showerror("Error", "Room number is required!", parent=dialog)
                return
            
            if not issue:
                messagebox.showerror("Error", "Issue description is required!", parent=dialog)
                return
            
            if not sender:
                sender = "manual@maintenance.com"
            
            if not subject:
                subject = "Manual Maintenance Request"
            
            # Create email info dictionary
            email_info = {
                'sender': sender,
                'subject': subject,
                'body': f"Room Number: {room}\nIssue: {issue}",
                'send_completion_email': send_completion  # Store whether to send completion email
            }
            
            # Create the job
            job = self.job_manager.create_job(room, issue, email_info)
            
            # Add to spreadsheet
            self.spreadsheet_handler.add_job(job)
            
            # Add to queue for GUI update
            self.new_jobs_queue.append(job.job_number)
            
            # Update status
            self.status_label.config(text=f"✅ Manual job #{job.job_number} created", fg='#2ecc71')
            self.root.after(3000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
            
            # Close dialog
            dialog.destroy()
            
            # Show success message
            messagebox.showinfo("Success", 
                              f"Job #{job.job_number} created successfully!\n\n"
                              f"Room: {room}\n"
                              f"Completion email will{' ' if send_completion else ' NOT '}be sent when job is finished.",
                              parent=self.root)
        
        create_button = tk.Button(button_frame,
                                 text="Create Job",
                                 command=create_job,
                                 font=('Arial', 11, 'bold'),
                                 bg='#27ae60',
                                 fg='white',
                                 padx=20,
                                 pady=10,
                                 relief='raised',
                                 borderwidth=2)
        create_button.pack(side=tk.LEFT, padx=10)
        
        cancel_button = tk.Button(button_frame,
                                 text="Cancel",
                                 command=dialog.destroy,
                                 font=('Arial', 11, 'bold'),
                                 bg='#95a5a6',
                                 fg='white',
                                 padx=20,
                                 pady=10,
                                 relief='raised',
                                 borderwidth=2)
        cancel_button.pack(side=tk.LEFT, padx=10)
        
        # Configure grid weights
        form_frame.grid_columnconfigure(1, weight=1)
        form_frame.grid_rowconfigure(1, weight=1)
    
    def require_password_minimize(self, event=None):
        """Require password to minimize or exit fullscreen"""
        # Show password dialog
        password = simpledialog.askstring("Password Required", 
                                         "Enter password to minimize/exit:",
                                         show='*',
                                         parent=self.root)
        
        if password == self.password:
            # Toggle fullscreen
            current_state = self.root.attributes('-fullscreen')
            self.root.attributes('-fullscreen', not current_state)
            
            if not current_state:  # If we're exiting fullscreen
                self.root.geometry("1024x768")  # Set a reasonable window size
        else:
            messagebox.showerror("Access Denied", "Incorrect password!")
            return "break"  # Prevent the key from doing anything
    
    def refresh_jobs(self):
        """Refresh the job grid with 6 COLUMNS at 60% width"""
        # Clear existing job frames and resolution widgets
        for widget in self.jobs_grid_frame.winfo_children():
            widget.destroy()
        self.resolution_widgets.clear()
        
        # Get open jobs
        open_jobs = self.job_manager.get_open_jobs()
        
        # If no jobs, show simple message directly in the grid frame
        if not open_jobs:
            no_jobs_label = tk.Label(self.jobs_grid_frame,
                                    text="📭 No Active Maintenance Jobs\n\nNew jobs will appear automatically when received",
                                    font=('Arial', 18),
                                    bg='#2c3e50',
                                    fg='#bdc3c7')
            no_jobs_label.pack(expand=True, fill='both')
            return
        
        # Sort by job number
        open_jobs.sort(key=lambda x: int(x.job_number))
        
        # Display jobs in grid (6 COLUMNS at 60% width)
        for i, job in enumerate(open_jobs):
            col = i % 6  # 6 COLUMNS
            row = i // 6
            
            # Create job card frame - 60% WIDTH (210px) and TALLER (750px)
            job_frame = tk.Frame(self.jobs_grid_frame, 
                                bg='#ecf0f1',
                                relief='raised',
                                borderwidth=2)
            job_frame.grid(row=row, column=col, padx=4, pady=4, sticky='nsew')
            job_frame.grid_propagate(False)
            job_frame.configure(width=210, height=750)  # 60% WIDTH and TALLER
            
            # Store reference
            self.job_frames[job.job_number] = job_frame
            
            # Job header with job number on left and remove button on right
            header_frame = tk.Frame(job_frame, bg='#3498db', height=30)
            header_frame.pack(fill='x')
            header_frame.pack_propagate(False)
            
            # Left side: Job number
            left_header = tk.Frame(header_frame, bg='#3498db')
            left_header.pack(side=tk.LEFT, fill='both', expand=True)
            
            job_number_label = tk.Label(left_header, 
                                       text=f"JOB #{job.job_number}",
                                       font=('Arial', 10, 'bold'),
                                       bg='#3498db',
                                       fg='white')
            job_number_label.pack(pady=6)
            
            # Check if this job should send completion email
            send_completion = job.email_info.get('send_completion_email', True)
            if not send_completion:
                no_email_label = tk.Label(left_header,
                                         text="(No completion email)",
                                         font=('Arial', 7, 'italic'),
                                         bg='#3498db',
                                         fg='#ffffcc')
                no_email_label.pack()
            
            # Right side: Remove button (top right corner)
            right_header = tk.Frame(header_frame, bg='#3498db')
            right_header.pack(side=tk.RIGHT, padx=5, pady=2)
            
            # Remove job button - compact with X icon
            remove_btn = tk.Button(right_header,
                                  text="✕",
                                  command=lambda jn=job.job_number: self.remove_job(jn),
                                  font=('Arial', 8, 'bold'),
                                  bg='#e74c3c',
                                  fg='white',
                                  width=2,
                                  height=1,
                                  relief='flat',
                                  borderwidth=1,
                                  padx=0,
                                  pady=0)
            remove_btn.pack()
            
            # Job info - compact for narrow width
            info_frame = tk.Frame(job_frame, bg='#ecf0f1', padx=6, pady=6)
            info_frame.pack(fill='both', expand=True)
            
            # Room number - compact
            room_frame = tk.Frame(info_frame, bg='#ecf0f1')
            room_frame.pack(fill='x', pady=2)
            
            tk.Label(room_frame, 
                    text="Room:", 
                    font=('Arial', 8, 'bold'),
                    bg='#ecf0f1').pack(side=tk.LEFT)
            
            tk.Label(room_frame, 
                    text=job.room_number, 
                    font=('Arial', 9),
                    bg='#ecf0f1').pack(side=tk.LEFT, padx=2)
            
            # Issue - compact for 60% width
            tk.Label(info_frame, 
                    text="Issue:", 
                    font=('Arial', 8, 'bold'),
                    bg='#ecf0f1').pack(anchor='nw', pady=(3, 0))
            
            issue_frame = tk.Frame(info_frame, height=90, bg='#ecf0f1')
            issue_frame.pack(fill='x', pady=(1, 0))
            
            # Create compact scrollable text widget for issue
            issue_text = tk.Text(issue_frame, 
                                height=5,
                                width=22,  # Narrower for 60% width
                                font=('Arial', 7),
                                bg='#ffffff',
                                relief='sunken',
                                wrap='word')
            issue_text.insert('1.0', job.issue)
            issue_text.config(state='disabled')
            
            # Add scrollbar for issue
            issue_scroll = tk.Scrollbar(issue_frame, command=issue_text.yview)
            issue_text.config(yscrollcommand=issue_scroll.set)
            issue_text.pack(side=tk.LEFT, fill='both', expand=True)
            issue_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            
            # Resolution field - editable text box (compact)
            tk.Label(info_frame, 
                    text="Resolution:", 
                    font=('Arial', 8, 'bold'),
                    bg='#ecf0f1').pack(anchor='nw', pady=(5, 0))
            
            resolution_frame = tk.Frame(info_frame, height=120, bg='#ecf0f1')
            resolution_frame.pack(fill='x', pady=(1, 0), expand=True)
            
            # Create compact scrollable text widget for resolution
            resolution_text = tk.Text(resolution_frame, 
                                     height=6,
                                     width=22,  # Narrower for 60% width
                                     font=('Arial', 7),
                                     bg='#ffffcc',  # Light yellow background for editing
                                     relief='sunken',
                                     wrap='word')
            resolution_text.insert('1.0', job.resolution if job.resolution else "")
            
            # Add scrollbar for resolution
            resolution_scroll = tk.Scrollbar(resolution_frame, command=resolution_text.yview)
            resolution_text.config(yscrollcommand=resolution_scroll.set)
            resolution_text.pack(side=tk.LEFT, fill='both', expand=True)
            resolution_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            
            # Store resolution widget reference
            self.resolution_widgets[job.job_number] = resolution_text
            
            # Save Resolution button - compact
            save_res_btn = tk.Button(info_frame,
                                   text="💾 Save",
                                   command=lambda jn=job.job_number: self.save_resolution(jn),
                                   font=('Arial', 7, 'bold'),
                                   bg='#9b59b6',
                                   fg='white',
                                   padx=6,
                                   pady=2)
            save_res_btn.pack(pady=(4, 6))
            
            # Created time - smaller font
            time_frame = tk.Frame(info_frame, bg='#ecf0f1')
            time_frame.pack(fill='x', pady=2)
            
            created_time = job.created_time.strftime('%Y-%m-%d %H:%M') if job.created_time else "N/A"
            tk.Label(time_frame, 
                    text=f"Created: {created_time}", 
                    font=('Arial', 6),
                    bg='#ecf0f1',
                    fg='#7f8c8d').pack()
            
            # Status indicator - compact
            status_color = {
                'Pending': '#f39c12',    # Orange
                'In Progress': '#2ecc71', # Green
                'Paused': '#e74c3c',      # Red
                'Completed': '#95a5a6'    # Gray
            }.get(job.status.value, '#95a5a6')
            
            status_frame = tk.Frame(info_frame, bg='#ecf0f1')
            status_frame.pack(fill='x', pady=3)
            
            tk.Label(status_frame, 
                    text="Status:", 
                    font=('Arial', 7, 'bold'),
                    bg='#ecf0f1').pack(side=tk.LEFT)
            
            status_circle = tk.Canvas(status_frame, width=8, height=8, bg='#ecf0f1', highlightthickness=0)
            status_circle.create_oval(1, 1, 7, 7, fill=status_color, outline=status_color)
            status_circle.pack(side=tk.LEFT, padx=2)
            
            tk.Label(status_frame, 
                    text=job.status.value, 
                    font=('Arial', 7),
                    bg='#ecf0f1').pack(side=tk.LEFT)
            
            # VERTICAL ACTION BUTTONS FRAME - ultra compact for 60% width
            button_frame = tk.Frame(job_frame, bg='#ecf0f1', pady=6)
            button_frame.pack(fill='x', padx=4)
            
            # Button colors
            start_color = '#27ae60' if job.status.value == "Pending" else '#95a5a6'
            pause_color = '#f39c12' if job.status.value == "In Progress" else '#95a5a6'
            resume_color = '#3498db' if job.status.value == "Paused" else '#95a5a6'
            complete_color = '#e74c3c' if job.status.value in ["In Progress", "Paused"] else '#95a5a6'
            
            # ULTRA COMPACT VERTICAL BUTTON LAYOUT for 60% width
            # Start button
            start_btn = tk.Button(button_frame, 
                                 text="▶ START",
                                 command=lambda jn=job.job_number: self.start_job(jn),
                                 font=('Arial', 7, 'bold'),
                                 bg=start_color,
                                 fg='white',
                                 padx=3,
                                 pady=2,
                                 relief='raised',
                                 borderwidth=1,
                                 state='normal' if job.status.value == "Pending" else 'disabled')
            start_btn.pack(fill='x', pady=1)
            
            # Pause button
            pause_btn = tk.Button(button_frame, 
                                 text="⏸ PAUSE",
                                 command=lambda jn=job.job_number: self.pause_job(jn),
                                 font=('Arial', 7, 'bold'),
                                 bg=pause_color,
                                 fg='white',
                                 padx=3,
                                 pady=2,
                                 relief='raised',
                                 borderwidth=1,
                                 state='normal' if job.status.value == "In Progress" else 'disabled')
            pause_btn.pack(fill='x', pady=1)
            
            # Resume button
            resume_btn = tk.Button(button_frame, 
                                  text="▶ RESUME",
                                  command=lambda jn=job.job_number: self.resume_job(jn),
                                  font=('Arial', 7, 'bold'),
                                  bg=resume_color,
                                  fg='white',
                                  padx=3,
                                  pady=2,
                                  relief='raised',
                                  borderwidth=1,
                                  state='normal' if job.status.value == "Paused" else 'disabled')
            resume_btn.pack(fill='x', pady=1)
            
            # Complete button
            complete_btn = tk.Button(button_frame, 
                                    text="✓ COMPLETE",
                                    command=lambda jn=job.job_number: self.complete_job(jn),
                                    font=('Arial', 7, 'bold'),
                                    bg=complete_color,
                                    fg='white',
                                    padx=3,
                                    pady=2,
                                    relief='raised',
                                    borderwidth=1,
                                    state='normal' if job.status.value in ["In Progress", "Paused"] else 'disabled')
            complete_btn.pack(fill='x', pady=1)
    
    def remove_job(self, job_number):
        """Remove a job from the system"""
        # Confirm removal
        response = messagebox.askyesno("Confirm Removal", 
                                      f"Are you sure you want to remove Job #{job_number}?\n\n"
                                      f"This action cannot be undone.",
                                      parent=self.root)
        
        if not response:
            return
        
        # Remove from job manager
        if self.job_manager.remove_job(job_number):
            # Remove from spreadsheet
            self.spreadsheet_handler.remove_job(job_number)
            
            # Clear references
            if job_number in self.job_frames:
                del self.job_frames[job_number]
            
            if job_number in self.resolution_widgets:
                del self.resolution_widgets[job_number]
            
            # Refresh display
            self.refresh_jobs()
            
            # Update status
            self.status_label.config(text=f"🗑️ Job #{job_number} removed", fg='#e74c3c')
            self.root.after(3000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
        else:
            messagebox.showerror("Error", f"Failed to remove Job #{job_number}", parent=self.root)
    
    def check_new_jobs_queue(self):
        """Check the queue for new jobs and update GUI"""
        try:
            # Process any jobs in the queue
            if self.new_jobs_queue:
                print(f"📋 Processing {len(self.new_jobs_queue)} jobs from queue")
                # Just refresh the display since jobs are already saved
                self.refresh_jobs()
                
                # Update status for the most recent job
                if self.new_jobs_queue:
                    job_num = self.new_jobs_queue[-1]
                    self.status_label.config(text=f"📨 New job #{job_num} received", fg='#9b59b6')
                    # Clear the queue
                    self.new_jobs_queue.clear()
        
        except Exception as e:
            print(f"❌ Error checking job queue: {e}")
        
        # Schedule next check
        self.root.after(2000, self.check_new_jobs_queue)  # Check every 2 seconds
    
    def save_resolution(self, job_number):
        """Save resolution text for a job"""
        if job_number not in self.resolution_widgets:
            print(f"❌ Resolution widget not found for job #{job_number}")
            return
        
        resolution_text = self.resolution_widgets[job_number].get("1.0", "end-1c").strip()
        
        if self.job_manager.update_job_resolution(job_number, resolution_text):
            # Update spreadsheet in real-time
            job = self.job_manager.get_job(job_number)
            self.spreadsheet_handler.update_job_resolution_only(job_number, resolution_text)
            
            self.status_label.config(text=f"💾 Resolution saved for job #{job_number}", fg='#9b59b6')
            self.root.after(3000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
        else:
            self.status_label.config(text=f"❌ Failed to save resolution", fg='#e74c3c')
            self.root.after(3000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
    
    def start_job(self, job_number):
        """Start the selected job"""
        if self.job_manager.update_job_status(job_number, 'start'):
            # Update spreadsheet in real-time
            job = self.job_manager.get_job(job_number)
            self.spreadsheet_handler.update_job(job)
            
            self.refresh_jobs()
            self.status_label.config(text=f"✅ Job #{job_number} started", fg='#2ecc71')
            self.root.after(3000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
    
    def pause_job(self, job_number):
        """Pause the selected job"""
        if self.job_manager.update_job_status(job_number, 'pause'):
            # Update spreadsheet in real-time
            job = self.job_manager.get_job(job_number)
            self.spreadsheet_handler.update_job(job)
            
            self.refresh_jobs()
            self.status_label.config(text=f"⏸ Job #{job_number} paused", fg='#f39c12')
            self.root.after(3000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
    
    def resume_job(self, job_number):
        """Resume the selected job"""
        if self.job_manager.update_job_status(job_number, 'resume'):
            # Update spreadsheet in real-time
            job = self.job_manager.get_job(job_number)
            self.spreadsheet_handler.update_job(job)
            
            self.refresh_jobs()
            self.status_label.config(text=f"▶ Job #{job_number} resumed", fg='#3498db')
            self.root.after(3000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
    
    def complete_job(self, job_number):
        """Complete the selected job"""
        # Save resolution first if there's text
        resolution_text = ""
        if job_number in self.resolution_widgets:
            resolution_text = self.resolution_widgets[job_number].get("1.0", "end-1c").strip()
            if resolution_text:
                self.job_manager.update_job_resolution(job_number, resolution_text)
        
        if self.job_manager.update_job_status(job_number, 'complete'):
            # Update spreadsheet in real-time
            job = self.job_manager.get_job(job_number)
            self.spreadsheet_handler.update_job(job)
            
            # Check if we should send completion email
            send_completion = job.email_info.get('send_completion_email', True)
            
            if send_completion:
                # Send completion email as REPLY to original with job description and resolution
                self.send_completion_email(job, resolution_text)
            else:
                print(f"📧 Completion email SKIPPED for job #{job.job_number} (user preference)")
                self.status_label.config(text=f"✅ Job #{job_number} completed (no email)", fg='#2ecc71')
                self.root.after(3000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
            
            self.refresh_jobs()
    
    def send_completion_email(self, job, resolution_text):
        """Send job completion email as REPLY to original request with CC"""
        # Get the original email info from the job
        original_email_info = {
            'sender': job.email_info.get('sender', ''),
            'subject': job.email_info.get('subject', 'Maintenance Request')
        }
        
        print(f"📧 Sending completion email for job #{job.job_number}")
        print(f"📧 Original sender: {original_email_info['sender']}")
        print(f"📧 Original subject: {original_email_info['subject']}")
        print(f"📧 Resolution: {resolution_text[:50]}...")
        
        success = self.email_monitor.send_completion_email(job, original_email_info, resolution_text)
        
        if success:
            print(f"✅ Completion email sent as REPLY for job #{job.job_number} with CC")
            self.status_label.config(text=f"📧 Completion email sent for job #{job.job_number}", fg='#9b59b6')
        else:
            print(f"❌ Failed to send completion email for job #{job.job_number}")
            self.status_label.config(text=f"❌ Failed to send completion email", fg='#e74c3c')
        
        self.root.after(5000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
    
    def require_password_exit(self):
        """Require password to exit the application"""
        password = simpledialog.askstring("Password Required", 
                                         "Enter password to exit application:",
                                         show='*',
                                         parent=self.root)
        
        if password == self.password:
            return True
        else:
            messagebox.showerror("Access Denied", "Incorrect password!")
            return False
    
    def on_close(self):
        """Handle window close event - now requires password"""
        # First ask for password
        if not self.require_password_exit():
            return  # Don't proceed if password is incorrect
        
        # Once password is verified, show confirmation
        response = messagebox.askyesno("Confirm Exit", 
                                      "Are you sure you want to exit?\n\nThis will stop monitoring for new jobs.",
                                      parent=self.root)
        if response:
            self.monitoring_active = False
            if self.monitor_thread:
                self.monitor_thread.join(timeout=2)
            self.root.destroy()
    
    def start_email_monitoring(self):
        """Start monitoring emails in a separate thread"""
        self.monitoring_active = True
        self.monitor_thread = threading.Thread(target=self.email_monitoring_loop, daemon=True)
        self.monitor_thread.start()
        print("✅ Email monitoring started")
    
    def email_monitoring_loop(self):
        """Main email monitoring loop - optimized for speed"""
        print("🔍 Email monitoring loop started...")
        
        # Wait a moment for GUI to initialize
        time.sleep(2)
        
        while self.monitoring_active:
            try:
                print("🔄 Checking for new emails...")
                # Check for new emails using fast method
                new_jobs = self.email_monitor.check_new_emails_fast()
                
                if new_jobs:
                    print(f"📨 Found {len(new_jobs)} new job(s)")
                
                for job_info in new_jobs:
                    # Create new job
                    job = self.job_manager.create_job(
                        job_info['room_number'],
                        job_info['issue'],
                        job_info
                    )
                    
                    # Add to spreadsheet
                    self.spreadsheet_handler.add_job(job)
                    
                    # Add to queue for GUI update
                    self.new_jobs_queue.append(job.job_number)
                
                # Short sleep to prevent CPU overuse
                time.sleep(10)
                
            except Exception as e:
                print(f"❌ Error in email monitoring loop: {e}")
                time.sleep(30)  # Longer sleep on error
    
    def manual_email_check(self):
        """Manually check for new emails"""
        self.status_label.config(text="📧 Checking for new emails...", fg='#9b59b6')
        try:
            new_jobs = self.email_monitor.check_new_emails_fast()
            if new_jobs:
                self.status_label.config(text=f"📨 Found {len(new_jobs)} new job(s)", fg='#2ecc71')
                # Process new jobs
                for job_info in new_jobs:
                    job = self.job_manager.create_job(
                        job_info['room_number'],
                        job_info['issue'],
                        job_info
                    )
                    self.spreadsheet_handler.add_job(job)
                    self.new_jobs_queue.append(job.job_number)
            else:
                self.status_label.config(text="📭 No new emails found", fg='#f39c12')
        except Exception as e:
            self.status_label.config(text=f"❌ Error checking email: {e}", fg='#e74c3c')
        
        self.root.after(3000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
    
    def manual_weekly_report(self):
        """Manually trigger weekly report"""
        self.status_label.config(text="📊 Generating weekly report...", fg='#9b59b6')
        try:
            self.weekly_report.send_weekly_report()
            self.status_label.config(text="✅ Weekly report sent successfully", fg='#2ecc71')
        except Exception as e:
            self.status_label.config(text=f"❌ Error sending report: {e}", fg='#e74c3c')
        
        self.root.after(5000, lambda: self.status_label.config(text="🟢 Connected", fg='#2ecc71'))
