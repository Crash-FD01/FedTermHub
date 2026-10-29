# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copr usa questo Makefile quando il progetto è configurato con il metodo
# "Make SRPM". Produce il pacchetto sorgente (SRPM) a partire dal checkout del
# repository, così Copr ricostruisce da sé a ogni commit senza che si debba
# caricare nulla a mano.
#
# Copr invoca:  make -f .copr/Makefile srpm outdir="<cartella>" spec=<file>
#
# NB: la versione si legge DENTRO la ricetta, non con $(shell): make espande
# $(shell) mentre analizza il Makefile, quindi prima che dnf abbia installato
# rpmspec, e VERSION risulterebbe vuota.

srpm:
	dnf -y install rpm-build rpmdevtools git tar gzip
	set -eu; \
	VERSION="$$(rpmspec -q --qf '%{version}\n' fedtermhub.spec | head -1)"; \
	test -n "$$VERSION" || { echo "Versione non leggibile da fedtermhub.spec"; exit 1; }; \
	echo "Costruisco l'SRPM di fedtermhub $$VERSION"; \
	mkdir -p "$(outdir)" "$(CURDIR)/rpmbuild/SOURCES"; \
	git config --global --add safe.directory "$(CURDIR)" || true; \
	tar --exclude-vcs --exclude=rpmbuild \
	    --transform "s,^\.,fedtermhub-$$VERSION," \
	    -czf "$(CURDIR)/rpmbuild/SOURCES/fedtermhub-$$VERSION.tar.gz" .; \
	rpmbuild -bs fedtermhub.spec \
	    --define "_topdir $(CURDIR)/rpmbuild" \
	    --define "_sourcedir $(CURDIR)/rpmbuild/SOURCES" \
	    --define "_srcrpmdir $(outdir)"

.PHONY: srpm
