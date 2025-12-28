"""
Tests for Email Summary Generator Processor

Tests cover:
- Monthly index management
- Participant and entity extraction
- Summary generation
- Processing workflow
"""

import os
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock, MagicMock

# Set dummy API key before imports that require it
os.environ.setdefault('GOOGLE_API_KEY', 'test-key-for-unit-tests')

from processors.notes.email_summary_generator import EmailSummaryGenerator


@pytest.fixture
def temp_dirs(tmp_path):
    """Create temporary directories for testing."""
    input_dir = tmp_path / "email_digests"
    input_dir.mkdir()
    return input_dir


@pytest.fixture
def processor(temp_dirs):
    """Create processor with temp directories."""
    return EmailSummaryGenerator(
        input_dir=temp_dirs,
        index_dir=temp_dirs,
    )


class TestShouldProcess:
    """Tests for should_process logic."""
    
    def test_should_process_email_category(self, processor):
        """Should process files with email category."""
        frontmatter = {'category': 'email'}
        assert processor.should_process('2025-12-28 Emails.md', frontmatter) is True
    
    def test_should_not_process_meeting_category(self, processor):
        """Should not process files with meeting category."""
        frontmatter = {'category': 'meeting'}
        assert processor.should_process('meeting.md', frontmatter) is False
    
    def test_should_not_process_underscore_files(self, processor):
        """Should not process files starting with underscore."""
        frontmatter = {'category': 'email'}
        assert processor.should_process('_Email Triage Log.md', frontmatter) is False
    
    def test_should_not_process_index_files(self, processor):
        """Should not process index files."""
        frontmatter = {'category': 'email'}
        assert processor.should_process('2025-12 Email Index.md', frontmatter) is False


class TestExtraction:
    """Tests for participant and entity extraction."""
    
    def test_extract_participants(self, processor):
        """Should extract participant wikilinks from From/To lines."""
        content = """
*From:* [[John Smith]] (john@example.com) → *To:* [[Jane Doe]] (jane@example.com)

Some email content here.
"""
        participants = processor._extract_participants(content)
        assert '[[John Smith]]' in participants
        assert '[[Jane Doe]]' in participants
    
    def test_extract_entities(self, processor):
        """Should extract entity wikilinks excluding participants."""
        content = """
*From:* [[John Smith]] → *To:* [[Jane Doe]]

We discussed [[Project Alpha]] with [[Acme Corp]] today.
"""
        entities = processor._extract_entities(content)
        assert '[[Project Alpha]]' in entities
        assert '[[Acme Corp]]' in entities
        # Should not include participants
        assert '[[John Smith]]' not in entities
        assert '[[Jane Doe]]' not in entities
    
    def test_parse_wikilinks(self, processor):
        """Should correctly parse wikilinks from text."""
        text = "Meeting with [[John Smith]] about [[Project X]] and [[Company Y]]."
        links = processor._parse_wikilinks(text)
        assert len(links) == 3
        assert '[[John Smith]]' in links
        assert '[[Project X]]' in links
        assert '[[Company Y]]' in links


class TestBuildTitle:
    """Tests for title building."""
    
    def test_build_title(self, processor):
        """Should build correct title from frontmatter."""
        frontmatter = {'email_count': 5, 'thread_count': 2}
        title = processor._build_title(frontmatter)
        assert title == "2 threads, 5 emails"


class TestMonthlyIndex:
    """Tests for monthly index management."""
    
    def test_get_monthly_index_path(self, processor, temp_dirs):
        """Should generate correct index path."""
        path = processor._get_monthly_index_path('2025-12-28')
        assert path == temp_dirs / '2025-12 Email Index.md'
    
    def test_get_previous_month(self, processor):
        """Should correctly calculate previous month."""
        assert processor._get_previous_month('2025-12-28') == '2025-11'
        assert processor._get_previous_month('2025-01-15') == '2024-12'
    
    def test_ensure_monthly_index_creates_file(self, processor, temp_dirs):
        """Should create index file if it doesn't exist."""
        index_path = processor._ensure_monthly_index_exists('2025-12-28')
        assert index_path.exists()
        assert index_path.name == '2025-12 Email Index.md'
    
    def test_parse_monthly_index_empty(self, processor, temp_dirs):
        """Should return empty dict for empty index."""
        index_path = temp_dirs / '2025-12 Email Index.md'
        index_path.write_text('')
        
        entries = processor._parse_monthly_index(index_path)
        assert entries == {}
    
    def test_parse_monthly_index_with_entries(self, processor, temp_dirs):
        """Should correctly parse index with entries."""
        index_content = """# 2025-12-28 - 2 threads, 4 emails

*Source:* [[2025-12-28 Emails]]

**Participants:** [[John Smith]], [[Jane Doe]]
**Mentioned:** [[Project Alpha]]

## Summary
- Discussed project updates

---

"""
        index_path = temp_dirs / '2025-12 Email Index.md'
        index_path.write_text(index_content)
        
        entries = processor._parse_monthly_index(index_path)
        assert '[[2025-12-28 Emails]]' in entries
        entry = entries['[[2025-12-28 Emails]]']
        assert entry['date'] == '2025-12-28'
        assert entry['title'] == '2 threads, 4 emails'
        assert '[[John Smith]]' in entry['participants']
        assert '[[Project Alpha]]' in entry['entities']
    
    def test_rebuild_monthly_index(self, processor, temp_dirs):
        """Should rebuild index with sorted entries."""
        index_path = temp_dirs / '2025-12 Email Index.md'
        index_path.write_text('')
        
        entries = {
            '[[2025-12-27 Emails]]': {
                'date': '2025-12-27',
                'title': '1 thread, 2 emails',
                'summary': 'Previous day summary',
                'participants': ['[[John]]'],
                'entities': [],
            },
            '[[2025-12-28 Emails]]': {
                'date': '2025-12-28',
                'title': '2 threads, 4 emails',
                'summary': 'Today summary',
                'participants': ['[[Jane]]'],
                'entities': ['[[Project]]'],
            },
        }
        
        processor._rebuild_monthly_index(index_path, entries)
        
        content = index_path.read_text()
        # Newer entries should come first
        assert content.index('2025-12-28') < content.index('2025-12-27')
        assert '**Participants:** [[Jane]]' in content
        assert '**Mentioned:** [[Project]]' in content


class TestSummaryGeneration:
    """Tests for summary generation and callouts."""
    
    def test_generate_summary_callout(self, processor):
        """Should generate correct summary callout."""
        summary = "## Summary\n- Email 1 discussed X\n- Email 2 discussed Y"
        callout = processor._generate_summary_callout(summary, '2025-12-28')
        
        assert processor.SUMMARY_START in callout
        assert processor.SUMMARY_END in callout
        assert '[[2025-12 Email Index]]' in callout
        assert summary in callout
    
    def test_remove_summary_section(self, processor):
        """Should remove existing summary section."""
        content = f"""Before content

{processor.SUMMARY_START}
Old summary here
{processor.SUMMARY_END}

After content"""
        
        result = processor._remove_summary_section(content)
        assert processor.SUMMARY_START not in result
        assert 'Old summary here' not in result
        assert 'Before content' in result
        assert 'After content' in result


class TestProcessFile:
    """Async tests for file processing."""
    
    @pytest.mark.asyncio
    async def test_process_file_creates_summary(self, processor, temp_dirs):
        """Should generate summary and update index."""
        # Create test digest file
        digest_content = """---
date: '2025-12-28'
category: email
email_count: 2
thread_count: 1
processing_stages:
  - email_digest_created
  - entities_resolved
---
# Email Digest - 2025-12-28

## Subject: Project Update
*From:* [[John Smith]] → *To:* [[Jane Doe]]

We need to discuss [[Project Alpha]].
"""
        digest_file = temp_dirs / '2025-12-28 Emails.md'
        digest_file.write_text(digest_content)
        
        # Mock AI response
        mock_response = Mock()
        mock_response.error = None
        mock_response.content = """## Summary
- John emailed Jane about Project Alpha

## Action Items
- Follow up on Project Alpha discussion
"""
        
        with patch.object(processor.ai_model, 'message', return_value=mock_response):
            await processor.process_file('2025-12-28 Emails.md')
        
        # Check digest was updated with summary
        updated_content = digest_file.read_text()
        assert processor.SUMMARY_START in updated_content
        assert 'Project Alpha' in updated_content
        
        # Check index was created/updated
        index_file = temp_dirs / '2025-12 Email Index.md'
        assert index_file.exists()
        index_content = index_file.read_text()
        assert '2025-12-28' in index_content
        assert '[[John Smith]]' in index_content or '[[Jane Doe]]' in index_content
