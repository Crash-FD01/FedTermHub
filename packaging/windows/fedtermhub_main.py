# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Punto di ingresso di FedTermHub.exe (PyInstaller): lo stesso di «python -m fedtermhub.qtui»."""
import sys

from fedtermhub.qtui.app import main

if __name__ == "__main__":
    sys.exit(main())
