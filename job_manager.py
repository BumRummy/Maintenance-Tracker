# job_manager.py
import json
import os
from datetime import datetime
from enum import Enum
import re

class JobStatus(Enum):
    PENDING = "Pending"
    IN_PROGRESS = "In Progress"
    PAUSED = "Paused"
    COMPLETED = "Completed"

class Job:
    def __init__(self, job_number, room_number, issue, email_info):
        self.job_number = job_number
        self.room_number = room_number
        self.issue = issue
        self.email_info = email_info
        self.resolution = ""  # NEW: Resolution field
        
        self.status = JobStatus.PENDING
        self.created_time = datetime.now()
        self.start_time = None
        self.pause_times = []
        self.resume_times = []
        self.completion_time = None
        
        self.total_paused_time = 0  # in seconds
        self.elapsed_time = 0  # in seconds
    
    def start_job(self):
        if self.status == JobStatus.PENDING:
            self.status = JobStatus.IN_PROGRESS
            self.start_time = datetime.now()
            return True
        return False
    
    def pause_job(self):
        if self.status == JobStatus.IN_PROGRESS:
            self.status = JobStatus.PAUSED
            self.pause_times.append(datetime.now())
            return True
        return False
    
    def resume_job(self):
        if self.status == JobStatus.PAUSED:
            self.status = JobStatus.IN_PROGRESS
            self.resume_times.append(datetime.now())
            # Calculate paused time if we have matching pause/resume pairs
            if len(self.resume_times) <= len(self.pause_times):
                pause_time = self.pause_times[-1]
                resume_time = self.resume_times[-1]
                self.total_paused_time += (resume_time - pause_time).total_seconds()
            return True
        return False
    
    def complete_job(self):
        if self.status in [JobStatus.IN_PROGRESS, JobStatus.PAUSED]:
            self.status = JobStatus.COMPLETED
            self.completion_time = datetime.now()
            
            # If paused, calculate final paused time
            if self.status == JobStatus.PAUSED and len(self.pause_times) > len(self.resume_times):
                self.total_paused_time += (datetime.now() - self.pause_times[-1]).total_seconds()
            
            # Calculate total elapsed time
            if self.start_time:
                total_time = (self.completion_time - self.start_time).total_seconds()
                self.elapsed_time = total_time - self.total_paused_time
            
            return True
        return False
    
    def set_resolution(self, resolution_text):
        """Set the resolution text for the job"""
        self.resolution = resolution_text[:1000]  # Limit length
    
    def to_dict(self):
        return {
            'job_number': self.job_number,
            'room_number': self.room_number,
            'issue': self.issue,
            'resolution': self.resolution,  # NEW
            'status': self.status.value,
            'created_time': self.created_time.isoformat() if self.created_time else None,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'completion_time': self.completion_time.isoformat() if self.completion_time else None,
            'total_paused_time': self.total_paused_time,
            'elapsed_time': self.elapsed_time,
            'email_sender': self.email_info.get('sender', ''),
            'email_subject': self.email_info.get('subject', '')
        }

class JobManager:
    def __init__(self, storage_file='jobs.json'):
        self.storage_file = storage_file
        self.jobs = {}
        self.next_job_number = 1
        self.load_jobs()
    
    def load_jobs(self):
        """Load jobs from storage file"""
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'r') as f:
                    data = json.load(f)
                    self.jobs = {}
                    for job_num, job_data in data.items():
                        job = Job(
                            job_data['job_number'],
                            job_data['room_number'],
                            job_data['issue'],
                            {
                                'sender': job_data.get('email_sender', ''),
                                'subject': job_data.get('email_subject', '')
                            }
                        )
                        job.status = JobStatus(job_data['status'])
                        job.created_time = datetime.fromisoformat(job_data['created_time']) if job_data['created_time'] else None
                        job.start_time = datetime.fromisoformat(job_data['start_time']) if job_data['start_time'] else None
                        job.completion_time = datetime.fromisoformat(job_data['completion_time']) if job_data['completion_time'] else None
                        job.total_paused_time = job_data['total_paused_time']
                        job.elapsed_time = job_data['elapsed_time']
                        job.resolution = job_data.get('resolution', '')  # Load resolution
                        
                        self.jobs[job_num] = job
                        job_num_int = int(job_num)
                        if job_num_int >= self.next_job_number:
                            self.next_job_number = job_num_int + 1
            except Exception as e:
                print(f"❌ Error loading jobs: {e}")
                self.jobs = {}
                self.next_job_number = 1
    
    def save_jobs(self):
        """Save jobs to storage file"""
        try:
            data = {}
            for job_num, job in self.jobs.items():
                data[job_num] = job.to_dict()
            
            with open(self.storage_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"❌ Error saving jobs: {e}")
    
    def extract_job_info_from_email(self, email_body):
        """
        Extract job information from email body with STRICT format:
        Line 1: Room Number: XXX (or Room: XXX)
        Line 2: Issue: YYY
        Read issue text until a blank line, then stop.
        Returns (room_number, issue, is_valid_format, error_message)
        """
        if not email_body or not isinstance(email_body, str):
            return None, None, False, "Empty or invalid email body"
        
        # Split into lines (preserve empty lines)
        lines = email_body.split('\n')
        
        # Find Line 1 (first non-empty line)
        line1 = None
        line1_index = -1
        for i, line in enumerate(lines):
            if line.strip():  # Found first non-empty line
                line1 = line.strip()
                line1_index = i
                break
        
        if line1 is None:
            return None, None, False, "Email is empty"
        
        # Check Line 1 format: Must start with "Room Number:" or "Room:"
        line1_patterns = [
            r'^Room\s+Number\s*:\s*(\d+)',
            r'^Room\s*(?:#\s*)?:\s*(\d+)'
        ]
        
        room_number = None
        for pattern in line1_patterns:
            match = re.match(pattern, line1, re.IGNORECASE)
            if match:
                room_number = match.group(1)
                break
        
        if not room_number:
            return None, None, False, f"Line 1 doesn't match 'Room Number: XXX' or 'Room: XXX' format. Got: '{line1}'"
        
        # Find Line 2 (next non-empty line after Line 1)
        line2 = None
        line2_index = -1
        for i in range(line1_index + 1, len(lines)):
            if lines[i].strip():  # Found next non-empty line
                line2 = lines[i].strip()
                line2_index = i
                break
        
        if line2 is None:
            return None, None, False, "No Line 2 found (Issue line)"
        
        # Check Line 2 format: Must start with "Issue:"
        if not re.match(r'^Issue\s*:', line2, re.IGNORECASE):
            return None, None, False, f"Line 2 doesn't start with 'Issue:'. Got: '{line2}'"
        
        # Extract issue text from Line 2 (remove "Issue:" prefix)
        issue_match = re.match(r'^Issue\s*:\s*(.+)', line2, re.IGNORECASE)
        if not issue_match:
            return None, None, False, f"Could not extract issue from Line 2. Got: '{line2}'"
        
        # Start with the issue text from Line 2
        issue_parts = [issue_match.group(1).strip()]
        
        # Continue reading following lines until we hit a blank line
        for i in range(line2_index + 1, len(lines)):
            current_line = lines[i].strip()
            
            # Stop at first blank line (end of issue description)
            if not current_line:
                break
            
            # Add this line to the issue
            issue_parts.append(current_line)
        
        # Combine all issue lines
        full_issue = ' '.join(issue_parts)
        
        # Clean up: remove extra spaces, limit length
        full_issue = ' '.join(full_issue.split())
        
        if len(full_issue.strip()) < 3:
            return None, None, False, "Issue description too short (less than 3 characters)"
        
        # Limit to 500 characters
        full_issue = full_issue[:500]
        
        return room_number, full_issue, True, "Valid strict format"
    
    def validate_job_format(self, room_number, issue, email_body):
        """
        Validate that the job follows the required STRICT format.
        Uses the strict extraction method.
        Returns (is_valid, error_message)
        """
        if not room_number or not room_number.isdigit():
            return False, f"Room number must be a number (got: '{room_number}')"
        
        if not issue or len(issue.strip()) < 3:
            return False, f"Issue description must be at least 3 characters (got: '{issue}')"
        
        # Use the strict extraction to validate against email body
        extracted_room, extracted_issue, is_valid, error_msg = self.extract_job_info_from_email(email_body)
        
        if not is_valid:
            return False, f"Email format issue: {error_msg}"
        
        # The extracted room number should match the provided room number
        if extracted_room and extracted_room != room_number:
            print(f"⚠️  Room number mismatch: extracted '{extracted_room}' vs provided '{room_number}'")
            # We'll still accept it since the provided one might be correct
        
        return True, "Valid strict format"
    
    def create_job(self, room_number, issue, email_info):
        """Create a new job"""
        email_body = email_info.get('body', '')
        
        # For debugging
        print(f"\n{'='*60}")
        print(f"Creating Job")
        print(f"{'='*60}")
        print(f"Room: {room_number}")
        print(f"Issue preview: {issue[:100]}...")
        print(f"Email body lines:")
        lines = email_body.split('\n')
        for i, line in enumerate(lines[:10]):  # Show first 10 lines
            print(f"  [{i+1:2}] '{line}'")
        print(f"{'='*60}")
        
        # Validate the job data
        is_valid, error_msg = self.validate_job_format(room_number, issue, email_body)
        
        if not is_valid:
            print(f"⚠️  Job validation warning: {error_msg}")
            print(f"   Creating job anyway with: Room {room_number}, Issue: {issue[:50]}...")
        else:
            print(f"✅ Job format validation passed")
        
        job_number = str(self.next_job_number)
        job = Job(job_number, room_number, issue, email_info)
        self.jobs[job_number] = job
        self.next_job_number += 1
        self.save_jobs()
        
        print(f"✅ Job #{job_number} created successfully")
        print(f"{'='*60}\n")
        return job
    
    def get_job(self, job_number):
        """Get a job by number"""
        return self.jobs.get(str(job_number))
    
    def get_all_jobs(self):
        """Get all jobs"""
        return list(self.jobs.values())
    
    def get_open_jobs(self):
        """Get all non-completed jobs"""
        return [job for job in self.jobs.values() if job.status != JobStatus.COMPLETED]
    
    def get_completed_jobs(self):
        """Get all completed jobs"""
        return [job for job in self.jobs.values() if job.status == JobStatus.COMPLETED]
    
    def update_job_status(self, job_number, action):
        """Update job status based on action"""
        job = self.get_job(job_number)
        if not job:
            print(f"❌ Job #{job_number} not found")
            return False
        
        success = False
        if action == 'start':
            success = job.start_job()
            if success:
                print(f"✅ Job #{job_number} started")
        elif action == 'pause':
            success = job.pause_job()
            if success:
                print(f"⏸️  Job #{job_number} paused")
        elif action == 'resume':
            success = job.resume_job()
            if success:
                print(f"▶️  Job #{job_number} resumed")
        elif action == 'complete':
            success = job.complete_job()
            if success:
                print(f"✅ Job #{job_number} completed")
        
        if success:
            self.save_jobs()
        else:
            print(f"❌ Failed to {action} job #{job_number}")
        
        return success
    
    def update_job_resolution(self, job_number, resolution_text):
        """Update the resolution text for a job"""
        job = self.get_job(job_number)
        if not job:
            print(f"❌ Job #{job_number} not found")
            return False
        
        job.set_resolution(resolution_text)
        self.save_jobs()
        print(f"✅ Resolution updated for job #{job_number}")
        return True
    
    def debug_email_parsing(self, email_body):
        """
        Debug method to see how email is parsed with strict rules
        """
        print(f"\n{'='*60}")
        print(f"DEBUG: Strict Email Parsing Analysis")
        print(f"{'='*60}")
        print(f"Email body length: {len(email_body)} chars")
        print(f"\nFirst 15 lines of email:")
        lines = email_body.split('\n')
        for i, line in enumerate(lines[:15]):
            line_num = i + 1
            if line.strip():
                print(f"  [{line_num:2}] '{line}'")
            else:
                print(f"  [{line_num:2}] (empty line)")
        
        room, issue, is_valid, error = self.extract_job_info_from_email(email_body)
        
        print(f"\nExtraction Results:")
        print(f"  Room Number: {room}")
        print(f"  Issue: {issue[:150] if issue else 'None'}...")
        print(f"  Is Valid: {is_valid}")
        print(f"  Error: {error}")
        
        print(f"\nValidation against strict format:")
        print(f"  1. Line 1 must start with 'Room Number:' or 'Room:' followed by number")
        print(f"  2. Line 2 must start with 'Issue:'")
        print(f"  3. Issue text continues until first blank line")
        
        print(f"{'='*60}\n")
        return room, issue, is_valid, error
    
    def test_strict_format(self, test_email):
        """
        Test if an email matches the strict format
        """
        print(f"\n{'='*60}")
        print(f"TESTING STRICT FORMAT")
        print(f"{'='*60}")
        
        room, issue, is_valid, error = self.extract_job_info_from_email(test_email)
        
        if is_valid:
            print(f"✅ VALID FORMAT")
            print(f"   Room: {room}")
            print(f"   Issue: {issue}")
        else:
            print(f"❌ INVALID FORMAT")
            print(f"   Error: {error}")
        
        print(f"{'='*60}")
        return is_valid
        
        # job_manager.py (add this method to the JobManager class)
    def remove_job(self, job_number):
        """Remove a job from the system"""
        job_number_str = str(job_number)
        if job_number_str in self.jobs:
            del self.jobs[job_number_str]
            self.save_jobs()
            print(f"✅ Job #{job_number} removed from job manager")
            return True
        else:
            print(f"❌ Job #{job_number} not found in job manager")
            return False
