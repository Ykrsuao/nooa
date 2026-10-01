# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run the CLI with a file-triggered Ctrl+C for headless Windows tests."""

import _thread
import sys
import threading
from pathlib import Path

from nooa_cli import main

stop_file = Path(sys.argv.pop(1))
done = threading.Event()


def stop_on_request() -> None:
    # Blocking stdin reads in a helper thread can stall native-library startup
    # on Windows. Poll a private file instead; no production shutdown API is needed.
    while not done.wait(0.05):
        if stop_file.exists():
            _thread.interrupt_main()
            return


watcher = threading.Thread(target=stop_on_request)
watcher.start()
try:
    main()
finally:
    done.set()
    watcher.join()
