CC ?= cc
# CFLAGS con "?=" vale solo per una compilazione a mano: quando la
# distribuzione passa i propri flag (su Fedora %{optflags} via %set_build_flags)
# quelli vincono, ed e' giusto cosi'. L'helper maneggia credenziali, quindi
# deve ereditare tutto l'hardening della distribuzione — fra cui
# -fstack-clash-protection, -fcf-protection e -D_FORTIFY_SOURCE=3 — e non
# limitarsi a questi valori di ripiego.
CFLAGS ?= -O2 -g -fstack-protector-strong -D_FORTIFY_SOURCE=2
# Gli avvisi sono nostri e vanno aggiunti ai flag della distribuzione, non al
# posto loro: per questo "+=" e non dentro CFLAGS.
WARNFLAGS = -Wall -Wextra -Wformat=2
# Hardening del collegamento richiesto dalle linee guida Fedora. Anche qui in
# aggiunta, cosi' %{build_ldflags} resta intatto.
LDFLAGS += -Wl,-z,relro -Wl,-z,now
# Cartella delle librerie private (pacchetti che portano con se' FreeRDP 3): per esempio
#   make RPATH='$$ORIGIN/lib'
# Vuota per chi compila contro la FreeRDP del sistema (Fedora, install.sh).
RPATH ?=
ifneq ($(strip $(RPATH)),)
LDFLAGS += -Wl,-rpath,'$(RPATH)' -Wl,--enable-new-dtags
endif
PKGS = freerdp3 freerdp-client3 winpr3
CPPFLAGS += $(shell pkg-config --cflags $(PKGS))
LDLIBS += $(shell pkg-config --libs $(PKGS)) -pthread

all: fedtermrdp-native

fedtermrdp-native: fedtermrdp_native.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARNFLAGS) -std=gnu11 -o $@ $< $(LDFLAGS) $(LDLIBS)

clean:
	rm -f fedtermrdp-native

.PHONY: all clean
