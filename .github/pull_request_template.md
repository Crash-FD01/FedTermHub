<!-- SPDX-FileCopyrightText: 2026 Crash -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
### Cosa cambia / What changes

### Perché / Why
<!-- es. Closes #123. Per un bug: la causa, non solo il sintomo. -->

### Verifiche / Checks
- [ ] `python3 tests/test_core.py` e `python3 tests/test_theme_parity.py` passano / pass
- [ ] I test GTK passano sotto Xvfb / GTK tests pass under Xvfb (`xvfb-run -a python3 tests/test_ui_behaviours.py` e gli altri)
- [ ] Le modifiche allo stile sono in entrambi i temi / Style changes are in both themes
- [ ] Provato su / Tested on: <!-- Fedora 44, GNOME, Wayland/X11 -->
- [ ] I file nuovi hanno l'intestazione SPDX / New files carry an SPDX header
