"""
Gmail API utilities for email digest processor.

Provides methods to:
- Authenticate with Gmail API (reusing existing OAuth flow)
- Fetch emails since a given timestamp
- Get thread context for embedding
- Detect automated/marketing emails
"""

import os
import pickle
import base64
from datetime import datetime
from typing import List, Dict, Optional, Any
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from config.logging_config import setup_logger
from config.services_config import GOOGLE_SCOPES

logger = setup_logger(__name__)


# Gmail categories to filter out (promotional, social, etc.)
# Note: CATEGORY_FORUMS removed - catches legitimate org emails
FILTERED_CATEGORIES = {
    'CATEGORY_PROMOTIONS',
    'CATEGORY_SOCIAL', 
    'CATEGORY_UPDATES',
}

# Headers that indicate automated/mailing list emails
AUTOMATED_HEADERS = [
    'List-Unsubscribe',
    'List-Id',
    'X-Mailer',
    'X-Campaign',
    'X-Mailchimp',
    'Precedence',  # Often 'bulk' or 'list'
]

# No-reply sender patterns
NO_REPLY_PATTERNS = [
    'noreply@',
    'no-reply@',
    'donotreply@',
    'notifications@',
    'notify@',
    'mailer@',
    'automated@',
    'calendar-notification@google.com',
    'calendar-server.bounces.google.com',
]

# Calendar/meeting response patterns in subject
CALENDAR_SUBJECT_PATTERNS = [
    'accepted:',
    'declined:',
    'tentative:',
    'invitation:',
    'updated invitation:',
    'canceled event:',
    'cancelled event:',
]


class GmailUtils:
    """Gmail API utilities for fetching and processing emails."""
    
    def __init__(self, credentials_path: str = 'credentials.json'):
        self.credentials_path = credentials_path
        self.creds = None
        self.service = None
    
    def _get_credentials(self):
        """Get or refresh OAuth credentials, reusing existing token.pickle."""
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                self.creds = pickle.load(token)
        
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, GOOGLE_SCOPES)
                self.creds = flow.run_local_server(port=0)
            
            with open('token.pickle', 'wb') as token:
                pickle.dump(self.creds, token)
        
        return self.creds
    
    def _get_service(self):
        """Get Gmail API service, initializing if needed."""
        if self.service is None:
            creds = self._get_credentials()
            self.service = build('gmail', 'v1', credentials=creds)
        return self.service
    
    def get_emails_since(self, since_timestamp: datetime, 
                         include_sent: bool = True,
                         include_received: bool = True) -> List[Dict[str, Any]]:
        """
        Fetch emails since a given timestamp.
        
        Args:
            since_timestamp: Fetch emails after this time
            include_sent: Include sent emails
            include_received: Include received emails
            
        Returns:
            List of email dicts with id, thread_id, subject, from, to, date, body, labels
        """
        service = self._get_service()
        
        # Gmail uses epoch seconds for after: query
        epoch_seconds = int(since_timestamp.timestamp())
        
        emails = []
        
        # Build query - Gmail API uses 'after:' with epoch timestamp
        base_query = f'after:{epoch_seconds}'
        
        queries = []
        if include_received:
            queries.append(f'{base_query} in:inbox')
        if include_sent:
            queries.append(f'{base_query} in:sent')
        
        for query in queries:
            try:
                results = service.users().messages().list(
                    userId='me',
                    q=query,
                    maxResults=100  # Reasonable limit per day
                ).execute()
                
                messages = results.get('messages', [])
                
                for msg_ref in messages:
                    msg_data = self._get_message_details(msg_ref['id'])
                    if msg_data:
                        emails.append(msg_data)
                        
            except Exception as e:
                logger.error(f"Error fetching emails with query '{query}': {e}")
        
        # Sort by date, oldest first (for chronological processing)
        emails.sort(key=lambda x: x.get('date', ''))
        
        return emails
    
    def _get_message_details(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Fetch full message details by ID."""
        service = self._get_service()
        
        try:
            msg = service.users().messages().get(
                userId='me',
                id=message_id,
                format='full'
            ).execute()
            
            headers = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
            
            # Extract body
            body = self._extract_body(msg['payload'])
            
            return {
                'id': message_id,
                'thread_id': msg.get('threadId'),
                'subject': headers.get('Subject', '(No Subject)'),
                'from': headers.get('From', ''),
                'to': headers.get('To', ''),
                'cc': headers.get('Cc', ''),
                'date': headers.get('Date', ''),
                'labels': msg.get('labelIds', []),
                'headers': headers,
                'body': body,
                'snippet': msg.get('snippet', ''),
                'attachments': self._extract_attachments(msg['payload']),
            }
            
        except Exception as e:
            logger.error(f"Error fetching message {message_id}: {e}")
            return None
    
    def _extract_body(self, payload: Dict) -> str:
        """Extract plain text body from message payload."""
        body = ''
        
        if 'body' in payload and payload['body'].get('data'):
            body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
        
        elif 'parts' in payload:
            for part in payload['parts']:
                mime_type = part.get('mimeType', '')
                
                # Prefer plain text
                if mime_type == 'text/plain' and part.get('body', {}).get('data'):
                    body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='replace')
                    break
                
                # Recurse into multipart
                elif mime_type.startswith('multipart/'):
                    body = self._extract_body(part)
                    if body:
                        break
            
            # Fallback to HTML if no plain text
            if not body:
                for part in payload['parts']:
                    if part.get('mimeType') == 'text/html' and part.get('body', {}).get('data'):
                        html = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='replace')
                        # Basic HTML stripping (could use BeautifulSoup for better results)
                        import re
                        body = re.sub(r'<[^>]+>', '', html)
                        body = re.sub(r'\s+', ' ', body).strip()
                        break
        
        return body.strip()
    
    def _extract_attachments(self, payload: Dict) -> List[Dict[str, Any]]:
        """Extract attachment info (filename and size) from message payload."""
        attachments = []
        
        def _scan_parts(parts: List[Dict]) -> None:
            for part in parts:
                filename = part.get('filename', '')
                body = part.get('body', {})
                
                # Check if this part is an attachment (has filename and size)
                if filename and body.get('size', 0) > 0:
                    attachments.append({
                        'filename': filename,
                        'size': body.get('size', 0),
                        'mime_type': part.get('mimeType', ''),
                    })
                
                # Recurse into nested parts
                if 'parts' in part:
                    _scan_parts(part['parts'])
        
        if 'parts' in payload:
            _scan_parts(payload['parts'])
        
        return attachments
    
    def get_thread_messages(self, thread_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Get messages from a thread for context.
        
        Args:
            thread_id: Gmail thread ID
            limit: Maximum number of messages to return (most recent)
            
        Returns:
            List of message dicts, oldest first (for reading context)
        """
        service = self._get_service()
        
        try:
            thread = service.users().threads().get(
                userId='me',
                id=thread_id,
                format='full'
            ).execute()
            
            messages = []
            for msg in thread.get('messages', []):
                headers = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
                body = self._extract_body(msg['payload'])
                attachments = self._extract_attachments(msg['payload'])
                
                messages.append({
                    'id': msg['id'],
                    'from': headers.get('From', ''),
                    'to': headers.get('To', ''),
                    'date': headers.get('Date', ''),
                    'subject': headers.get('Subject', ''),
                    'body': body,  # Full body - let caller handle truncation
                    'snippet': msg.get('snippet', ''),
                    'attachments': attachments,
                })
            
            # Return most recent N messages, but in chronological order
            if len(messages) > limit:
                messages = messages[-limit:]
            
            return messages
            
        except Exception as e:
            logger.error(f"Error fetching thread {thread_id}: {e}")
            return []
    
    def is_automated_email(self, email: Dict[str, Any]) -> tuple[bool, str]:
        """
        Check if an email appears to be automated/marketing.
        
        Args:
            email: Email dict with headers
            
        Returns:
            Tuple of (is_automated, reason)
        """
        headers = email.get('headers', {})
        from_addr = email.get('from', '').lower()
        
        # Check for strong automated headers (skip List-* as they catch org emails)
        for header in ['X-Campaign', 'X-Mailchimp', 'X-Mailer']:
            if header in headers:
                return True, f"Header: {header}"
        
        # Precedence header with bulk/junk value (skip 'list' - used by orgs)
        if 'Precedence' in headers:
            value = headers['Precedence'].lower()
            if value in ('bulk', 'junk'):
                return True, f"Precedence: {value}"
        
        # Check for no-reply sender patterns
        for pattern in NO_REPLY_PATTERNS:
            if pattern in from_addr:
                return True, f"Sender pattern: {pattern}"
        
        # Check for calendar response subjects
        subject = email.get('subject', '').lower()
        for pattern in CALENDAR_SUBJECT_PATTERNS:
            if subject.startswith(pattern):
                return True, f"Calendar subject: {pattern}"
        
        return False, ""
    
    def is_filtered_category(self, email: Dict[str, Any]) -> bool:
        """
        Check if email is in a filtered category (promotions, social, etc.).
        
        Args:
            email: Email dict with labels
            
        Returns:
            True if email should be filtered out
        """
        labels = set(email.get('labels', []))
        return bool(labels & FILTERED_CATEGORIES)
    
    def parse_email_address(self, email_string: str) -> Dict[str, str]:
        """
        Parse email address string into name and address.
        
        Args:
            email_string: e.g., "John Smith <john@example.com>"
            
        Returns:
            Dict with 'name' and 'email' keys
        """
        import re
        
        # Match "Name <email>" pattern
        match = re.match(r'^([^<]+)<([^>]+)>$', email_string.strip())
        if match:
            return {
                'name': match.group(1).strip().strip('"'),
                'email': match.group(2).strip(),
            }
        
        # Just email address
        return {
            'name': '',
            'email': email_string.strip(),
        }
    
    def format_datetime(self, date_string: str) -> Optional[datetime]:
        """Parse email date header into datetime."""
        try:
            return parsedate_to_datetime(date_string)
        except Exception:
            return None
