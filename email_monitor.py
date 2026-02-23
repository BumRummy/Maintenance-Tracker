# email_monitor.py
import imaplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime, formatdate, make_msgid
import re
import smtplib
import ssl
import time
from datetime import datetime
import os

class EmailMonitor:
    def __init__(self, config):
        self.config = config
        self.email_config = config['email']
        self.notification_config = config.get('notification', {})
        self.imap = None
        self.smtp = None
        self.connected_imap = False
        self.connected_smtp = False
        self.startup_time = datetime.now()
        self.processed_ids = set()  # Track processed email IDs
        print(f"📧 Email monitor started at: {self.startup_time}")
        
        # Pre-compile regex patterns for speed
        self.room_patterns = [
            re.compile(r'^Room\s+Number\s*:\s*(\d+)', re.IGNORECASE),
            re.compile(r'^Room\s*(?:#\s*)?:\s*(\d+)', re.IGNORECASE)
        ]
        self.issue_pattern = re.compile(r'^Issue\s*:\s*(.+)', re.IGNORECASE)
        self.line2_pattern = re.compile(r'^Issue\s*:', re.IGNORECASE)
        self.email_regex = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+')
    
    def connect(self):
        """Connect to both IMAP and SMTP servers"""
        return self.connect_imap() and self.connect_smtp()
    
    def connect_imap(self):
        """Connect to IMAP server for receiving emails"""
        try:
            print(f"🔗 Connecting to IMAP server {self.email_config['imap_server']}...")
            
            context = ssl.create_default_context()
            self.imap = imaplib.IMAP4_SSL(
                self.email_config['imap_server'],
                self.email_config['imap_port'],
                ssl_context=context
            )
            
            self.imap.login(
                self.email_config['email_address'],
                self.email_config['password']
            )
            
            self.imap.select('INBOX')
            self.connected_imap = True
            print(f"✅ Successfully connected to IMAP: {self.email_config['email_address']}")
            return True
            
        except Exception as e:
            print(f"❌ IMAP connection failed: {str(e)}")
            self.connected_imap = False
            return False
    
    def connect_smtp(self):
        """Connect to SMTP server for sending emails"""
        try:
            # Get SMTP settings from config with defaults
            smtp_server = self.email_config.get('smtp_server', 'smtp.gmail.com')
            smtp_port = self.email_config.get('smtp_port', 587)
            use_tls = self.email_config.get('use_tls', True)
            
            print(f"📤 Connecting to SMTP server {smtp_server}:{smtp_port}...")
            
            if use_tls:
                # Use TLS (port 587)
                self.smtp = smtplib.SMTP(smtp_server, smtp_port)
                self.smtp.starttls()  # Upgrade to secure connection
            else:
                # Use SSL (port 465)
                context = ssl.create_default_context()
                self.smtp = smtplib.SMTP_SSL(smtp_server, smtp_port, context=context)
            
            # Login
            self.smtp.login(
                self.email_config['email_address'],
                self.email_config['password']
            )
            
            self.connected_smtp = True
            print(f"✅ Successfully connected to SMTP: {self.email_config['email_address']}")
            return True
            
        except Exception as e:
            print(f"❌ SMTP connection failed: {str(e)}")
            self.connected_smtp = False
            return False
    
    def disconnect(self):
        """Disconnect from both IMAP and SMTP servers"""
        if self.imap:
            try:
                self.imap.close()
                self.imap.logout()
            except:
                pass
            self.imap = None
        self.connected_imap = False
        
        if self.smtp:
            try:
                self.smtp.quit()
            except:
                pass
            self.smtp = None
        self.connected_smtp = False
    
    def test_connection(self):
        """Test both IMAP and SMTP connections"""
        try:
            imap_success = self.connect_imap()
            smtp_success = self.connect_smtp()
            
            if imap_success:
                print("✅ IMAP connection successful")
            else:
                print("❌ IMAP connection failed")
            
            if smtp_success:
                print("✅ SMTP connection successful")
            else:
                print("❌ SMTP connection failed")
            
            self.disconnect()
            return imap_success and smtp_success
            
        except Exception as e:
            print(f"❌ Connection test failed: {str(e)}")
            return False
    
    def send_instant_confirmation(self, original_email):
        """Send instant confirmation - just 'request received.'"""
        try:
            # Extract email address from sender
            sender = original_email.get('sender', '')
            email_match = self.email_regex.search(sender)
            if email_match:
                recipient = email_match.group(0)
            else:
                print(f"❌ Cannot extract email from sender: {sender}")
                return False
            
            # Connect to SMTP if not already connected
            if not self.connected_smtp:
                if not self.connect_smtp():
                    print("❌ Cannot send confirmation: SMTP connection failed")
                    return False
            
            # Create SIMPLE message - just "request received."
            msg = MIMEMultipart()
            msg['From'] = self.email_config['email_address']
            msg['To'] = recipient
            msg['Subject'] = "Re: Maintenance Request"
            
            # Just "request received." as requested - NOTHING ELSE
            msg.attach(MIMEText("request received.", 'plain'))
            
            # Send
            self.smtp.send_message(msg)
            
            print(f"✅ Instant confirmation sent to {recipient}: 'request received.'")
            return True
            
        except smtplib.SMTPAuthenticationError as e:
            print(f"❌ SMTP Authentication failed: {e}")
            self.connected_smtp = False
            return False
            
        except smtplib.SMTPException as e:
            print(f"❌ SMTP error sending confirmation: {str(e)}")
            self.connected_smtp = False
            return False
            
        except Exception as e:
            print(f"❌ Failed to send confirmation: {str(e)}")
            self.connected_smtp = False
            return False
    
    def send_friendly_confirmation(self, original_email, job_number):
        """Send friendly confirmation email with job details"""
        try:
            # Extract email address from sender
            sender = original_email.get('sender', '')
            email_match = self.email_regex.search(sender)
            if email_match:
                recipient = email_match.group(0)
            else:
                print(f"❌ Cannot extract email from sender: {sender}")
                return False
            
            # Connect to SMTP if not already connected
            if not self.connected_smtp:
                if not self.connect_smtp():
                    print("❌ Cannot send confirmation: SMTP connection failed")
                    return False
            
            # Create friendly message
            msg = MIMEMultipart()
            msg['From'] = self.email_config['email_address']
            msg['To'] = recipient
            msg['Subject'] = "Maintenance Request Received"
            
            # Friendly confirmation with job details
            room_number = original_email.get('room_number', 'Unknown')
            issue = original_email.get('issue', 'No issue provided')
            
            confirmation_text = f"""Thank you for submitting your maintenance request.

We have received your request and created Job #{job_number}.

Details:
• Room Number: {room_number}
• Issue: {issue}

Your job has been logged into our system and will be addressed by our maintenance team.

You will receive another email when your job is completed.

Thank you,
Maintenance Team"""
            
            msg.attach(MIMEText(confirmation_text, 'plain'))
            
            # Send
            self.smtp.send_message(msg)
            
            print(f"✅ Friendly confirmation sent to {recipient} for Job #{job_number}")
            return True
            
        except smtplib.SMTPAuthenticationError as e:
            print(f"❌ SMTP Authentication failed: {e}")
            self.connected_smtp = False
            return False
            
        except smtplib.SMTPException as e:
            print(f"❌ SMTP error sending confirmation: {str(e)}")
            self.connected_smtp = False
            return False
            
        except Exception as e:
            print(f"❌ Failed to send confirmation: {str(e)}")
            self.connected_smtp = False
            return False
    
    def send_completion_email(self, job, original_email_info, resolution=""):
        """Send job completion email as REPLY to original request with CC support"""
        try:
            # Extract original sender from email info
            sender = original_email_info.get('sender', '')
            original_subject = original_email_info.get('subject', 'Maintenance Request')
            email_match = self.email_regex.search(sender)
            
            if email_match:
                recipient = email_match.group(0)
            else:
                print(f"❌ Cannot extract email from sender: {sender}")
                return False
            
            # Connect to SMTP if not already connected
            if not self.connected_smtp:
                if not self.connect_smtp():
                    print("❌ Cannot send completion email: SMTP connection failed")
                    return False
            
            # Create email as a REPLY to the original
            msg = MIMEMultipart()
            msg['From'] = self.email_config['email_address']
            msg['To'] = recipient
            
            # Set as reply with "Re:" prefix
            if original_subject.lower().startswith('re:'):
                msg['Subject'] = original_subject
            else:
                msg['Subject'] = f"Re: {original_subject}"
            
            # Add In-Reply-To and References headers for proper threading
            msg['Date'] = formatdate(localtime=True)
            msg['Message-ID'] = make_msgid()
            
            # Get CC emails from config
            cc_emails = self.notification_config.get('cc_emails', '')
            if cc_emails:
                msg['Cc'] = cc_emails
                print(f"📧 CC addresses: {cc_emails}")
            
            # Format completion time
            completion_time = job.completion_time.strftime('%Y-%m-%d %H:%M') if job.completion_time else 'N/A'
            
            # Create email body WITHOUT time tracking section
            email_body = f"""
JOB COMPLETION NOTIFICATION
===========================

Job Details:
------------
Job Number: #{job.job_number}
Room Number: {job.room_number}
Status: COMPLETED
Completion Time: {completion_time}

Original Issue:
---------------
{job.issue}

Resolution:
-----------
{resolution if resolution else "No resolution details provided"}

---
This email was automatically sent by the Maintenance Tracker System.
"""
            
            msg.attach(MIMEText(email_body, 'plain'))
            
            # Send email with CC recipients
            all_recipients = [recipient]
            if cc_emails:
                # Split CC emails by comma and strip whitespace
                cc_list = [email.strip() for email in cc_emails.split(',') if email.strip()]
                all_recipients.extend(cc_list)
            
            self.smtp.send_message(msg)
            
            print(f"✅ Completion email sent as REPLY to {recipient}")
            if cc_emails:
                print(f"✅ CC'd to: {cc_emails}")
            return True
            
        except smtplib.SMTPAuthenticationError as e:
            print(f"❌ SMTP Authentication failed: {e}")
            self.connected_smtp = False
            return False
            
        except smtplib.SMTPException as e:
            print(f"❌ SMTP error sending completion email: {str(e)}")
            self.connected_smtp = False
            return False
            
        except Exception as e:
            print(f"❌ Failed to send completion email: {str(e)}")
            self.connected_smtp = False
            return False
    
    def send_email(self, to_email, subject, body, is_html=False, cc_emails=None):
        """Send a general email with optional CC"""
        try:
            # Connect to SMTP if not already connected
            if not self.connected_smtp:
                if not self.connect_smtp():
                    print("❌ Cannot send email: SMTP connection failed")
                    return False
            
            # Create message
            msg = MIMEMultipart()
            msg['From'] = self.email_config['email_address']
            msg['To'] = to_email
            msg['Subject'] = subject
            
            if cc_emails:
                msg['Cc'] = cc_emails
            
            if is_html:
                msg.attach(MIMEText(body, 'html'))
            else:
                msg.attach(MIMEText(body, 'plain'))
            
            # Prepare recipients list
            all_recipients = [to_email]
            if cc_emails:
                cc_list = [email.strip() for email in cc_emails.split(',') if email.strip()]
                all_recipients.extend(cc_list)
            
            # Send
            self.smtp.send_message(msg)
            
            print(f"✅ Email sent to {to_email}")
            if cc_emails:
                print(f"✅ CC'd to: {cc_emails}")
            return True
            
        except smtplib.SMTPAuthenticationError as e:
            print(f"❌ SMTP Authentication failed: {e}")
            self.connected_smtp = False
            return False
            
        except Exception as e:
            print(f"❌ Failed to send email: {str(e)}")
            self.connected_smtp = False
            return False
    
    def mark_all_existing_as_read(self):
        """Mark all existing unread emails as read on startup"""
        if not self.connected_imap:
            if not self.connect_imap():
                return False
        
        try:
            status, messages = self.imap.search(None, 'UNSEEN')
            if status != 'OK':
                return True
            
            email_ids = messages[0].split()
            if not email_ids:
                print("📭 No existing unread emails to mark as read")
                return True
            
            print(f"📨 Marking {len(email_ids)} existing unread emails as read...")
            
            for email_id in email_ids:
                try:
                    self.imap.store(email_id, '+FLAGS', '\\Seen')
                except:
                    pass
            
            print("✅ All existing emails marked as read")
            return True
            
        except Exception as e:
            print(f"❌ Error marking emails as read: {str(e)}")
            return False
    
    def parse_email_content_strict_fast(self, text):
        """Fast parsing of email content with STRICT format"""
        if not text or not isinstance(text, str):
            return None
        
        lines = text.split('\n')
        
        # Find first non-empty line
        line1 = None
        line1_idx = -1
        for i, line in enumerate(lines):
            if line.strip():
                line1 = line.strip()
                line1_idx = i
                break
        
        if not line1:
            return None
        
        # Fast room number extraction
        room_number = None
        for pattern in self.room_patterns:
            match = pattern.match(line1)
            if match:
                room_number = match.group(1)
                break
        
        if not room_number:
            return None
        
        # Find issue line (look ahead max 5 lines for speed)
        line2 = None
        line2_idx = -1
        for i in range(line1_idx + 1, min(line1_idx + 5, len(lines))):
            if lines[i].strip():
                line2 = lines[i].strip()
                line2_idx = i
                break
        
        if not line2 or not self.line2_pattern.match(line2):
            return None
        
        # Extract issue
        issue_match = self.issue_pattern.match(line2)
        if not issue_match:
            return None
        
        issue_parts = [issue_match.group(1).strip()]
        
        # Collect until blank line (max 10 lines for speed)
        for i in range(line2_idx + 1, min(line2_idx + 10, len(lines))):
            line = lines[i].strip()
            if not line:
                break
            issue_parts.append(line)
        
        full_issue = ' '.join(issue_parts)
        full_issue = ' '.join(full_issue.split())[:500]
        
        return {
            'room_number': room_number,
            'issue': full_issue
        }
    
    def get_email_body_fast(self, msg):
        """Fast email body extraction"""
        body = ""
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                
                # Skip attachments
                if "attachment" in content_disposition:
                    continue
                
                # Prefer plain text
                if content_type == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode('utf-8', errors='ignore')
                            break
                    except:
                        continue
        else:
            # Not multipart
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode('utf-8', errors='ignore')
            except:
                body = str(msg.get_payload())
        
        return body.strip()
    
    def is_valid_job_email_strict_fast(self, body):
        """Fast strict validation of email format"""
        lines = [line.strip() for line in body.split('\n') if line.strip()]
        
        if len(lines) < 2:
            return False
        
        # Check Line 1
        line1 = lines[0]
        room_valid = False
        for pattern in self.room_patterns:
            if pattern.match(line1):
                room_valid = True
                break
        
        if not room_valid:
            return False
        
        # Check Line 2
        line2 = lines[1]
        if not self.line2_pattern.match(line2):
            return False
        
        return True
    
    def mark_email_read_fast(self, uid):
        """Mark email as read quickly"""
        try:
            self.imap.uid('store', uid, '+FLAGS', '\\Seen')
            return True
        except:
            return False
    
    def check_new_emails_fast(self):
        """Fast email checking - optimized for speed"""
        if not self.connected_imap:
            if not self.connect_imap():
                return []
        
        try:
            # Use UID for better tracking and speed
            status, messages = self.imap.uid('search', None, 'UNSEEN')
            
            if status != 'OK':
                return []
            
            email_uids = messages[0].split()
            if not email_uids:
                return []
            
            print(f"📨 Fast check: Found {len(email_uids)} new unseen email(s)")
            
            jobs = []
            
            # Process max 5 emails at once for speed
            for uid in email_uids[:5]:
                try:
                    uid_str = uid.decode('utf-8')
                    
                    # Skip if already processed in this session
                    if uid_str in self.processed_ids:
                        continue
                    
                    # Fetch email quickly
                    status, msg_data = self.imap.uid('fetch', uid, '(RFC822)')
                    
                    if status != 'OK':
                        continue
                    
                    # Parse email
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            
                            # Get sender
                            sender = msg['From'] or "Unknown"
                            
                            # Get subject
                            subject = ""
                            if msg['Subject']:
                                subject_header = decode_header(msg['Subject'])
                                if subject_header and subject_header[0]:
                                    subj, encoding = subject_header[0]
                                    if isinstance(subj, bytes):
                                        subject = subj.decode(encoding or 'utf-8', errors='ignore')
                                    else:
                                        subject = str(subj)
                            
                            # Get body with fast extraction
                            body = self.get_email_body_fast(msg)
                            
                            # Fast validation
                            if not self.is_valid_job_email_strict_fast(body):
                                self.mark_email_read_fast(uid)
                                continue
                            
                            # Fast parsing
                            job_info = self.parse_email_content_strict_fast(body)
                            if job_info:
                                job_info.update({
                                    'email_uid': uid_str,
                                    'sender': sender,
                                    'subject': subject,
                                    'body': body[:200],
                                    'received_time': datetime.now()
                                })
                                jobs.append(job_info)
                                
                                # Mark as read immediately
                                self.mark_email_read_fast(uid)
                                self.processed_ids.add(uid_str)
                                print(f"✅ Fast-processed email {uid_str}")
                
                except Exception as e:
                    print(f"❌ Fast processing error for UID {uid}: {str(e)}")
                    continue
            
            return jobs
            
        except Exception as e:
            print(f"❌ Fast email check error: {str(e)}")
            self.connected_imap = False
            return []
    
    def check_new_emails(self):
        """Main entry point - uses fast checking"""
        return self.check_new_emails_fast()
