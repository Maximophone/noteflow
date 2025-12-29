"""
Tests for Email Digest Processor

Tests cover:
- Gmail filtering (categories, automated detection)
- Importance scoring with mocked AI
- Digest file generation
- State management
"""

import os
import pytest
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock, MagicMock

# Set dummy API key before imports that require it
os.environ.setdefault('GOOGLE_API_KEY', 'test-key-for-unit-tests')

from processors.notes.email_digest import EmailDigestProcessor
from integrations.gmail_utils import GmailUtils


class TestGmailUtils:
    """Tests for Gmail utility functions."""
    
    def test_is_automated_email_with_campaign_header(self):
        """Email with X-Campaign header should be detected as automated."""
        gmail = GmailUtils()
        email = {
            'headers': {'X-Campaign': 'marketing-2025'},
            'from': 'newsletter@company.com'
        }
        is_automated, reason = gmail.is_automated_email(email)
        assert is_automated is True
        assert 'X-Campaign' in reason
    
    def test_is_automated_email_with_noreply(self):
        """Email from noreply address should be detected as automated."""
        gmail = GmailUtils()
        email = {
            'headers': {},
            'from': 'noreply@company.com'
        }
        is_automated, reason = gmail.is_automated_email(email)
        assert is_automated is True
        assert 'noreply' in reason
    
    def test_is_automated_email_personal(self):
        """Personal email should not be detected as automated."""
        gmail = GmailUtils()
        email = {
            'headers': {},
            'from': 'john.smith@example.com'
        }
        is_automated, reason = gmail.is_automated_email(email)
        assert is_automated is False
        assert reason == ''
    
    def test_is_filtered_category_promotions(self):
        """Promotional email should be filtered."""
        gmail = GmailUtils()
        email = {'labels': ['INBOX', 'CATEGORY_PROMOTIONS']}
        assert gmail.is_filtered_category(email) is True
    
    def test_is_filtered_category_personal(self):
        """Personal email should not be filtered."""
        gmail = GmailUtils()
        email = {'labels': ['INBOX', 'CATEGORY_PERSONAL']}
        assert gmail.is_filtered_category(email) is False
    
    def test_parse_email_address_with_name(self):
        """Parse email with name and address."""
        gmail = GmailUtils()
        result = gmail.parse_email_address('John Smith <john@example.com>')
        assert result['name'] == 'John Smith'
        assert result['email'] == 'john@example.com'
    
    def test_parse_email_address_only(self):
        """Parse email address only."""
        gmail = GmailUtils()
        result = gmail.parse_email_address('john@example.com')
        assert result['name'] == ''
        assert result['email'] == 'john@example.com'


class TestEmailDigestProcessor:
    """Tests for the Email Digest Processor."""
    
    @pytest.fixture
    def temp_dirs(self, tmp_path):
        """Create temporary directories for testing."""
        output_dir = tmp_path / "email_digests"
        output_dir.mkdir()
        state_file = tmp_path / "state.json"
        return output_dir, state_file
    
    @pytest.fixture
    def processor(self, temp_dirs):
        """Create processor with temp directories."""
        output_dir, state_file = temp_dirs
        return EmailDigestProcessor(
            output_dir=output_dir,
            state_file=state_file
        )
    
    def test_should_run_today_first_run(self, processor):
        """First run should always proceed."""
        assert processor._should_run_today() is True
    
    def test_should_run_today_already_ran(self, processor):
        """Should skip if already ran today."""
        state = {
            'last_run_date': datetime.now().strftime('%Y-%m-%d'),
            'last_run_timestamp': datetime.now().isoformat()
        }
        processor._save_state(state)
        assert processor._should_run_today() is False
    
    def test_should_run_today_ran_yesterday(self, processor):
        """Should run if last run was yesterday."""
        yesterday = datetime.now() - timedelta(days=1)
        state = {
            'last_run_date': yesterday.strftime('%Y-%m-%d'),
            'last_run_timestamp': yesterday.isoformat()
        }
        processor._save_state(state)
        assert processor._should_run_today() is True
    
    def test_get_last_run_timestamp_default(self, processor):
        """Default should be EARLIEST_DATE when no state exists."""
        timestamp = processor._get_last_run_timestamp()
        # Should return EARLIEST_DATE (2025-12-19) when no prior state
        assert timestamp == processor.EARLIEST_DATE
    
    def test_pre_filter_emails_removes_promotions(self, processor):
        """Pre-filter should remove promotional emails."""
        emails = [
            {'id': '1', 'labels': ['INBOX'], 'headers': {}, 'from': 'friend@example.com'},
            {'id': '2', 'labels': ['INBOX', 'CATEGORY_PROMOTIONS'], 'headers': {}, 'from': 'store@shop.com'},
        ]
        filtered, triage = processor._pre_filter_emails(emails)
        assert len(filtered) == 1
        assert filtered[0]['id'] == '1'
        assert len(triage) == 2  # Both emails logged
    
    def test_pre_filter_emails_removes_automated(self, processor):
        """Pre-filter should remove automated emails."""
        emails = [
            {'id': '1', 'labels': ['INBOX'], 'headers': {}, 'from': 'friend@example.com'},
            {'id': '2', 'labels': ['INBOX'], 'headers': {'X-Campaign': 'promo'}, 'from': 'news@letter.com'},
        ]
        filtered, triage = processor._pre_filter_emails(emails)
        assert len(filtered) == 1
        assert filtered[0]['id'] == '1'
        assert len(triage) == 2  # Both emails logged


class TestEmailDigestProcessorAsync:
    """Async tests for Email Digest Processor."""
    
    @pytest.fixture
    def temp_dirs(self, tmp_path):
        """Create temporary directories for testing."""
        output_dir = tmp_path / "email_digests"
        output_dir.mkdir()
        state_file = tmp_path / "state.json"
        return output_dir, state_file
    
    @pytest.fixture
    def processor(self, temp_dirs):
        """Create processor with temp directories."""
        output_dir, state_file = temp_dirs
        return EmailDigestProcessor(
            output_dir=output_dir,
            state_file=state_file
        )
    
    @pytest.mark.asyncio
    async def test_score_importance_filters_low_scores(self, processor):
        """Importance scoring should filter emails below threshold."""
        emails = [
            {'id': '1', 'from': 'friend@example.com', 'to': 'me@example.com', 
             'subject': 'Lunch tomorrow?', 'snippet': 'Want to grab lunch?', 'date': '2025-12-28'},
            {'id': '2', 'from': 'store@shop.com', 'to': 'me@example.com',
             'subject': '50% OFF SALE!!!', 'snippet': 'Amazing deals...', 'date': '2025-12-28'},
        ]
        
        # Mock AI response
        mock_response = Mock()
        mock_response.error = None
        mock_response.content = json.dumps([
            {'email_id': '1', 'score': 8, 'reason': 'Personal email from friend'},
            {'email_id': '2', 'score': 2, 'reason': 'Marketing email'},
        ])
        
        with patch.object(processor.ai_model, 'message', return_value=mock_response):
            important, triage = await processor._score_importance(emails)
        
        assert len(important) == 1
        assert important[0]['id'] == '1'
        assert important[0]['importance_score'] == 8
        assert len(triage) == 2  # Both emails logged
    
    @pytest.mark.asyncio
    async def test_create_single_digest(self, processor, temp_dirs):
        """Test digest file creation."""
        output_dir, _ = temp_dirs
        
        emails = [{
            'id': 'msg1',
            'thread_id': 'thread1',
            'from': 'John Smith <john@example.com>',
            'to': 'me@example.com',
            'subject': 'Project Update',
            'date': 'Sat, 28 Dec 2025 10:30:00 +0000',
            'body': 'Here is the update on the project.',
            'snippet': 'Here is the update...',
            'labels': ['INBOX'],
        }]
        
        # Create digest
        await processor._create_single_digest('2025-12-28', emails)
        
        # Check file was created
        digest_file = output_dir / '2025-12-28 Emails.md'
        assert digest_file.exists()
        
        content = digest_file.read_text()
        assert 'Email Digest - 2025-12-28' in content
        assert 'john@example.com' in content  # Email address before entity resolution
        assert 'Project Update' in content
    
    @pytest.mark.asyncio
    async def test_process_all_updates_state(self, processor):
        """process_all should update state after successful run."""
        # Mock Gmail to return no emails
        with patch.object(processor.gmail, 'get_emails_since', return_value=[]):
            await processor.process_all()
        
        # State should be updated
        state = processor._load_state()
        assert state.get('last_run_date') == datetime.now().strftime('%Y-%m-%d')
    
    @pytest.mark.asyncio
    async def test_process_all_skips_if_already_run(self, processor):
        """process_all should skip if already run today."""
        # Set state to today
        state = {
            'last_run_date': datetime.now().strftime('%Y-%m-%d'),
        }
        processor._save_state(state)
        
        # Mock Gmail - should NOT be called
        mock_get_emails = Mock()
        with patch.object(processor.gmail, 'get_emails_since', mock_get_emails):
            await processor.process_all()
        
        mock_get_emails.assert_not_called()
