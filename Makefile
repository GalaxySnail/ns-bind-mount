CC      ?= cc
CFLAGS  ?= -O2 -g -Wall -Wextra
LDFLAGS ?=

TARGET  := ns-bind-mount
PREFIX  ?= /usr/local

.PHONY: all clean test install uninstall

all: $(TARGET)

$(TARGET): ns_bind_mount.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TARGET)

test: $(TARGET)
	BIN=$(CURDIR)/$(TARGET) tests/run-tests.sh

install: $(TARGET)
	install -Dm755 $(TARGET) $(DESTDIR)$(PREFIX)/bin/$(TARGET)

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/$(TARGET)
