# weekly_report.py
import schedule
import time
import threading
from datetime import datetime, timedelta
from spreadsheet_handler import SpreadsheetHandler

class WeeklyReport:
    def __init__(self, config, email_monitor, spreadsheet_handler):
        self.config = config
        self.email_monitor = email_monitor
        self.spreadsheet_handler = spreadsheet_handler
        self.scheduler_running = False
        
    def generate_weekly_report(self):
        """Generate and send weekly report"""
        try:
            # Get last week's jobs
            weekly_jobs = self.spreadsheet_handler.get_weekly_jobs()
            
            if weekly_jobs.empty:
                print("No jobs found for last week")
                return
            
            # Generate report text
            report_date = datetime.now() - timedelta(days=7)
            report_text = f"Weekly Maintenance Report - Week of {report_date.strftime('%Y-%m-%d')}\n"
            report_text += "=" * 50 + "\n\n"
            
            # Summary statistics
            total_jobs = len(weekly_jobs)
            completed_jobs = len(weekly_jobs[weekly_jobs['Status'] == 'Completed'])
            pending_jobs = len(weekly_jobs[weekly_jobs['Status'] == 'Pending'])
            in_progress_jobs = len(weekly_jobs[weekly_jobs['Status'] == 'In Progress'])
            
            report_text += f"Summary:\n"
            report_text += f"- Total Jobs: {total_jobs}\n"
            report_text += f"- Completed: {completed_jobs}\n"
            report_text += f"- In Progress: {in_progress_jobs}\n"
            report_text += f"- Pending: {pending_jobs}\n\n"
            
            # Job details
            report_text += "Job Details:\n"
            report_text += "-" * 50 + "\n"
            
            for _, job in weekly_jobs.iterrows():
                report_text += f"\nJob #{job['Job Number']}:\n"
                report_text += f"  Room: {job['Room Number']}\n"
                report_text += f"  Issue: {job['Description']}\n"
                report_text += f"  Status: {job['Status']}\n"
                report_text += f"  Created: {job['Created Time']}\n"
                if pd.notna(job['Completion Time']):
                    report_text += f"  Completed: {job['Completion Time']}\n"
                report_text += f"  Active Time: {job['Total Active Time (seconds)']/3600:.2f} hours\n"
            
            # Send email
            recipient = self.config['notification']['weekly_report_email']
            subject = f"Weekly Maintenance Report - {datetime.now().strftime('%Y-%m-%d')}"
            
            success = self.email_monitor.send_email(recipient, subject, report_text)
            
            if success:
                print(f"Weekly report sent to {recipient}")
            else:
                print(f"Failed to send weekly report")
            
            return success
            
        except Exception as e:
            print(f"Error generating weekly report: {e}")
            return False
    
    def schedule_weekly_report(self):
        """Schedule the weekly report"""
        try:
            # Schedule for Monday mornings at 9:00 AM
            schedule.every().monday.at("09:00").do(self.generate_weekly_report)
            
            print("Weekly report scheduled for Monday at 09:00")
            
            # Run the scheduler in a separate thread
            self.scheduler_running = True
            scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
            scheduler_thread.start()
            
        except Exception as e:
            print(f"Error scheduling weekly report: {e}")
    
    def run_scheduler(self):
        """Run the scheduler continuously"""
        while self.scheduler_running:
            schedule.run_pending()
            time.sleep(60)  # Check every minute
    
    def stop_scheduler(self):
        """Stop the scheduler"""
        self.scheduler_running = False
        schedule.clear()