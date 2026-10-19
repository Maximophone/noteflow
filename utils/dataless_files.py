"""
Let this process download Google Drive files that exist only in the cloud.

Drive for desktop leaves recordings as "dataless" placeholders until someone
opens them. macOS will not fetch one for a process started by launchd unless
the process opts in, so every read fails with EDEADLK ("Resource deadlock
avoided"); the transcriber retried 2026-10-15-matilda-rui-2.m4a for 20 hours
that way, until reading it from a shell downloaded it. Opting in is a single
process-wide I/O policy, set once at startup.
"""

import ctypes
import ctypes.util
import os
import sys

from config.logging_config import setup_logger

logger = setup_logger(__name__)

# From <sys/resource.h>.
IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
IOPOL_SCOPE_PROCESS = 0
IOPOL_MATERIALIZE_DATALESS_FILES_ON = 2


def enable_dataless_file_downloads() -> bool:
    """Ask macOS to download cloud-only files when this process reads them.

    Returns True if the policy is now on. Elsewhere, or if the call fails, it
    logs and returns False rather than stopping the service: everything still
    works except reading files that are not on disk yet.
    """
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        result = libc.setiopolicy_np(
            IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
            IOPOL_SCOPE_PROCESS,
            IOPOL_MATERIALIZE_DATALESS_FILES_ON,
        )
    except (OSError, AttributeError) as e:
        logger.warning("Could not enable downloading cloud-only Drive files: %s", e)
        return False
    if result != 0:
        err = ctypes.get_errno()
        logger.warning(
            "Could not enable downloading cloud-only Drive files: setiopolicy_np "
            "failed with errno %d (%s)", err, os.strerror(err),
        )
        return False
    logger.info("Enabled downloading cloud-only Drive files on read")
    return True
