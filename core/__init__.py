# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
#
# This file is part of AnimaWorks core/server, licensed under Apache-2.0.
# See LICENSE for the full license text.
"""AnimaWorks core package."""

import sys
import warnings


if sys.version_info[:2] != (3, 12):
    warnings.warn(
        f"AnimaWorks runtime is validated on Python 3.12; running {sys.version_info.major}.{sys.version_info.minor}",
        RuntimeWarning,
        stacklevel=2,
    )
