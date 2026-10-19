"""
Tests for opting in to downloading cloud-only Drive files, and for the log
line the transcriber writes when a placeholder still cannot be read.
"""

import ctypes
import ctypes.util
import errno
import sys
from unittest.mock import MagicMock, patch

import pytest

from processors.audio.transcriber import AudioTranscriber
from utils import dataless_files
from utils.dataless_files import (
    IOPOL_MATERIALIZE_DATALESS_FILES_ON,
    IOPOL_SCOPE_PROCESS,
    IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
    enable_dataless_file_downloads,
)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS I/O policy")
def test_policy_is_on_after_enabling():
    assert enable_dataless_file_downloads() is True
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    policy = libc.getiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS)
    assert policy == IOPOL_MATERIALIZE_DATALESS_FILES_ON


def test_does_nothing_off_macos():
    with patch.object(dataless_files.sys, "platform", "linux"), \
         patch.object(dataless_files.ctypes, "CDLL") as cdll:
        assert enable_dataless_file_downloads() is False
    cdll.assert_not_called()


def test_failed_call_warns_and_does_not_raise():
    libc = MagicMock()
    libc.setiopolicy_np.return_value = -1
    with patch.object(dataless_files.sys, "platform", "darwin"), \
         patch.object(dataless_files.ctypes, "CDLL", return_value=libc), \
         patch.object(dataless_files.ctypes, "get_errno", return_value=errno.EPERM), \
         patch.object(dataless_files, "logger") as logger:
        assert enable_dataless_file_downloads() is False
    logger.warning.assert_called_once()
    assert "Could not enable downloading cloud-only Drive files" in logger.warning.call_args[0][0]


@pytest.mark.asyncio
async def test_edeadlk_is_logged_as_cloud_only_placeholder(tmp_path):
    input_dir = tmp_path / "incoming"
    input_dir.mkdir()
    (input_dir / "meeting.m4a").write_bytes(b"fake audio")
    with patch("processors.audio.transcriber.AI"), \
         patch("processors.audio.transcriber.get_prompt", return_value=""):
        transcriber = AudioTranscriber(
            input_dir=input_dir,
            output_dir=tmp_path / "out",
            processed_dir=tmp_path / "processed",
            api_key="test-key",
        )
    transcriber.files_in_process.add("meeting.m4a")
    deadlock = OSError(errno.EDEADLK, "Resource deadlock avoided")
    with patch.object(transcriber, "transcribe_audio_file", side_effect=deadlock), \
         patch("processors.audio.transcriber.logger") as logger:
        with pytest.raises(OSError):
            await transcriber.process_single_file("meeting.m4a")
    assert "file is a cloud-only Drive placeholder, not downloaded" in logger.error.call_args[0][0]
    assert "meeting.m4a" in transcriber.failed_recently
