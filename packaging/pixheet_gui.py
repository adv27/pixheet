"""The entry script that PyInstaller freezes into the Pixheet desktop app."""

from pixheet.gui import main

raise SystemExit(main())
